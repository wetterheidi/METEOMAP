#!/usr/bin/env python3
"""
Shared SYNOP-BUFR message decoder (eccodes), used by bufr_collector.py
and wis2_collector.py.
"""
import math
import datetime
import pathlib
import tempfile

import eccodes

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
    # Prefer the first occurrence: for a key that appears more than once
    # (e.g. airTemperature/windSpeed in template 307096, which most WIS2
    # nodes use), an unqualified codes_get returns the *last* one — usually
    # an empty supplementary-sensor slot — so temp/dewp/wind came out missing.
    for key in (f'#1#{k}', k):
        try:
            v = eccodes.codes_get(h, key)
            return None if v in (MISS, MISSL, 2147483647, -1e+100) else v
        except Exception:
            continue
    return None

def _safe_arr(h, k):
    try:
        return [None if v in (MISS, MISSL, 2147483647, -1e+100) else v
                for v in eccodes.codes_get_array(h, k)]
    except Exception:
        return []

# ── BUFR message decoder ──────────────────────────────────────────────────────

def decode_msg(handle, metar_type: str = 'SYNOP-BUFR', raw_prefix: str = 'BUFR SYNOP') -> dict | None:
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
    temp = round(tk - 273.15, 1) if tk and tk > 183.0 else None   # 183 K = -90 °C
    dewp = round(dk - 273.15, 1) if dk and dk > 183.0 else None

    ws   = _safe(handle, 'windSpeed')
    wd   = _safe(handle, 'windDirection')
    wg   = _safe(handle, 'maximumWindGustSpeed')
    wspd = round(ws * 1.94384, 1) if ws is not None else None
    wgst = round(wg * 1.94384, 1) if wg is not None else None
    wdir = int(wd) if wd is not None else None

    # 307096 carries station pressure as nonCoordinatePressure
    qfe  = _safe(handle, 'stationPressure') or _safe(handle, 'nonCoordinatePressure')
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
        'obsTime': obs_time, 'metarType': metar_type,
        'temp': temp, 'dewp': dewp, 'wdir': wdir, 'wspd': wspd, 'wgst': wgst,
        'altim': qnh, 'slp': round(slp / 100, 1) if slp else None,
        'visib': str(int(vis)) if vis is not None else None,
        'wxString': wx_str, 'skyCondition': sky,
        'rawOb': f'{raw_prefix} WMO{wmo_id} {yr}-{mo:02d}-{dy:02d} {hr:02d}:{mi:02d}Z',
    }


def decode_all_from_bytes(data: bytes, metar_type: str = 'SYNOP-BUFR',
                           raw_prefix: str = 'BUFR SYNOP') -> list[dict]:
    """Decode every BUFR message and subset in a byte buffer that may hold
    one (single-station) or many (bundled, e.g. DWD's national bulletins)
    messages. Unlike decode_msg(), does not drop obs with no wmoId — callers
    that need a classic 5-digit WMO ID decide themselves what to do with those."""
    obs_list: list[dict] = []
    with tempfile.NamedTemporaryFile(suffix='.bufr', delete=False) as tmp:
        tmp.write(data)
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
                                obs = decode_msg(sub, metar_type=metar_type, raw_prefix=raw_prefix)
                                if obs:
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
