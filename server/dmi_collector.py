#!/usr/bin/env python3
"""
MeteoMap DMI Collector
Fetches near-real-time observations from the DMI Open Data API for Greenland stations.
Run every 15 minutes via cron (same as bufr_collector).

Output: /apps/MeteoMap/data/dmi_latest.json
"""
import sys
import json
import math
import datetime
import pathlib

import requests

# ── Configuration ─────────────────────────────────────────────────────────────

DATA_DIR = pathlib.Path('/apps/MeteoMap/data')
OUTPUT   = DATA_DIR / 'dmi_latest.json'
BASE_URL = 'https://opendataapi.dmi.dk/v2/metObs/collections'

# Greenland bounding box: minLon, minLat, maxLon, maxLat
GL_BBOX  = '-73,-60,-11,85'

PARAMS = [
    'temp_dry', 'temp_dew', 'wind_speed', 'wind_dir', 'wind_max',
    'pressure_at_sea', 'pressure', 'visib_mean_last10min',
    'cloud_cover', 'cloud_height', 'weather', 'humidity',
]

# WMO cloud cover (oktas 0–8) → METAR sky cover code
OKTA_MAP = {0: 'SKC', 1: 'FEW', 2: 'FEW', 3: 'SCT', 4: 'SCT',
            5: 'BKN', 6: 'BKN', 7: 'BKN', 8: 'OVC'}

# DMI weather code → METAR wx string (subset of WMO ww codes)
WX_MAP = {
    45: 'FG', 47: 'FG', 48: 'FZFG', 49: 'FZFG',
    51: '-DZ', 53: 'DZ', 55: '+DZ',
    56: 'FZDZ', 57: '+FZDZ',
    61: '-RA', 63: 'RA', 65: '+RA',
    66: 'FZRA', 67: '+FZRA',
    71: '-SN', 73: 'SN', 75: '+SN',
    77: 'SG',
    80: '-SHRA', 81: 'SHRA', 82: '+SHRA',
    85: '-SHSN', 86: '+SHSN',
    95: 'TS', 96: 'TSRA', 99: 'TSGR',
}

SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; MeteoMap/1.0)'})

# ── API helpers ───────────────────────────────────────────────────────────────

def fetch_stations() -> dict[str, dict]:
    """Return dict of stationId → {name, lat, lon, elev, wmoId}."""
    r = SESSION.get(
        f'{BASE_URL}/station/items?bbox={GL_BBOX}&limit=200',
        timeout=20,
    )
    r.raise_for_status()
    stations = {}
    for f in r.json().get('features', []):
        p  = f['properties']
        sid = p.get('stationId', '')
        if sid in stations:
            continue
        coords = f.get('geometry', {}).get('coordinates', [None, None, None])
        wmo_raw = p.get('wmoStationId') or sid
        try:
            wmo_id = int(str(wmo_raw).replace(' ', ''))
        except Exception:
            wmo_id = None
        stations[sid] = {
            'name': p.get('name', '').strip(),
            'lat':  round(float(coords[1]), 4) if coords[1] is not None else None,
            'lon':  round(float(coords[0]), 4) if coords[0] is not None else None,
            'elev': int(coords[2]) if len(coords) > 2 and coords[2] is not None else None,
            'wmoId': wmo_id,
        }
    return stations


def fetch_observations(period: str = 'latest-hour') -> dict[str, dict]:
    """Return dict of stationId → {parameterId: (value, observed_dt)}."""
    # Build URL manually — requests would percent-encode commas in parameterId/sortorder
    qs = (
        f'bbox={GL_BBOX}'
        f'&parameterId={",".join(PARAMS)}'
        f'&period={period}'
        f'&sortorder=observed,DESC'
        f'&limit=10000'
    )
    r = SESSION.get(f'{BASE_URL}/observation/items?{qs}', timeout=30)
    r.raise_for_status()
    result: dict[str, dict] = {}
    for f in r.json().get('features', []):
        p   = f['properties']
        sid = p.get('stationId', '')
        pid = p.get('parameterId', '')
        val = p.get('value')
        obs = p.get('observed', '')
        if val is None or not obs:
            continue
        try:
            t = datetime.datetime.fromisoformat(obs.replace('Z', '+00:00'))
        except Exception:
            continue
        if sid not in result:
            result[sid] = {}
        # keep most recent value per parameter
        if pid not in result[sid] or t > result[sid][pid][1]:
            result[sid][pid] = (val, t)
    return result


# ── Observation builder ───────────────────────────────────────────────────────

def build_obs(sid: str, station: dict, params: dict) -> dict | None:
    def val(key):
        entry = params.get(key)
        return entry[0] if entry else None

    def obs_time(key):
        entry = params.get(key)
        return entry[1] if entry else None

    lat  = station.get('lat')
    lon  = station.get('lon')
    if lat is None or lon is None:
        return None

    # Use temp_dry timestamp as reference obsTime
    t_ref = obs_time('temp_dry') or obs_time('wind_speed') or obs_time('pressure_at_sea')
    if t_ref is None:
        return None
    unix_t = int(t_ref.timestamp())

    temp = val('temp_dry')
    dewp = val('temp_dew')
    ws   = val('wind_speed')
    wg   = val('wind_max')
    wd   = val('wind_dir')
    slp  = val('pressure_at_sea')
    qfe  = val('pressure')
    vis  = val('visib_mean_last10min')
    cc   = val('cloud_cover')
    ch   = val('cloud_height')
    ww   = val('weather')

    wspd = round(float(ws) * 1.94384, 1) if ws is not None else None
    wgst = round(float(wg) * 1.94384, 1) if wg is not None else None
    wdir = int(wd) if wd is not None else None
    altim = round(float(slp), 1) if slp is not None else None

    # QNH from station pressure if no MSL pressure
    if altim is None and qfe is not None and station.get('elev') and temp is not None:
        tk = float(temp) + 273.15
        altim = round(float(qfe) * math.pow(1 + 0.0065 * station['elev'] / tk, 5.2561), 1)

    # Sky condition from single cloud layer
    sky = []
    if cc is not None and cc >= 0:
        cover_str = OKTA_MAP.get(int(round(cc)), 'FEW')
        if cover_str != 'SKC':
            base_m  = float(ch) if ch is not None else 0
            base_ft = max(0, int(base_m * 3.28084))
            sky = [{'skyCover': cover_str, 'cloudBase': base_ft}]

    wx_str = None
    if ww is not None:
        wx_str = WX_MAP.get(int(ww))

    wmo_id = station.get('wmoId')
    name   = station.get('name', '')

    return {
        'icaoId':       f'DMI{sid}',
        'wmoId':        wmo_id,
        'name':         name,
        'lat':          lat,
        'lon':          lon,
        'elev':         station.get('elev'),
        'obsTime':      unix_t,
        'metarType':    'SYNOP-DMI',
        'temp':         round(float(temp), 1) if temp is not None else None,
        'dewp':         round(float(dewp), 1) if dewp is not None else None,
        'wdir':         wdir,
        'wspd':         wspd,
        'wgst':         wgst,
        'altim':        altim,
        'slp':          altim,
        'visib':        str(int(float(vis))) if vis is not None else None,
        'wxString':     wx_str,
        'skyCondition': sky,
        'rawOb':        f'DMI {sid} {name} {t_ref.strftime("%Y-%m-%d %H:%MZ")}',
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print('DMI: Stationsmetadaten abrufen …')
    try:
        stations = fetch_stations()
    except Exception as exc:
        print(f'FAIL Stationen: {exc}', file=sys.stderr)
        sys.exit(1)
    print(f'  {len(stations)} Stationen gefunden')

    print('DMI: Beobachtungen abrufen …')
    try:
        obs_raw = fetch_observations('latest-hour')
    except Exception as exc:
        print(f'FAIL Beobachtungen: {exc}', file=sys.stderr)
        sys.exit(1)
    print(f'  {len(obs_raw)} Stationen mit Daten')

    now    = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(hours=2)

    result = []
    for sid, params in obs_raw.items():
        station = stations.get(sid)
        if not station:
            continue
        obs = build_obs(sid, station, params)
        if not obs:
            continue
        # drop stale observations
        if obs['obsTime'] < cutoff.timestamp():
            continue
        result.append(obs)

    OUTPUT.write_text(json.dumps(result), encoding='utf-8')
    print(f'Fertig: {len(result)} Stationen → {OUTPUT}')


if __name__ == '__main__':
    main()
