#!/usr/bin/env python3
"""
MeteoMap SYNOP Collector
Fetches OGIMET SYNOP block texts, parses FM-12, stores observations in SQLite.
Run every 30 minutes via systemd timer.
"""
import sys
import time
import datetime
import json
import math
import pathlib
import re
import requests

from obs_store import open_store

# ── Configuration ─────────────────────────────────────────────────────────────

DATA_DIR      = pathlib.Path('/apps/MeteoMap/data')
HOURS_BACK    = 3.0     # how far back to request from OGIMET
REQUEST_PAUSE = 2.5     # seconds between OGIMET requests
TIMEOUT       = 30

# European + N-African blocks
PREFETCH_BLOCKS = [
    1, 2, 3, 4,
    6, 7,
    10, 11, 12,
    13, 14,
    15, 16,
    17,
    20, 26,
]

SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; MeteoMap/1.0)'})

# ── WMO station DB ─────────────────────────────────────────────────────────────

_WMO_STATIONS: dict[int, dict] | None = None

def load_wmo_stations() -> dict[int, dict]:
    global _WMO_STATIONS
    if _WMO_STATIONS is not None:
        return _WMO_STATIONS
    p = pathlib.Path(__file__).parent.parent / 'wmo_stations.json'
    if not p.exists():
        print(f'WARN: wmo_stations.json nicht gefunden ({p})', file=sys.stderr)
        _WMO_STATIONS = {}
        return _WMO_STATIONS
    with open(p, encoding='utf-8') as f:
        lst = json.load(f)
    _WMO_STATIONS = {int(s['wmo']): s for s in lst if s.get('wmo') is not None}
    print(f'WMO-Stationen geladen: {len(_WMO_STATIONS)}')
    return _WMO_STATIONS

# ── OGIMET fetch ───────────────────────────────────────────────────────────────

def fetch_block(block: int, hours: float) -> str:
    now = datetime.datetime.utcnow()
    frm = now - datetime.timedelta(hours=hours)
    fmt = lambda d: d.strftime('%Y%m%d%H%M')
    url = (f'https://www.ogimet.com/cgi-bin/getsynop'
           f'?block={block:02d}&begin={fmt(frm)}&end={fmt(now)}')
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

# ── FM-12 SYNOP parser ────────────────────────────────────────────────────────

def _vis_from_vv(vv: int) -> int | None:
    if vv <= 50:   return vv * 100
    if vv <= 80:   return (vv - 50) * 1000
    if vv == 81:   return 30
    if vv == 82:   return 50
    if vv == 83:   return 100
    if vv == 84:   return 200
    if vv == 85:   return 500
    if vv == 86:   return 1000
    if vv == 87:   return 2000
    if vv == 88:   return 5000
    if vv == 89:   return 9999
    if vv == 90:   return 50
    if vv == 91:   return 200
    if vv == 92:   return 500
    if vv == 93:   return 1000
    if vv == 94:   return 2000
    if vv == 95:   return 4000
    if vv == 96:   return 10000
    if vv == 97:   return 20000
    if vv == 98:   return 50000
    if vv == 99:   return 9999
    return None

_COVERS = ['SKC', 'FEW', 'FEW', 'SCT', 'SCT', 'BKN', 'BKN', 'BKN', 'OVC', 'OVC']
_COVER_RANK = {'SKC': 0, 'FEW': 1, 'SCT': 2, 'BKN': 3, 'OVC': 4}

def _synop_ww(ww: int) -> str | None:
    if 95 <= ww <= 99: return 'TS'
    if ww == 17:       return 'TS'
    if ww == 94:       return 'SN'
    if ww == 93:       return 'RASN'
    if ww == 92:       return 'RA'
    if ww == 91:       return '-RA'
    if 87 <= ww <= 90: return '+SHSN'
    if 83 <= ww <= 86: return 'SHSN'
    if 80 <= ww <= 82: return 'SHRA'
    if 77 <= ww <= 79: return 'SN'
    if 73 <= ww <= 75: return 'SN'
    if 71 <= ww <= 72: return '-SN'
    if 68 <= ww <= 69: return 'RASN'
    if 66 <= ww <= 67: return 'FZRA'
    if 64 <= ww <= 65: return '+RA'
    if 61 <= ww <= 63: return 'RA'
    if 58 <= ww <= 60: return '-RA'
    if 56 <= ww <= 57: return 'FZDZ'
    if 53 <= ww <= 55: return 'DZ'
    if 50 <= ww <= 52: return '-DZ'
    if 45 <= ww <= 49: return 'FG'
    if 41 <= ww <= 44: return 'BR'
    if ww in (31, 32): return 'DS'
    if ww >= 30:       return 'DU'
    if ww == 10:       return 'BR'
    return None

def _hscode_to_ft(hs: int) -> int | None:
    if hs <= 9:   return hs * 100
    if hs <= 56:  return (hs - 10) * 300 + 1000
    if hs <= 80:  return (hs - 56) * 1500 + 14500
    if hs <= 88:  return (hs - 80) * 6000 + 50500
    return None

def _dedup_sky(layers: list[dict]) -> list[dict]:
    """Merge cloud layers within 500 ft, keep max 3, ceiling logic."""
    valid = [l for l in layers if l.get('cloudBase') is not None]
    valid.sort(key=lambda l: l['cloudBase'])
    deduped: list[dict] = []
    for layer in valid:
        idx = next((j for j, d in enumerate(deduped)
                    if abs(layer['cloudBase'] - d['cloudBase']) < 500), None)
        if idx is None:
            deduped.append(dict(layer))
        elif _COVER_RANK.get(layer['skyCover'], 0) > _COVER_RANK.get(deduped[idx]['skyCover'], 0):
            deduped[idx] = dict(layer)
    ceils  = [l for l in deduped if l['skyCover'] in ('BKN', 'OVC')]
    others = [l for l in deduped if l['skyCover'] not in ('BKN', 'OVC')]
    if ceils:
        below = [l for l in others if l['cloudBase'] < ceils[0]['cloudBase']][-1:]
        result = below + [ceils[0]] + ceils[1:2]
    else:
        result = others[:3]
    result.sort(key=lambda l: l['cloudBase'])
    return result


def parse_synop_line(line: str, stations: dict[int, dict]) -> dict | None:
    """Parse one comma-separated OGIMET SYNOP line into an obs dict."""
    parts = line.split(',')
    if len(parts) < 7:
        return None

    try:
        wmo_id = int(parts[0])
    except ValueError:
        return None

    st = stations.get(wmo_id)
    if not st:
        return None

    try:
        yr, mo, dy = int(parts[1]), int(parts[2]), int(parts[3])
        hr, mn     = int(parts[4]), int(parts[5]) if parts[5].strip() else 0
        obs_time   = int(datetime.datetime(yr, mo, dy, hr, mn,
                                           tzinfo=datetime.timezone.utc).timestamp())
    except (ValueError, OverflowError):
        return None

    raw = ','.join(parts[6:]).strip()

    obs: dict = {
        'icaoId':       f'WMO{wmo_id:05d}',
        'wmoId':        wmo_id,
        'name':         st.get('name', ''),
        'lat':          st.get('lat'),
        'lon':          st.get('lon'),
        'elev':         st.get('elev'),
        'country':      st.get('country'),
        'obsTime':      obs_time,
        'rawOb':        raw,
        'metarType':    'SYNOP',
        'temp':         None, 'dewp':  None, 'wspd':  None, 'wdir':  None,
        'wgst':         None, 'visib': None, 'altim': None, 'slp':   None,
        'wxString':     None, 'skyCondition': [], 'presTend': None,
    }

    if obs['lat'] is None or obs['lon'] is None:
        return None

    # OOXX = automatic/unmanned station report. Its group layout differs from
    # AAXX in ways that aren't reliably documented (e.g. the slot that lines up
    # with the 1SnTTT temperature group actually carries skin/surface
    # temperature, several degrees above the real air temperature — confirmed
    # against DWD BUFR ground truth). Every OOXX-reporting station observed so
    # far also sends a correct AAXX report for the same obsTime, and the store
    # upserts by (source, station, obsTime) — so returning an obs here would
    # silently clobber the good AAXX row instead of just being redundant.
    # Drop OOXX lines entirely rather than risk showing wrong values.
    if raw.startswith('OOXX'):
        return None

    # Wind indicator from AAXX header
    aam = re.search(r'AAXX\s+\d{4}(\d)', raw)
    wind_ind = int(aam.group(1)) if aam else 1  # 0-2 = m/s, 3-4 = kt

    tokens = re.sub(r'AAXX\s+\d{5}', '', raw).split()
    i = 0
    # Skip station number token (IIiii)
    if tokens and re.fullmatch(r'\d{5}', tokens[0]):
        i += 1

    # irixhVV
    if i < len(tokens) and re.fullmatch(r'[0-9/]{5}', tokens[i]):
        vv_raw = tokens[i][3:5]
        if vv_raw.isdigit():
            vis = _vis_from_vv(int(vv_raw))
            obs['visib'] = str(vis) if vis is not None else None
        i += 1

    # Nddff
    if i < len(tokens) and re.fullmatch(r'[0-9/]{5}', tokens[i]):
        t = tokens[i]
        n_char = t[0]
        dd_str, ff_str = t[1:3], t[3:5]
        if dd_str.isdigit():
            dd = int(dd_str)
            obs['wdir'] = 'VRB' if dd == 99 else dd * 10
        if ff_str.isdigit():
            ff = int(ff_str)
            obs['wspd'] = round(ff * 1.944) if wind_ind <= 2 else ff
        if n_char.isdigit():
            n_val = int(n_char)
            cover = _COVERS[n_val] if n_val < len(_COVERS) else None
            if cover and cover != 'SKC':
                obs['skyCondition'] = [{'skyCover': cover, 'cloudBase': None}]
            i += 1

    sec3_start = tokens.index('333') if '333' in tokens else -1
    sec5_start = tokens.index('555') if '555' in tokens else -1
    main_end   = sec3_start if sec3_start >= 0 else len(tokens)

    # Section 1 groups
    for tok in tokens[i:main_end]:
        if not tok or tok == '/////':
            continue

        # 1SnTTT – air temperature
        if re.fullmatch(r'1[01]\d{3}', tok):
            T = int(tok[2:]) / 10
            obs['temp'] = -T if tok[1] == '1' else T

        # 2SnTdTdTd – dew point
        elif re.fullmatch(r'2[01]\d{3}', tok):
            D = int(tok[2:]) / 10
            obs['dewp'] = -D if tok[1] == '1' else D

        # 3PPPP – station pressure (QFE)
        elif re.fullmatch(r'3\d{4}', tok):
            raw3 = int(tok[1:]) / 10
            qfe  = raw3 + 1000 if raw3 < 500 else raw3
            obs['slp'] = qfe
            h = obs['elev'] or 0
            T = (obs['temp'] + 273.15) if obs['temp'] is not None else 288.15
            obs['altim'] = round(qfe * math.pow(1 + 0.0065 * h / T, 5.2561), 1)

        # 4PPPP – MSL pressure (fallback if no group-3)
        elif re.fullmatch(r'4\d{4}', tok):
            if obs['altim'] is None:
                raw4 = int(tok[1:]) / 10
                obs['slp']   = raw4 + 1000 if raw4 < 500 else raw4
                obs['altim'] = obs['slp']

        # 5appp – pressure tendency
        elif re.fullmatch(r'5[0-8]\d{3}', tok):
            a   = int(tok[1])
            ppp = int(tok[2:]) / 10
            obs['presTend'] = -ppp if a >= 5 else ppp

        # 7wwW1W2 – present weather
        elif re.fullmatch(r'7\d{4}', tok):
            ww = int(tok[1:3])
            if ww > 3:
                obs['wxString'] = _synop_ww(ww)

        elif tok == '333':
            break

    # Recalculate QNH with actual temperature
    if obs['slp'] and obs['temp'] is not None and obs['elev']:
        T = obs['temp'] + 273.15
        obs['altim'] = round(obs['slp'] * math.pow(1 + 0.0065 * obs['elev'] / T, 5.2561), 1)

    # Sections 3 + 5 (gusts, detailed cloud)
    extra_tokens: list[str] = []
    if sec3_start >= 0:
        end = sec5_start if sec5_start > sec3_start else len(tokens)
        extra_tokens += tokens[sec3_start + 1:end]
    if sec5_start >= 0:
        extra_tokens += tokens[sec5_start + 1:]

    wspd10 = None
    for tok in extra_tokens:
        if not tok or len(tok) != 5:
            continue
        # 911ff – gust
        if re.fullmatch(r'911\d{2}', tok):
            ff = int(tok[3:])
            if 0 < ff <= 51:
                obs['wgst'] = round(ff * 1.944) if wind_ind <= 2 else ff
        # 910ff – 10-min mean (gust fallback)
        elif re.fullmatch(r'910\d{2}', tok) and obs['wgst'] is None:
            ff = int(tok[3:])
            if 0 < ff <= 51:
                wspd10 = round(ff * 1.944) if wind_ind <= 2 else ff
        # 8NsCshs – cloud layer
        elif re.fullmatch(r'8[0-9][/0-9]\d{2}', tok):
            ns  = int(tok[1])
            hss = int(tok[3:5])
            if ns > 0:
                h_ft   = _hscode_to_ft(hss)
                cover  = 'FEW' if ns <= 2 else 'SCT' if ns <= 4 else 'BKN' if ns <= 7 else 'OVC'
                if h_ft is not None:
                    obs['skyCondition'].append({'skyCover': cover, 'cloudBase': h_ft})

    if obs['wgst'] is None and wspd10 and obs['wspd'] and wspd10 > obs['wspd']:
        obs['wgst'] = wspd10

    obs['skyCondition'] = _dedup_sky(obs['skyCondition'])
    return obs


def parse_synop_block(text: str, stations: dict[int, dict]) -> list[dict]:
    result = []
    for line in text.splitlines():
        line = line.strip().rstrip('=')
        if not line or line.startswith('#'):
            continue
        try:
            obs = parse_synop_line(line, stations)
            if obs:
                result.append(obs)
        except Exception:
            pass
    return result

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stations = load_wmo_stations()
    if not stations:
        print('Keine Stationen – Abbruch.', file=sys.stderr)
        sys.exit(1)

    all_obs: list[dict] = []
    ok = fail = 0

    for block in PREFETCH_BLOCKS:
        try:
            text = fetch_block(block, HOURS_BACK)
            obs_list = parse_synop_block(text, stations)
            all_obs.extend(obs_list)
            print(f'[block {block:02d}] OK – {len(obs_list)} Stationen')
            ok += 1
        except Exception as exc:
            print(f'[block {block:02d}] FAIL – {exc}', file=sys.stderr)
            fail += 1
        time.sleep(REQUEST_PAUSE)

    print(f'\nFetch: {ok} OK, {fail} failed – {len(all_obs)} Beobachtungen gesamt')

    written = 0
    with open_store() as db:
        for obs in all_obs:
            skey = f'WMO{obs["wmoId"]:05d}'
            try:
                db.upsert('synop-ogimet', skey, obs['obsTime'],
                          obs['lat'], obs['lon'], obs)
                written += 1
            except Exception as exc:
                print(f'  SQLite upsert FAIL {skey}: {exc}', file=sys.stderr)
        removed = db.cleanup()
    print(f'SQLite: {written} Zeilen geschrieben, {removed} alte gelöscht')
    sys.exit(0 if fail == 0 else 1)


if __name__ == '__main__':
    main()
