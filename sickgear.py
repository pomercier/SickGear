#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SickGear - FastAPI + SQLAlchemy Async (version optimisée)
Améliorations appliquées:
5. Compression des réponses optimisée
6. Cache template plus intelligent
7. Connection pooling pour HTTP client
8. Async file operations
12. Background tasks pour opérations lourdes
13. Configuration dynamique
"""

# =========================
# Imports
# =========================
import asyncio
import hashlib
import json
import random
import re
import shutil
import subprocess
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import aiofiles
import aiofiles.os
import httpx
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import BaseLoader, Environment, Template, select_autoescape
from pydantic import BaseModel, validator
from sqlalchemy import (
    Column,
    Integer,
    String,
    case,
    distinct,
    func,
    select
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from functools import lru_cache
import os

# Optimisation parsing : utiliser lxml si disponible
try:
    from lxml import etree, html
    LXML_AVAILABLE = True
except ImportError:
    LXML_AVAILABLE = False

# Cache Redis optionnel
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None

# =========================
# Config centralisée avec validation Pydantic
# =========================
class Settings(BaseModel):
    base_url: str = "http://192.168.0.111:8081"
    db_path: str = "/home/pomercier/Disque_dur/SickGear/sickbeard.db"
    host: str = "0.0.0.0"
    port: int = 5000
    template_cache_exp_min: int = 60
    template_cache_max_items: int = 200
    bg_update_min: int = 30
    httpx_timeout_s: float = 5.0
    httpx_retries: int = 2
    httpx_max_keepalive: int = 10
    httpx_max_connections: int = 20
    open_dolphin_allowed_base: str = "/home/pomercier/Disque_dur"
    background_sample_limit: int = 200
    enable_gzip: bool = True
    gzip_min_size: int = 1000
    gzip_compress_level: int = 6
    redis_url: str = "redis://localhost:6379"
    redis_cache_ttl_s: int = 300
    redis_enabled: bool = True
    config_file: str = "/tmp/sickgear_config.json"

    @validator('db_path', 'open_dolphin_allowed_base')
    def validate_paths(cls, v):
        if not Path(v).exists():
            raise ValueError(f"Path does not exist: {v}")
        return v

    class Config:
        json_encoders = {
            Path: lambda v: str(v)
        }

# Chargement initial de la configuration
def load_config() -> Settings:
    config_file = Path("/tmp/sickgear_config.json")
    if config_file.exists():
        try:
            with open(config_file, 'r') as f:
                config_data = json.load(f)
                return Settings(**config_data)
        except Exception:
            return Settings()
    return Settings()

CONFIG = load_config()

# =========================
# Modèles Pydantic
# =========================
class Show(BaseModel):
    Emission: Optional[str] = None
    Saison: Optional[int] = None
    Episode: Optional[int] = None
    Nom_episode: Optional[str] = None
    Reseau_de_streaming: Optional[str] = None
    Location: Optional[str] = None
    Showid: Optional[str] = None
    show_id: Optional[int] = None
    indexer_id: Optional[int] = None
    thumbnail: Optional[str] = None

class MissingEpisode(BaseModel):
    manquant: Optional[str] = None
    show_id: Optional[int] = None
    indexer_id: Optional[int] = None
    thumbnail: Optional[str] = None
    airdate: Optional[int] = None

class EndedShowData(BaseModel):
    urls: List[str] = []
    links: List[str] = []

class UpcomingShow(BaseModel):
    Emission: str
    Date_diffusion: str
    Image: str
    indexer_id: Optional[str] = None
    show_id: Optional[str] = None

# =========================
# SQLAlchemy ORM Models
# =========================
Base = declarative_base()

class TVShow(Base):
    __tablename__ = 'tv_shows'
    indexer_id = Column(Integer, primary_key=True)
    show_name = Column(String)
    indexer = Column(Integer)
    location = Column(String)
    status = Column(String)

class TVEpisode(Base):
    __tablename__ = 'tv_episodes'
    episode_id = Column(Integer, primary_key=True)
    showid = Column(Integer)
    season = Column(Integer)
    episode = Column(Integer)
    name = Column(String)
    network = Column(String)
    status = Column(Integer)
    file_size = Column(Integer)
    airdate = Column(Integer)

# =========================
# Engine & async session
# =========================
DATABASE_URL = f"sqlite+aiosqlite:///{CONFIG.db_path}"
engine = create_async_engine(DATABASE_URL, future=True, connect_args={"check_same_thread": False})
AsyncSessionFactory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# =========================
# Cache Redis et décorateur
# =========================
redis_client = None
if REDIS_AVAILABLE and CONFIG.redis_enabled:
    try:
        redis_client = redis.from_url(CONFIG.redis_url)
    except Exception:
        redis_client = None

def cache_results(key_prefix: str, model: Optional[Any] = None):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            if not redis_client:
                return await func(*args, **kwargs)
            arg_representation = str(args) + str(kwargs)
            unique_suffix = hashlib.md5(arg_representation.encode()).hexdigest()
            cache_key = f"{key_prefix}:{unique_suffix}"
            try:
                cached = await redis_client.get(cache_key)
                if cached:
                    data = json.loads(cached)
                    if model and isinstance(data, list):
                        return [model.model_validate(item) for item in data]
                    elif model:
                        return model.model_validate(data)
                    return data
            except Exception:
                pass
            result = await func(*args, **kwargs)
            try:
                if isinstance(result, list) and result and isinstance(result[0], BaseModel):
                    json_to_cache = json.dumps([item.model_dump() for item in result])
                elif isinstance(result, BaseModel):
                    json_to_cache = result.model_dump_json()
                else:
                    json_to_cache = json.dumps(result)
                await redis_client.setex(cache_key, CONFIG.redis_cache_ttl_s, json_to_cache)
            except Exception:
                pass
            return result
        return wrapper
    return decorator

# =========================
# Helpers
# =========================
_INDEXER_SHOWID_RE = re.compile(r"(?:cache/images/shows/|shows/|tvid_prodid=)(\d+)[-:](\d+)")

def extract_indexer_showid_from_url(url: str):
    if not url: return None, None
    match = _INDEXER_SHOWID_RE.search(url)
    if match: return match.group(1), match.group(2)
    parts = re.findall(r"(\d+)", url)
    return (parts[-2], parts[-1]) if len(parts) >= 2 else (None, None)

def format_image_url_from_raw(url: str) -> str:
    if not url or url.startswith("http"): return url
    if "cache/images/shows" in url: return f"{CONFIG.base_url}/{url.lstrip('/')}"
    idx, sid = extract_indexer_showid_from_url(url)
    if idx and sid: return f"{CONFIG.base_url}/cache/images/shows/{idx}-{sid}/thumbnails/poster.jpg"
    return f"{CONFIG.base_url}/{url.lstrip('/')}" if url.startswith("/") else url

def normalize_show_name(name: str) -> str:
    if not name: return ""
    return unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("utf-8").title().replace("'S", "'s")

def safe_parse_datetime(date_str: str) -> Optional[datetime]:
    if not date_str: return None
    formats = ["%Y%m%d%H%M", "%Y-%m-%d %H:%M", "%Y%m%d", "%d/%m/%Y %H:%M"]
    for fmt in formats:
        try:
            return datetime.strptime(str(date_str).strip(), fmt)
        except (ValueError, TypeError):
            continue
    return None

# =========================
# Template optimisé avec cache LRU
# =========================
class OptimizedTemplateEngine:
    def __init__(self):
        self.env = Environment(loader=BaseLoader(), autoescape=select_autoescape(["html", "xml"]), optimized=True)
        self._setup_filters()

    def _setup_filters(self):
        def jinja_format_date(dt_str: str) -> str:
            dt = safe_parse_datetime(dt_str)
            if not dt: return dt_str or ""
            months = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']
            return f"{dt.day} {months[dt.month - 1]}\n{dt.strftime('%H:%M')}"
        def jinja_show_link(indexer: Any, show_id: Any) -> str:
            return f"{CONFIG.base_url}/home/view-show?tvid_prodid={indexer}:{show_id}"
        self.env.filters['format_date'] = jinja_format_date
        self.env.filters['show_link'] = jinja_show_link

    @lru_cache(maxsize=CONFIG.template_cache_max_items)
    def _get_template(self, template_string: str) -> Template:
        return self.env.from_string(template_string)

    def render(self, template_string: str, **context) -> str:
        template = self._get_template(template_string)
        context.setdefault("base_url", CONFIG.base_url)
        return template.render(**context)

template_engine = OptimizedTemplateEngine()

# =========================
# Data Service (avec ORM et Cache)
# =========================
class DataService:
    @cache_results("shows_data", model=Show)
    async def get_shows(self) -> List[Show]:
        async with AsyncSessionFactory() as session:
            repert = str(Path(CONFIG.db_path).parent) + "/"
            next_episodes_cte = (
                select(TVEpisode.showid, TVEpisode.season, func.min(TVEpisode.episode).label("next_episode"))
                .where(TVEpisode.status.not_in([1, 5, 7]), TVEpisode.season > 0)
                .group_by(TVEpisode.showid, TVEpisode.season)
                .cte("next_episodes")
            )
            subquery = (
                select(
                    TVShow.show_name, TVShow.indexer_id, TVShow.indexer, TVEpisode.season, TVEpisode.episode,
                    TVEpisode.name, TVEpisode.network,
                    (TVShow.location + '/fanart.jpg').label("location"),
                    (f"{repert}cache/images/shows/" + TVShow.indexer.cast(String) + '-' + TVShow.indexer_id.cast(String) + '/fanart').label("showid"),
                    func.row_number().over(partition_by=TVShow.show_name, order_by=[TVEpisode.season, TVEpisode.episode]).label("rn")
                )
                .join(TVEpisode, TVShow.indexer_id == TVEpisode.showid)
                .join(next_episodes_cte, (TVEpisode.showid == next_episodes_cte.c.showid) & (TVEpisode.season == next_episodes_cte.c.season) & (TVEpisode.episode == next_episodes_cte.c.next_episode))
                .where(TVEpisode.status.not_in([1, 5, 7]), TVEpisode.season > 0)
                .distinct().subquery("ranked_episodes")
            )
            stmt = (
                select(
                    subquery.c.show_name.label("Emission"), subquery.c.season.label("Saison"), subquery.c.episode.label("Episode"),
                    subquery.c.name.label("Nom_episode"), subquery.c.network.label("Reseau_de_streaming"), subquery.c.location.label("Location"),
                    subquery.c.showid.label("Showid"), subquery.c.indexer_id.label("show_id"), subquery.c.indexer.label("indexer_id")
                )
                .where(subquery.c.rn == 1).order_by(func.lower(subquery.c.show_name))
            )
            result = await session.execute(stmt)
            rows = result.mappings().all()
            shows = [Show.model_validate(row) for row in rows]
            for show in shows:
                if show.indexer_id and show.show_id:
                    show.thumbnail = f"{CONFIG.base_url}/cache/images/shows/{show.indexer_id}-{show.show_id}/thumbnails/poster.jpg"
            return shows

    @cache_results("missing_episodes", model=MissingEpisode)
    async def get_missing_episodes(self) -> List[MissingEpisode]:
        async with AsyncSessionFactory() as session:
            season_str = case((TVEpisode.season < 10, '0' + TVEpisode.season.cast(String)), else_=TVEpisode.season.cast(String))
            episode_str = case((TVEpisode.episode < 10, '0' + TVEpisode.episode.cast(String)), else_=TVEpisode.episode.cast(String))
            manquant_expr = TVShow.show_name + ' S' + season_str + 'E' + episode_str
            stmt = (
                select(
                    distinct(manquant_expr).label("manquant"), TVShow.indexer_id.label("show_id"), TVShow.indexer.label("indexer_id"),
                    (f"{CONFIG.base_url}/cache/images/shows/" + TVShow.indexer.cast(String) + '-' + TVShow.indexer_id.cast(String) + '/thumbnails/poster.jpg').label("thumbnail"),
                    TVEpisode.airdate
                )
                .join(TVEpisode, TVShow.indexer_id == TVEpisode.showid, isouter=True)
                .where(TVEpisode.status.not_in([1, 5, 7]), TVEpisode.status < 100, TVEpisode.season != 0, TVEpisode.file_size == 0)
                .order_by("manquant")
            )
            result = await session.execute(stmt)
            rows = result.mappings().all()
            missing = [MissingEpisode.model_validate(row) for row in rows]
            missing.sort(key=lambda x: x.airdate or float('inf'))
            return missing

    @cache_results("ended_shows", model=EndedShowData)
    async def get_ended_shows(self) -> EndedShowData:
        async with AsyncSessionFactory() as session:
            stmt = (select(TVShow.indexer_id, TVShow.indexer).where(TVShow.status == 'Ended').order_by(TVShow.show_name))
            result = await session.execute(stmt)
            rows = result.all()
            image_urls = []
            image_links = []
            for row in rows:
                indexer_id, show_id = row
                if indexer_id and show_id:
                    image_urls.append(f"{CONFIG.base_url}/cache/images/shows/{show_id}-{indexer_id}/thumbnails/poster.jpg")
                    image_links.append(f"{CONFIG.base_url}/home/view-show?tvid_prodid={show_id}:{indexer_id}")
            return EndedShowData(urls=image_urls, links=image_links)

data_service = DataService()

# =========================
# Background image cache avec opérations async
# =========================
class BackgroundCache:
    def __init__(self):
        self.cache: Optional[str] = None
        self.last_update: Optional[datetime] = None
        self.update_interval = timedelta(minutes=CONFIG.bg_update_min)

    async def refresh(self):
        """Tâche de fond pour rafraîchir le cache"""
        try:
            prochaines_emissions = await data_service.get_shows()
            repertoire = str(Path(CONFIG.db_path).parent) + "/"
            await self._update_background(prochaines_emissions, repertoire)
        except Exception as e:
            print(f"Background cache refresh failed: {e}")

    async def _update_background(self, prochaines_emissions: List[Show], repertoire: str):
        if not prochaines_emissions:
            self.cache = ""
            self.last_update = datetime.now()
            return

        sample = random.sample(prochaines_emissions, k=min(len(prochaines_emissions), 10))
        for s in sample:
            showid_path = s.Showid
            if not showid_path:
                continue

            showdir = Path(showid_path)
            if not await aiofiles.os.path.exists(showdir):
                continue

            try:
                files = []
                # Correction ici : utiliser os.scandir au lieu de aiofiles.os.scandir
                for entry in os.scandir(showdir):
                    if entry.is_file() and entry.name.lower().endswith(('.jpg', '.jpeg', '.png')):
                        files.append(entry.path)
                        if len(files) >= CONFIG.background_sample_limit:
                            break

                if files:
                    chosen = random.choice(files)
                    self.cache = chosen.replace(repertoire, CONFIG.base_url + "/").replace("\\", "/")
                    self.last_update = datetime.now()
                    return
            except Exception:
                continue

        self.cache = ""
        self.last_update = datetime.now()

    async def get_background(self, prochaines_emissions: List[Show], repertoire: str) -> str:
        now = datetime.now()
        if self.cache and self.last_update and (now - self.last_update < self.update_interval):
            return self.cache

        if not prochaines_emissions:
            self.cache = ""
            self.last_update = now
            return self.cache

        # Mise à jour synchrone si nécessaire
        await self._update_background(prochaines_emissions, repertoire)
        return self.cache

background_cache = BackgroundCache()

# =========================
# HTTP helper avec connection pooling
# =========================
class HTTPClientManager:
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_keepalive_connections=CONFIG.httpx_max_keepalive,
                    max_connections=CONFIG.httpx_max_connections
                ),
                timeout=CONFIG.httpx_timeout_s
            )
        return self.client

    async def close(self):
        if self.client:
            await self.client.aclose()
            self.client = None

http_client_manager = HTTPClientManager()

async def http_get(url: str) -> Optional[str]:
    client = await http_client_manager.get_client()
    for attempt in range(CONFIG.httpx_retries + 1):
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
        except httpx.RequestError:
            if attempt >= CONFIG.httpx_retries:
                return None
            await asyncio.sleep(0.5 * (2 ** attempt))
    return None

class OptimizedHTMLParser:
    def parse_html(self, html_content: str) -> BeautifulSoup:
        return BeautifulSoup(html_content, 'lxml' if LXML_AVAILABLE else 'html.parser')
    def validate_structure(self, soup: BeautifulSoup) -> bool:
        return bool(soup.find(['div', 'body', 'html']))

html_parser = OptimizedHTMLParser()

@cache_results("schedule_data")
async def get_cached_schedule():
    html_content = await http_get(f"{CONFIG.base_url}/daily-schedule/")
    if not html_content:
        return []
    soup = html_parser.parse_html(html_content)
    if not html_parser.validate_structure(soup):
        return []
    data = []
    for show in soup.select(".daybyday-show"):
        if show.select_one(".over-layer0, .over-layer1"):
            continue
        name = show.get("data-name", "")
        time_raw = show.get("data-time", "")
        img = show.select_one("img")
        poster_url = img.get("src") if img else ""
        if name and time_raw and poster_url:
            data.append({"name": name, "time": time_raw, "poster": poster_url})
    return data

# =========================
# Template HTML
# =========================
template_string = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Émissions TV à venir</title><link rel="icon" type="image/x-icon" href="{{ base_url }}/images/ico/favicon.ico"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&display=swap" rel="stylesheet"><style>*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}:root{--bg-primary:#0a0a0f;--bg-secondary:#13131a;--bg-tertiary:#1a1a24;--bg-card:#16161f;--bg-hover:#1d1d28;--border-subtle:#252530;--border-light:#2a2a38;--text-primary:#e8e8f0;--text-secondary:#a8a8b8;--text-muted:#6a6a78;--accent-blue:#4a9eff;--accent-cyan:#3dd9eb;--shadow-sm:0 1px 3px rgba(0,0,0,0.3);--shadow-md:0 4px 12px rgba(0,0,0,0.4);--shadow-lg:0 8px 24px rgba(0,0,0,0.5);--transition-fast:150ms cubic-bezier(0.4,0,0.2,1);--transition-base:250ms cubic-bezier(0.4,0,0.2,1);--card-border-radius:8px}body{font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;margin:0;padding:0;background:linear-gradient(135deg,#0a0a0f 0%,#13131a 50%,#0f0f16 100%);background-attachment:fixed;min-height:100vh;color:var(--text-primary);line-height:1.6;-webkit-font-smoothing:antialiased}body::before{content:'';position:fixed;top:0;left:0;right:0;bottom:0;background-image:url("{{ bg_file }}");background-repeat:no-repeat;background-position:center;background-size:cover;opacity:0.03;z-index:-1;animation:subtle-shift 60s ease-in-out infinite alternate}@keyframes subtle-shift{0%{transform:scale(1) translateY(0)}100%{transform:scale(1.05) translateY(-10px)}}.content-wrapper{position:relative;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:2rem 0}.main-content{width:100%;max-width:1280px;padding:0 1.5rem}h1{color:var(--accent-cyan);text-align:center;font-weight:300;font-size:2.5rem;margin:1.5rem 0;letter-spacing:1px;position:relative;padding-bottom:10px}h1::after{content:'';position:absolute;bottom:0;left:50%;transform:translateX(-50%);width:60px;height:3px;background-color:var(--accent-blue);border-radius:3px}.page-precedente{margin-bottom:1.5rem}.page-precedente a{color:var(--text-secondary);text-decoration:none;font-size:0.9rem;font-weight:500;transition:var(--transition-base);display:inline-flex;align-items:center;gap:0.5rem;padding:0.5rem 1rem;border-radius:6px;border:1px solid var(--border-subtle);background:var(--bg-secondary)}.page-precedente a::before{content:'←';font-size:1rem;transition:var(--transition-fast)}.page-precedente a:hover{color:var(--accent-blue);border-color:var(--border-light);background:var(--bg-tertiary);transform:translateX(-2px)}.page-precedente a:hover::before{transform:translateX(-2px)}.card{background:var(--bg-card);border-radius:var(--card-border-radius);border:1px solid var(--border-subtle);margin-bottom:25px;overflow:hidden;box-shadow:var(--shadow-md);transition:var(--transition-base)}.card:hover{border-color:var(--border-light);box-shadow:var(--shadow-lg)}.card-header{padding:16px 20px;border-bottom:1px solid var(--border-subtle);background:linear-gradient(180deg,var(--bg-secondary) 0%,var(--bg-card) 100%);text-align:center}.card-header h2{margin:0;color:var(--accent-cyan);font-weight:400;font-size:1.5rem}.card-content{padding:15px}.grid-container{display:grid;gap:0;width:100%;margin:20px auto;border-radius:var(--card-border-radius);overflow:hidden;box-shadow:var(--shadow-md);background-color:var(--bg-card)}.show-grid{grid-template-columns:auto 1fr auto auto auto auto}.grid-header-row{display:contents}.grid-header{font-weight:600;padding:1rem 0.8rem;color:var(--text-secondary);letter-spacing:0.5px;font-size:0.95rem;text-transform:uppercase;display:flex;justify-content:center;align-items:center;text-align:center;background:var(--bg-secondary)}.grid-item{padding:0.7rem 0.8rem;border-bottom:1px solid var(--border-subtle);display:flex;align-items:center;min-height:65px;justify-content:center;text-align:center;transition:var(--transition-fast);color:var(--text-secondary)}.grid-item:nth-child(6n+2){justify-content:flex-start;text-align:left;color:var(--text-primary)}.show-row{display:contents}.show-row:hover .grid-item{background-color:var(--bg-hover);cursor:pointer}.show-thumbnail{width:45px;height:68px;border-radius:4px;object-fit:cover;transition:var(--transition-base);box-shadow:var(--shadow-sm);border:1px solid var(--border-subtle)}.show-thumbnail:hover{transform:scale(1.7);z-index:10;border-color:var(--accent-blue)}.missing-episodes-table{width:100%;max-width:600px;margin:0 auto;border-collapse:separate;border-spacing:0}.missing-episodes-table tr{display:flex;align-items:center;transition:background-color 0.2s}.missing-episodes-table tr:hover{background-color:var(--bg-hover)}.missing-episodes-table td{padding:8px;border-bottom:1px solid var(--border-subtle);color:var(--text-secondary)}.missing-episodes-table tr:last-child td{border-bottom:none}.missing-episodes-table td:first-child{width:55px;padding-right:15px}.missing-episodes-table td:last-child{flex:1;color:var(--text-primary)}.flex-center{display:flex;flex-wrap:wrap;justify-content:center;gap:20px;margin-top:20px}.show-cards{display:flex;flex-wrap:wrap;justify-content:center;gap:20px;padding:20px}.show-card{width:200px;transition:var(--transition-base)}.show-card:hover{transform:translateY(-4px)}.show-poster{position:relative;overflow:hidden;border-radius:var(--card-border-radius) var(--card-border-radius) 0 0;box-shadow:var(--shadow-md);height:293px;background:var(--bg-secondary)}.show-poster::before{content:'';position:absolute;inset:0;background:linear-gradient(180deg,transparent 50%,rgba(10,10,15,0.9) 100%);z-index:1;pointer-events:none}.show-poster img{width:100%;height:100%;object-fit:cover;transition:var(--transition-base)}.show-poster:hover img{transform:scale(1.05)}.show-date{position:absolute;left:0;right:0;bottom:0;color:var(--text-primary);text-align:center;padding:0.75rem;font-size:0.95rem;font-weight:500;white-space:pre-line;z-index:2;letter-spacing:-0.01em}.show-info{padding:10px 0;text-align:center;background:var(--bg-secondary);border-radius:0 0 var(--card-border-radius) var(--card-border-radius);border:1px solid var(--border-subtle);border-top:none}.show-info h3{margin:0;font-size:1rem;font-weight:500;color:var(--text-secondary)}.image-container{position:relative;overflow:hidden;border-radius:var(--card-border-radius);box-shadow:var(--shadow-md);transition:var(--transition-base);border:1px solid var(--border-subtle)}.image-container img{width:200px;height:293px;transition:var(--transition-base);object-fit:cover}.image-container:hover{transform:translateY(-5px);box-shadow:var(--shadow-lg);border-color:var(--border-light)}.image-container:hover img{opacity:0.9}@media (max-width:1024px){.show-grid{grid-template-columns:auto 1fr auto auto}.grid-header:nth-child(5),.grid-header:nth-child(6),.grid-item:nth-of-type(6n-1),.grid-item:nth-of-type(6n){display:none}}@media (max-width:768px){h1{font-size:2rem}.show-grid{grid-template-columns:1fr}.grid-header{display:none}.grid-item{display:grid;grid-template-columns:auto 1fr;gap:1rem;padding:1rem;justify-content:unset;text-align:left}.show-card{width:calc(50% - 15px)}.show-poster{height:auto;aspect-ratio:2 / 3}.show-cards{gap:30px}}</style></head><body><div class="content-wrapper"><div class="main-content"><div class="page-precedente"><a href="javascript:history.back()">Page précédente</a></div><div class="card"><div class="card-header"><h2>Prochaines émissions à écouter</h2></div><div class="card-content"><div class="grid-container show-grid"><div class="grid-header-row"><div class="grid-header"></div><div class="grid-header">Émission</div><div class="grid-header">Saison</div><div class="grid-header">Épisode</div><div class="grid-header">Nom de l'épisode</div><div class="grid-header">Réseau</div></div>{% for emission in prochaines_emissions %}<div class="show-row"><div class="grid-item"><a href="{{ emission.indexer_id|show_link(emission.show_id) }}"><img src="{{ emission.thumbnail }}" class="show-thumbnail" alt="{{ emission.Emission }}" loading="lazy"></a></div><div class="grid-item"><a href="{{ emission.indexer_id|show_link(emission.show_id) }}" style="color:inherit;text-decoration:none">{{ emission.Emission }}</a></div><div class="grid-item">{{ emission.Saison }}</div><div class="grid-item">{{ emission.Episode }}</div><div class="grid-item">{{ emission.Nom_episode }}</div><div class="grid-item">{% if emission.Reseau_de_streaming %}<img src="{{ base_url }}/cache/images/network/{{ emission.Reseau_de_streaming|lower }}.png" alt="{{ emission.Reseau_de_streaming }}" title="{{ emission.Reseau_de_streaming }}" style="max-height:30px;max-width:100%" loading="lazy">{% endif %}</div></div>{% endfor %}</div></div></div>{% if manq %}<div class="card"><div class="card-header"><h2>Épisodes manquants</h2></div><div class="card-content"><table class="missing-episodes-table">{% for emission in manq %}<tr><td><a href="{{ emission.indexer_id|show_link(emission.show_id) }}"><img src="{{ emission.thumbnail }}" class="show-thumbnail" alt="{{ emission.manquant }}" loading="lazy"></a></td><td>{{ emission.manquant }}</td></tr>{% endfor %}</table></div></div>{% endif %}{% if image_urls %}<div class="card"><div class="card-header"><h2>Émissions annulées</h2></div><div class="card-content"><div class="flex-center">{% for i in range(image_urls|length) %}<div class="image-container"><a href="{{ image_links[i] }}"><img src="{{ image_urls[i] }}" alt="Emission annulée" loading="lazy"></a></div>{% endfor %}</div></div></div>{% endif %}{% if next_show %}<div class="card"><div class="card-header"><h2>Épisodes à venir</h2></div><div class="card-content"><div class="show-cards">{% for next_s in next_show %}<div class="show-card"><div class="show-poster"><a href="{{ next_s.indexer_id|show_link(next_s.show_id) }}"><img src="{{ next_s.Image }}" alt="{{ next_s.Emission }}" loading="lazy"><div class="show-date">{{ next_s.Date_diffusion|format_date }}</div></a></div><div class="show-info"><h3>{{ next_s.Emission }}</h3></div></div>{% endfor %}</div></div></div>{% endif %}</div></div><script>document.addEventListener('DOMContentLoaded',function(){document.querySelectorAll('.show-row').forEach(row=>{const link=row.querySelector('a');if(link){const url=link.getAttribute('href');row.style.cursor='pointer';row.addEventListener('click',e=>{if(e.target.tagName!=='A'&&e.target.tagName!=='IMG')window.location.href=url;});}});});</script></body></html>"""

# =========================
# FastAPI app & lifespan
# =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global CONFIG
    # Charger la configuration au démarrage
    CONFIG = load_config()

    # Initialiser le client HTTP
    await http_client_manager.get_client()

    if redis_client:
        try:
            await redis_client.ping()
        except Exception:
            pass

    yield

    # Nettoyage
    await http_client_manager.close()
    if redis_client:
        await redis_client.close()
    await engine.dispose()

app = FastAPI(title="SickGear - FastAPI", lifespan=lifespan)
if CONFIG.enable_gzip:
    app.add_middleware(
        GZipMiddleware,
        minimum_size=CONFIG.gzip_min_size,
        compresslevel=CONFIG.gzip_compress_level
    )

# =========================
# Routes avec configuration dynamique
# =========================
@app.get("/", response_class=HTMLResponse)
async def ecouter(background_tasks: BackgroundTasks):
    repertoire = str(Path(CONFIG.db_path).parent) + "/"

    # Ajouter la tâche de fond pour rafraîchir le cache
    background_tasks.add_task(background_cache.refresh)

    # Récupérer les données
    prochaines_emissions = await data_service.get_shows()
    manq = await data_service.get_missing_episodes()
    ended_shows_data = await data_service.get_ended_shows()

    schedule_data = await get_cached_schedule()
    next_show = []
    if schedule_data:
        seen, now, processed_schedule = set(), datetime.now(), []
        for item in schedule_data:
            name = normalize_show_name(item.get("name"))
            if name in seen:
                continue
            seen.add(name)
            dt_obj = safe_parse_datetime(item.get("time"))
            if dt_obj and dt_obj < now:
                continue
            processed_schedule.append({"name": name, "dt_obj": dt_obj, "dt_raw": item.get("time"), "poster": format_image_url_from_raw(item.get("poster"))})
        processed_schedule.sort(key=lambda x: x["dt_obj"] or datetime.max)
        for p in processed_schedule:
            idx, sid = extract_indexer_showid_from_url(p["poster"])
            next_show.append(UpcomingShow(
                Emission=p["name"],
                Date_diffusion=p["dt_obj"].strftime("%Y-%m-%d %H:%M") if p["dt_obj"] else p["dt_raw"],
                Image=p["poster"],
                indexer_id=idx or "",
                show_id=sid or ""
            ))

    bg_file = await background_cache.get_background(prochaines_emissions, repertoire)
    html_content = template_engine.render(
        template_string,
        prochaines_emissions=[s.model_dump() for s in prochaines_emissions],
        manq=[m.model_dump() for m in manq],
        image_urls=ended_shows_data.urls,
        image_links=ended_shows_data.links,
        next_show=[ns.model_dump() for ns in next_show],
        bg_file=bg_file,
    )
    return HTMLResponse(content=html_content)

@app.post("/open-dolphin", response_class=JSONResponse)
async def open_dolphin(request: Request):
    try:
        data = await request.json()
        path_str = data.get('path')
        if not path_str:
            raise HTTPException(status_code=400, detail="Le champ 'path' est manquant.")
        BIN = shutil.which("dolphin")
        if not BIN:
            raise HTTPException(status_code=500, detail="'dolphin' n'a pas été trouvé sur le serveur.")
        allowed_base = Path(CONFIG.open_dolphin_allowed_base).resolve()
        user_path = Path(path_str).expanduser().resolve()
        if allowed_base not in user_path.parents and user_path != allowed_base:
            raise HTTPException(status_code=403, detail="Le chemin n'est pas autorisé.")
        subprocess.Popen([BIN, str(user_path)])
        return JSONResponse(content={'status': 'success', 'message': f"Dolphin ouvert sur '{user_path}'."})
    except HTTPException as e:
        raise e
    except Exception as e:
        return JSONResponse(content={'status': 'error', 'message': str(e)}, status_code=500)

@app.post("/reload-config", response_class=JSONResponse)
async def reload_config():
    """Recharge la configuration dynamiquement"""
    global CONFIG
    try:
        CONFIG = load_config()
        # Recharger les paramètres dépendants de la configuration
        template_engine._get_template.cache_clear()
        return {"status": "success", "message": "Configuration rechargée"}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
        )

@app.get("/config", response_class=JSONResponse)
async def get_config():
    """Retourne la configuration actuelle"""
    return CONFIG.model_dump()

# =========================
# Main
# =========================
if __name__ == "__main__":
    uvicorn.run(
        app,
        host=CONFIG.host,
        port=CONFIG.port,
        log_level="info"
    )
