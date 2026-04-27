#!/usr/bin/env python3
"""
MeteoMap API Server

GET /meteomap/ogimet/{block}?hours=3.0  → OGIMET SYNOP text (cached per block)
GET /meteomap/bufr?bbox=s,w,n,e         → DWD BUFR stations (bbox-filtered JSON)
GET /health                             → status
"""
import json
import time
import datetime
import threading
import pathlib

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

# ── Configuration ─────────────────────────────────────────────────────────────

REPO_ROOT       = pathlib.Path(__file__).parent.parent
DATA_DIR        = pathlib.Path('/apps/MeteoMap/data/synop')
BUFR_FILE       = pathlib.Path('/apps/MeteoMap/data/bufr_latest.json')
CACHE_TTL       = 35 * 60   # seconds – collector runs every 30 min, so 35 min gives overlap
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

@app.get('/')
def serve_index():
    return FileResponse(REPO_ROOT / 'meteomap_52.html', media_type='text/html')


@app.get('/wmo_stations.json')
def serve_wmo_stations():
    return FileResponse(REPO_ROOT / 'wmo_stations.json', media_type='application/json')


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


@app.get('/meteomap/bufr')
def get_bufr(bbox: str = Query(..., description='s,w,n,e')):
    if not BUFR_FILE.exists():
        raise HTTPException(503, 'BUFR-Daten noch nicht verfügbar – Collector läuft noch nicht')
    try:
        s, w, n, e = [float(x) for x in bbox.split(',')]
    except Exception:
        raise HTTPException(400, 'bbox muss s,w,n,e sein')
    all_stations = json.loads(BUFR_FILE.read_text(encoding='utf-8'))
    filtered = [st for st in all_stations
                if s <= st['lat'] <= n and w <= st['lon'] <= e]
    return filtered


@app.get('/health')
def health():
    cached     = len(list(DATA_DIR.glob('*.txt'))) if DATA_DIR.exists() else 0
    bufr_ready = BUFR_FILE.exists()
    return {'status': 'ok', 'cached_blocks': cached, 'bufr_ready': bufr_ready}
