#!/usr/bin/env python3
"""
MeteoMap SYNOP Collector
Fetches OGIMET SYNOP block texts for pre-defined regions and caches them as files.
Intended to run every 30 minutes via systemd timer.

Cache files: /apps/MeteoMap/data/synop/block_NN_hH.H.txt
Old files (> MAX_AGE_HOURS) are removed on each run.
"""
import sys
import time
import datetime
import pathlib
import requests

# ── Configuration ─────────────────────────────────────────────────────────────

DATA_DIR = pathlib.Path('/apps/MeteoMap/data/synop')
MAX_AGE_HOURS = 6       # delete cached files older than this
HOURS_BACK    = 3.0     # how far back to request from OGIMET
REQUEST_PAUSE = 2.5     # seconds between OGIMET requests (be polite)
TIMEOUT       = 30      # seconds per HTTP request

# European + N-African blocks to pre-cache on every run
PREFETCH_BLOCKS = [
    1, 2, 3, 4,         # Iceland/Greenland, UK/Ireland, France, Azores/NW Africa
    6, 7,               # Scandinavia, Benelux/Denmark
    10, 11, 12,         # Germany/Austria/Switzerland, Austria-E/Czech/Hungary, Poland
    13, 14,             # Romania/Ukraine, Balkans
    15, 16,             # Iberia, Italy
    17,                 # Turkey/Middle East
    20, 26,             # Russia W, Ukraine/Belarus
]

# ── Helpers ───────────────────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; MeteoMap/1.0)'})


def cache_path(block: int, hours: float) -> pathlib.Path:
    h = round(hours * 2) / 2   # round to nearest 0.5 h (matches client key)
    return DATA_DIR / f'block_{block:02d}_h{h:.1f}.txt'


def fetch_block(block: int, hours: float) -> str:
    now = datetime.datetime.utcnow()
    frm = now - datetime.timedelta(hours=hours)
    fmt = lambda d: d.strftime('%Y%m%d%H%M')
    url = (
        f'https://www.ogimet.com/cgi-bin/getsynop'
        f'?block={block:02d}&begin={fmt(frm)}&end={fmt(now)}'
    )
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def cleanup():
    if not DATA_DIR.exists():
        return
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    removed = sum(
        1 for f in DATA_DIR.glob('block_*.txt')
        if f.stat().st_mtime < cutoff and f.unlink() is None
    )
    if removed:
        print(f'[cleanup] removed {removed} stale file(s)')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cleanup()

    ok = fail = 0
    for block in PREFETCH_BLOCKS:
        try:
            text = fetch_block(block, HOURS_BACK)
            path = cache_path(block, HOURS_BACK)
            path.write_text(text, encoding='utf-8')
            lines = text.count('\n')
            print(f'[block {block:02d}] OK  – {lines} lines → {path.name}')
            ok += 1
        except Exception as exc:
            print(f'[block {block:02d}] FAIL – {exc}', file=sys.stderr)
            fail += 1

        time.sleep(REQUEST_PAUSE)

    print(f'\nDone: {ok} OK, {fail} failed')
    sys.exit(0 if fail == 0 else 1)


if __name__ == '__main__':
    main()
