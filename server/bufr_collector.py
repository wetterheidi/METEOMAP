#!/usr/bin/env python3
"""
MeteoMap BUFR Collector
Downloads DWD SYNOP BUFR files, decodes all stations, saves to SQLite.
Run every 10 minutes via systemd timer; only new files (not yet seen) are decoded.
"""
import sys
import json
import math
import datetime
import pathlib
import tempfile
import re
import urllib.parse

import requests
import eccodes

from obs_store import open_store

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

MISS  = eccodes.CODES_MISSING_DOUBLE
MISSL = eccodes.CODES_MISSING_LONG

COVER_RANK = {'SKC': 0, 'FEW': 1, 'SCT': 2, 'BKN': 3, 'OVC': 4}
CMAP       = {0: 'SKC', 1: 'FEW', 2: 'FEW', 3: 'FEW',
              4: 'SCT', 5: 'SCT', 6: 'BKN', 7: 'BKN', 8: 'OVC'}

TAB4677 = {17: 'TS', 91: '-RA', 92: 'RA', 93: 'RASN', 94: 'SN',
           95: 'TS', 96: 'TS', 97: 'TS', 98: 'TS', 99: 'TS'}
TAB4677_THRESH = [(87, '+SHSN'), (80, 'SHRA'), (73, 'SN'), (71, '-SN'),
                  (68, 'RASN'), (66, 'FZRA'), (61, 'RA'), (58, '-RA'),
                  (56, 'FZDZ'), (50, 'DZ'), (45, 'FG'), (40, 'BR'), (30, 'DS')]
TAB4678 = {100: None, 101: None, 102: 'BR', 103: 'FG', 104: 'FG',
           105: 'DZ', 106: 'RA', 107: 'SN', 108: 'SHRA', 109: 'TS', 110: 'BLSN',
           118: 'FZRA', 119: 'FZDZ', 122: 'DZ', 130: '-RA', 131: 'RA', 132: '+RA',
           135: 'RASN', 140: '-SN', 141: 'SN', 142: '+SN',
           160: '-SHRA', 161: 'SHRA', 162: '+SHRA',
           170: 'TS', 171: 'TSRA', 172: 'TSSN',
           508: None, 509: None}

# ── eccodes helpers ───────────────────────────────────────────────────────────

def _safe(h, k):
    try:
        v = eccodes.codes_get(h, k)
        return None if v in (MISS, MISSL, 2147483647, -1e+100) else v
    except Exception:
        return None

def _safe_arr(h, k):
    try:
        return [None if v in (MISS, MISSL, 2147483647, -1e+100) else v
                for v in eccodes.codes_get_array(h, k)]
    except Exception:
        return []

# ── BUFR message decoder ──────────────────────────────────────────────────────

def decode_msg(handle) -> dict | None:
    eccodes.codes_set(handle, 'unpack', 1)
    lat = _safe(handle, 'latitude')
    lon = _safe(handle, 'longitude')
    if lat is None or lon is None:
        return None

    blk    = _safe(handle, 'blockNumber')
    num    = _safe(handle, 'stationNumber')
    wmo_id = (int(blk) * 1000 + int(num)) if (blk and num) else None

    elev = (_safe(handle, 'heightOfStation') or
            _safe(handle, 'heightOfStationGroundAboveMeanSeaLevel'))

    yr, mo, dy = _safe(handle, 'year'), _safe(handle, 'month'), _safe(handle, 'day')
    hr, mi     = _safe(handle, 'hour'), (_safe(handle, 'minute') or 0)
    obs_time   = None
    if all(v is not None for v in [yr, mo, dy, hr]):
        try:
            obs_time = int(datetime.datetime(
                int(yr), int(mo), int(dy), int(hr), int(mi),
                tzinfo=datetime.timezone.utc).timestamp())
        except Exception:
            pass

    tk   = _safe(handle, 'airTemperature')
    dk   = _safe(handle, 'dewpointTemperature')
    temp = round(tk - 273.15, 1) if tk else None
    dewp = round(dk - 273.15, 1) if dk else None

    ws   = _safe(handle, 'windSpeed')
    wd   = _safe(handle, 'windDirection')
    wg   = _safe(handle, 'maximumWindGustSpeed')
    wspd = round(ws * 1.94384, 1) if ws is not None else None
    wgst = round(wg * 1.94384, 1) if wg is not None else None
    wdir = int(wd) if wd is not None else None

    qfe  = _safe(handle, 'stationPressure')
    slp  = _safe(handle, 'pressureReducedToMeanSeaLevel')
    qnh  = None
    if qfe and elev is not None and tk:
        qnh = round(qfe / 100 * math.pow(1 + 0.0065 * elev / tk, 5.2561), 1)
    elif slp:
        qnh = round(slp / 100, 1)

    vis    = _safe(handle, 'horizontalVisibility')
    ww     = _safe(handle, 'presentWeather')
    wx_str = None
    if ww is not None:
        ww_int = int(ww)
        if ww_int in TAB4677:
            wx_str = TAB4677[ww_int]
        elif ww_int < 91:
            wx_str = next((v for t, v in TAB4677_THRESH if ww_int >= t), None)
        elif ww_int in TAB4678:
            wx_str = TAB4678[ww_int]

    # Cloud layers
    c_amt  = _safe_arr(handle, 'cloudAmount')
    c_base = _safe_arr(handle, 'heightOfBaseOfCloud')
    c_vsig = _safe_arr(handle, 'verticalSignificanceSurfaceObservations')
    n      = min(len(c_amt), len(c_base))
    raw    = []
    for i in range(n):
        a, h = c_amt[i], c_base[i]
        if a is None or h is None:
            continue
        vs = c_vsig[i] if i < len(c_vsig) else None
        if vs == 7:
            continue
        base_ft = int(h * 3.28084)
        if base_ft < 0:
            continue
        raw.append((base_ft, CMAP.get(int(a), 'FEW'), vs))

    sig20  = [(b, c) for b, c, vs in raw if vs == 20]
    pool   = sig20 if sig20 else [(b, c) for b, c, vs in raw]
    pool.sort(key=lambda x: x[0])
    merged, used = [], []
    for base_ft, cover in pool:
        near = next((j for j, b in enumerate(used) if abs(base_ft - b) < 500), None)
        if near is None:
            merged.append({'skyCover': cover, 'cloudBase': base_ft})
            used.append(base_ft)
        elif COVER_RANK.get(cover, 0) > COVER_RANK.get(merged[near]['skyCover'], 0):
            merged[near] = {'skyCover': cover, 'cloudBase': base_ft}

    ceil_layers  = [l for l in merged if l['skyCover'] in ('BKN', 'OVC')]
    other_layers = [l for l in merged if l['skyCover'] not in ('BKN', 'OVC')]
    sky = []
    if ceil_layers:
        sky.append(ceil_layers[0])
        below = [l for l in other_layers if l['cloudBase'] < ceil_layers[0]['cloudBase']]
        sky   = sorted(below, key=lambda x: x['cloudBase'])[-1:] + sky
        if len(sky) < 3 and len(ceil_layers) > 1:
            sky.append(ceil_layers[1])
    else:
        sky = other_layers[:3]
    sky.sort(key=lambda x: x['cloudBase'])

    name = _safe(handle, 'stationOrSiteName') or ''
    return {
        'icaoId': f'WMO{str(wmo_id).zfill(5)}' if wmo_id else None,
        'wmoId': wmo_id, 'name': name.strip(),
        'lat': round(float(lat), 4), 'lon': round(float(lon), 4),
        'elev': int(elev) if elev is not None else None,
        'obsTime': obs_time, 'metarType': 'SYNOP-BUFR',
        'temp': temp, 'dewp': dewp, 'wdir': wdir, 'wspd': wspd, 'wgst': wgst,
        'altim': qnh, 'slp': round(slp / 100, 1) if slp else None,
        'visib': str(int(vis)) if vis is not None else None,
        'wxString': wx_str, 'skyCondition': sky,
        'rawOb': f'BUFR SYNOP WMO{wmo_id} {yr}-{mo:02d}-{dy:02d} {hr:02d}:{mi:02d}Z',
    }

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
