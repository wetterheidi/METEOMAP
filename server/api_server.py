#!/usr/bin/env python3
"""
MeteoMap API Server
Serves cached OGIMET SYNOP block texts to the MeteoMap client.

GET /meteomap/ogimet/{block}?hours=3.0
  → returns raw OGIMET text for that WMO block (from cache or fetched on-demand)

GET /health
  → returns status + number of cached blocks
"""
import time
import datetime
import threading
import pathlib

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

# ── Configuration ─────────────────────────────────────────────────────────────

DATA_DIR        = pathlib.Path('/apps/MeteoMap/data/synop')
CACHE_TTL       = 10 * 60   # seconds – serve from file if younger than this
FETCH_TIMEOUT   = 25        # seconds – OGIMET request timeout

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title='MeteoMap API', version='1.0')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['GET'],
    allow_headers=['*'],
)

SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; MeteoMap/1.0)'})

# Per-block locks prevent parallel stampede on the same block
_locks: dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def cache_path(block: int, hours: float) -> pathlib.Path:
    h = round(hours * 2) / 2   # round to nearest 0.5 h (matches collector)
    return DATA_DIR / f'block_{block:02d}_h{h:.1f}.txt'


def is_fresh(path: pathlib.Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL


def fetch_from_ogimet(block: int, hours: float) -> str:
    now = datetime.datetime.utcnow()
    frm = now - datetime.timedelta(hours=hours)
    fmt = lambda d: d.strftime('%Y%m%d%H%M')
    url = (
        f'https://www.ogimet.com/cgi-bin/getsynop'
        f'?block={block:02d}&begin={fmt(frm)}&end={fmt(now)}'
    )
    r = SESSION.get(url, timeout=FETCH_TIMEOUT)
    r.raise_for_status()
    return r.text


def get_lock(key: str) -> threading.Lock:
    with _locks_meta:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get('/meteomap/ogimet/{block}', response_class=PlainTextResponse)
def get_block(
    block: int,
    hours: float = Query(3.0, ge=1.0, le=48.0, description='Hours back from now'),
):
    hours = round(hours * 2) / 2   # normalise
    path  = cache_path(block, hours)

    # Fast path: fresh cache hit
    if is_fresh(path):
        return path.read_text(encoding='utf-8')

    # Slow path: fetch from OGIMET, cache result
    # Use a per-block lock so concurrent requests share one fetch
    lock = get_lock(f'{block}|{hours}')
    with lock:
        if is_fresh(path):                  # re-check inside lock
            return path.read_text(encoding='utf-8')

        # Try to fetch fresh data
        try:
            text = fetch_from_ogimet(block, hours)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
            return text
        except Exception as exc:
            # If we have a stale cache, return it rather than failing
            if path.exists():
                return path.read_text(encoding='utf-8')
            raise HTTPException(502, f'OGIMET fetch failed: {exc}')


@app.get('/health')
def health():
    cached = len(list(DATA_DIR.glob('*.txt'))) if DATA_DIR.exists() else 0
    return {'status': 'ok', 'cached_blocks': cached}
