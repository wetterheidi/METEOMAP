#!/usr/bin/env python3
"""
MeteoMap BUFR Collector
Downloads DWD SYNOP BUFR files, decodes all stations, saves to SQLite.
Run every 10 minutes via systemd timer; only new files (not yet seen) are decoded.
"""
import sys
import json
import datetime
import pathlib
import tempfile
import re
import urllib.parse

import requests
import eccodes

from obs_store import open_store
from bufr_decode import decode_msg

# ── Configuration ─────────────────────────────────────────────────────────────

DATA_DIR   = pathlib.Path('/apps/MeteoMap/data')
OUTPUT     = DATA_DIR / 'bufr_latest.json'   # legacy JSON, kept for old /meteomap/bufr route
SEEN_FILE  = DATA_DIR / 'bufr_seen_files.json'
DWD_BASE   = 'https://opendata.dwd.de/weather/weather_reports/synoptic/international/'
MAX_FILES  = 10      # max new files to decode per run (at 10-min takt: ~2 new files expected)
MAX_AGE_H  = 1.5     # only consider files younger than this

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (compatible; MeteoMap/1.0)',
    'Referer':    'https://opendata.dwd.de/',
})

# ── DWD file listing ──────────────────────────────────────────────────────────

def list_dwd_files() -> list[tuple]:
    r = SESSION.get(DWD_BASE, timeout=15)
    r.raise_for_status()
    entries = []
    for href in re.findall(r'href="([^"]+)"', r.text):
        fname = urllib.parse.unquote(href.split('/')[-1])
        m = re.search(r'EDZW_(\d{14})_.*synop_bufr', fname)
        if not m:
            continue
        ts_str = m.group(1)
        try:
            fts = datetime.datetime(
                int(ts_str[:4]), int(ts_str[4:6]), int(ts_str[6:8]),
                int(ts_str[8:10]), int(ts_str[10:12]), int(ts_str[12:14]),
                tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        sm   = re.search(re.escape(fname) + r'[^\d]*(\d{3,7})', r.text)
        size = int(sm.group(1)) if sm else 0
        entries.append((fname, fts, size))
    return entries

# ── BUFR file decode ──────────────────────────────────────────────────────────

def decode_bufr_url(url: str) -> list[dict]:
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    obs_list = []
    with tempfile.NamedTemporaryFile(suffix='.bufr', delete=False) as tmp:
        tmp.write(r.content)
        tmp_path = pathlib.Path(tmp.name)
    try:
        with open(tmp_path, 'rb') as f:
            while True:
                try:
                    handle = eccodes.codes_bufr_new_from_file(f)
                    if handle is None:
                        break
                    try:
                        n_sub = int(eccodes.codes_get(handle, 'numberOfSubsets') or 1)
                    except Exception:
                        n_sub = 1
                    for si in range(1, n_sub + 1):
                        try:
                            if n_sub > 1:
                                sub = eccodes.codes_clone(handle)
                                eccodes.codes_set(sub, 'unpack', 1)
                                eccodes.codes_set(sub, 'extractSubset', si)
                                eccodes.codes_set(sub, 'doExtractSubsets', 1)
                            else:
                                sub = handle
                            try:
                                obs = decode_msg(sub)
                                if obs and obs.get('wmoId'):
                                    obs_list.append(obs)
                            finally:
                                if n_sub > 1:
                                    eccodes.codes_release(sub)
                        except Exception:
                            pass
                    eccodes.codes_release(handle)
                except Exception:
                    break
    finally:
        tmp_path.unlink(missing_ok=True)
    return obs_list

# ── Main ──────────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding='utf-8')))
    except Exception:
        return set()


def save_seen(seen: set[str], all_entries: list[tuple]) -> None:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=MAX_AGE_H + 0.5)
    still_recent = {fname for fname, fts, _ in all_entries if fts >= cutoff}
    SEEN_FILE.write_text(
        json.dumps(sorted(seen & still_recent), ensure_ascii=False),
        encoding='utf-8',
    )


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print('DWD BUFR: Verzeichnis abrufen …')
    try:
        entries = list_dwd_files()
    except Exception as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        sys.exit(1)

    if not entries:
        print('Keine BUFR-Dateien gefunden.', file=sys.stderr)
        sys.exit(1)

    now      = datetime.datetime.now(datetime.timezone.utc)
    cutoff   = now - datetime.timedelta(hours=MAX_AGE_H)
    recent   = [(n, t, sz) for n, t, sz in entries if t >= cutoff]
    recent.sort(key=lambda x: x[1], reverse=True)   # neueste zuerst

    seen     = load_seen()
    new_only = [(n, t, sz) for n, t, sz in recent if n not in seen][:MAX_FILES]

    newest   = max((t for _, t, _ in entries), default=now)
    age_h    = (now - newest).total_seconds() / 3600
    print(f'{len(entries)} Dateien gesamt, {len(recent)} aktuell, '
          f'{len(new_only)} neu – neueste {newest.strftime("%H:%MZ")} ({age_h:.1f}h)')

    if not new_only:
        print('Keine neuen Dateien – fertig.')
        sys.exit(0)

    obs_cutoff = now - datetime.timedelta(hours=4)
    all_obs: list[dict] = []
    seen_legacy: dict[int, dict] = {}   # newest-per-station for legacy JSON

    for fname, _, _ in new_only:
        url = DWD_BASE + urllib.parse.quote(fname, safe='_-.,~')
        try:
            obs_list = decode_bufr_url(url)
            new = 0
            for obs in obs_list:
                t = obs.get('obsTime')
                if not t:
                    continue
                if datetime.datetime.fromtimestamp(t, datetime.timezone.utc) < obs_cutoff:
                    continue
                all_obs.append(obs)
                k = obs['wmoId']
                if k not in seen_legacy or t > (seen_legacy[k].get('obsTime') or 0):
                    seen_legacy[k] = obs
                new += 1
            print(f'  {fname}: {len(obs_list)} Stationen, {new} verwertbar (gesamt {len(all_obs)})')
        except Exception as exc:
            print(f'  {fname}: FAIL {exc}', file=sys.stderr)

    # ── Seen-Files aktualisieren ──────────────────────────────────────────────
    seen.update(fname for fname, _, _ in new_only)
    save_seen(seen, recent)

    # ── SQLite (neue Architektur) ─────────────────────────────────────────────
    written = 0
    with open_store() as db:
        for obs in all_obs:
            skey = f'WMO{obs["wmoId"]:05d}'
            try:
                db.upsert('synop-bufr', skey, obs['obsTime'],
                          obs['lat'], obs['lon'], obs)
                written += 1
            except Exception as exc:
                print(f'  SQLite upsert FAIL {skey}: {exc}', file=sys.stderr)
        removed = db.cleanup()
    print(f'SQLite: {written} Zeilen geschrieben, {removed} alte gelöscht')

    # ── Legacy JSON (für /meteomap/bufr bis Frontend-Migration) ──────────────
    result = list(seen_legacy.values())
    OUTPUT.write_text(json.dumps(result), encoding='utf-8')
    print(f'Fertig: {len(result)} Stationen (Legacy-JSON) → {OUTPUT}')

if __name__ == '__main__':
    main()
