#!/usr/bin/env python3
"""
MeteoMap – Lokaler Server mit CORS-Proxy
Starten: python server.py
Öffnen:  http://localhost:8765/meteomap.html
"""
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
import urllib.request, urllib.parse, sys, os, json, io, math, datetime, re

PORT = 8765

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == '/favicon.ico':
            self.send_response(204); self.end_headers(); return

        if parsed.path == '/dwd-browse':
            # Fetch a DWD OpenData directory listing and return as text
            qs   = urllib.parse.parse_qs(parsed.query)
            path = qs.get('path', ['/weather/weather_reports/synoptic/international/'])[0]
            url  = f'https://opendata.dwd.de{path}'
            req  = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
                'Accept':     'text/html,*/*',
                'Referer':    'https://opendata.dwd.de/',
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    data  = r.read()
                    ctype = r.headers.get('Content-Type', 'text/plain')
                    scode = r.status
                print(f'  DWD-browse: {scode} {ctype[:30]} {len(data)}B  {url}')
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                print(f'  DWD-browse ERROR: {e}  {url}')
                self.send_error(502, str(e))
            return

        if parsed.path == '/ogimet':
            # Proxy for OGIMET SYNOP data
            qs  = urllib.parse.parse_qs(parsed.query)
            url = qs.get('url', [None])[0]
            if not url:
                self.send_error(400, "url parameter missing"); return
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
                'Accept':     'text/plain,*/*',
                'Referer':    'https://www.ogimet.com/',
            })
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                print(f'  OGIMET error: {e}')
                self.send_error(502, str(e))
            return

        if parsed.path == '/dwd-synop':
            # DWD international BUFR SYNOP decoder
            # Requires: pip install eccodes
            import io as _io, math as _math, datetime as _dt, re as _re, json as _json
            try:
                import eccodes
            except ImportError:
                self.send_error(503, 'eccodes not installed: pip install eccodes')
                return
            qs    = urllib.parse.parse_qs(parsed.query)
            hours = float(qs.get('hours', ['2'])[0])
            bbox_s= qs.get('bbox', [''])[0]
            bbox  = [float(x) for x in bbox_s.split(',')] if bbox_s else None
            MISS  = eccodes.CODES_MISSING_DOUBLE
            MISSL = eccodes.CODES_MISSING_LONG
            DWD_H = {'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
                     'Referer':'https://opendata.dwd.de/'}
            DWD_BASE = 'https://opendata.dwd.de/weather/weather_reports/synoptic/international/'

            def _safe(h, k):
                try:
                    v = eccodes.codes_get(h, k)
                    return None if v in (MISS, MISSL, 2147483647, -1e+100) else v
                except: return None

            def _safe_arr(h, k):
                try:
                    return [None if v in (MISS,MISSL,2147483647,-1e+100) else v
                            for v in eccodes.codes_get_array(h, k)]
                except: return []

            def decode_msg(handle):
                eccodes.codes_set(handle, 'unpack', 1)
                lat = _safe(handle,'latitude'); lon = _safe(handle,'longitude')
                if lat is None or lon is None: return None
                blk = _safe(handle,'blockNumber'); num = _safe(handle,'stationNumber')
                wmo_id = (int(blk)*1000+int(num)) if (blk and num) else None
                elev = _safe(handle,'heightOfStation') or                        _safe(handle,'heightOfStationGroundAboveMeanSeaLevel')
                yr,mo,dy = _safe(handle,'year'),_safe(handle,'month'),_safe(handle,'day')
                hr,mi    = _safe(handle,'hour'),(_safe(handle,'minute') or 0)
                obs_time = None
                if all(v is not None for v in [yr,mo,dy,hr]):
                    try:
                        obs_time = int(_dt.datetime(int(yr),int(mo),int(dy),
                            int(hr),int(mi),tzinfo=_dt.timezone.utc).timestamp())
                    except: pass
                tk = _safe(handle,'airTemperature'); dk = _safe(handle,'dewpointTemperature')
                temp = round(tk-273.15,1) if tk else None
                dewp = round(dk-273.15,1) if dk else None
                ws = _safe(handle,'windSpeed'); wd = _safe(handle,'windDirection')
                wg = _safe(handle,'maximumWindGustSpeed')
                wspd = round(ws*1.94384,1) if ws is not None else None
                wgst = round(wg*1.94384,1) if wg is not None else None
                wdir = int(wd) if wd is not None else None
                qfe = _safe(handle,'stationPressure')
                slp = _safe(handle,'pressureReducedToMeanSeaLevel')
                qnh = None
                if qfe and elev is not None and tk:
                    qnh = round(qfe/100*_math.pow(1+0.0065*elev/tk,5.2561),1)
                elif slp: qnh = round(slp/100,1)
                vis = _safe(handle,'horizontalVisibility')
                ww  = _safe(handle,'presentWeather')
                wx_str = None
                if ww is not None:
                    ww_int = int(ww)
                    if ww_int > 99:
                        # WMO Table 4678 (automatische Stationen), Codes 100-191
                        print(f'  BUFR ww={ww_int} (Tab.4678 auto)')
                    # Tabelle 4677 (manuelle Stationen, Codes 0-99)
                    # 95-99: aktives Gewitter; 17: TS ohne Niederschlag an Station
                    # 91-94: Niederschlag NACH Gewitter – kein aktives TS!
                    tab4677 = {17:'TS',91:'-RA',92:'RA',93:'RASN',94:'SN',
                               95:'TS',96:'TS',97:'TS',98:'TS',99:'TS'}
                    tab4677_thresh = [(87,'+SHSN'),(80,'SHRA'),(73,'SN'),(71,'-SN'),
                                      (68,'RASN'),(66,'FZRA'),(61,'RA'),(58,'-RA'),
                                      (56,'FZDZ'),(50,'DZ'),(45,'FG'),(40,'BR'),(30,'DS')]
                    # Tabelle 4678 (automatische Stationen, Codes 100-191)
                    # 508/509 = kein Sensor / nicht verfügbar → None (wurden früher als >=95 → TS gemappt!)
                    tab4678 = {100:None,101:None,102:'BR',103:'FG',104:'FG',
                               105:'DZ',106:'RA',107:'SN',108:'SHRA',109:'TS',110:'BLSN',
                               118:'FZRA',119:'FZDZ',
                               122:'DZ',130:'-RA',131:'RA',132:'+RA',
                               135:'RASN',140:'-SN',141:'SN',142:'+SN',
                               160:'-SHRA',161:'SHRA',162:'+SHRA',
                               170:'TS',171:'TSRA',172:'TSSN',
                               508:None,509:None}
                    if ww_int in tab4677:
                        wx_str = tab4677[ww_int]
                    elif ww_int < 91:
                        wx_str = next((v for t,v in tab4677_thresh if ww_int>=t),None)
                    elif ww_int in tab4678:
                        wx_str = tab4678[ww_int]
                    elif 110 < ww_int <= 191:
                        wx_str = None
                    if wx_str and 'TS' in wx_str:
                        print(f'  [BUFR TS] WMO{wmo_id} ww={ww_int} → "{wx_str}"')
                cmap={0:'SKC',1:'FEW',2:'FEW',3:'FEW',4:'SCT',
                      5:'SCT',6:'BKN',7:'BKN',8:'OVC'}
                COVER_RANK = {'SKC':0,'FEW':1,'SCT':2,'BKN':3,'OVC':4}
                c_amt  = _safe_arr(handle,'cloudAmount')
                c_base = _safe_arr(handle,'heightOfBaseOfCloud')
                c_vsig = _safe_arr(handle,'verticalSignificanceSurfaceObservations')
                n = min(len(c_amt), len(c_base))
                # Collect raw layers: skip vertSig=7 (N, total cover) and missing bases
                raw = []
                for i in range(n):
                    a, h = c_amt[i], c_base[i]
                    if a is None or h is None: continue
                    vs = c_vsig[i] if i < len(c_vsig) else None
                    if vs == 7: continue          # total cloud cover N – no layer
                    base_ft = int(h * 3.28084)
                    if base_ft < 0: continue
                    raw.append((base_ft, cmap.get(int(a),'FEW'), vs))
                # Prefer vertSig=20 layers (explicitly observed single layers)
                sig20 = [(b,c) for b,c,vs in raw if vs == 20]
                pool  = sig20 if sig20 else [(b,c) for b,c,vs in raw]
                # Deduplicate: for bases within 500 ft keep highest cover
                pool.sort(key=lambda x: x[0])
                merged, used = [], []
                for base_ft, cover in pool:
                    near = next((j for j,b in enumerate(used) if abs(base_ft-b)<500), None)
                    if near is None:
                        merged.append({'skyCover':cover,'cloudBase':base_ft})
                        used.append(base_ft)
                    elif COVER_RANK.get(cover,0) > COVER_RANK.get(merged[near]['skyCover'],0):
                        merged[near] = {'skyCover':cover,'cloudBase':base_ft}
                # Build final sky: up to 3 layers; always include lowest ceiling (BKN/OVC)
                ceil_layers  = [l for l in merged if l['skyCover'] in ('BKN','OVC')]
                other_layers = [l for l in merged if l['skyCover'] not in ('BKN','OVC')]
                sky = []
                if ceil_layers:
                    sky.append(ceil_layers[0])   # lowest ceiling always included
                    below = [l for l in other_layers if l['cloudBase'] < ceil_layers[0]['cloudBase']]
                    sky = sorted(below, key=lambda x:x['cloudBase'])[-1:] + sky  # 1 layer below
                    if len(sky) < 3 and len(ceil_layers) > 1:
                        sky.append(ceil_layers[1])   # second ceiling layer if room
                else:
                    sky = other_layers[:3]
                sky.sort(key=lambda x: x['cloudBase'])
                name = _safe(handle,'stationOrSiteName') or ''
                return {'icaoId': f'WMO{str(wmo_id).zfill(5)}' if wmo_id else None,
                    'wmoId':wmo_id,'name':name.strip(),
                    'lat':round(float(lat),4),'lon':round(float(lon),4),
                    'elev':int(elev) if elev is not None else None,
                    'obsTime':obs_time,'metarType':'SYNOP-BUFR',
                    'temp':temp,'dewp':dewp,'wdir':wdir,'wspd':wspd,'wgst':wgst,
                    'altim':qnh,'slp':round(slp/100,1) if slp else None,
                    'visib':str(int(vis)) if vis is not None else None,
                    'wxString':wx_str,'skyCondition':sky,'rawOb':
                    f'BUFR SYNOP WMO{wmo_id} {yr}-{mo:02d}-{dy:02d} {hr:02d}:{mi:02d}Z'}

            try:
                # Get directory listing
                req = urllib.request.Request(DWD_BASE, headers=DWD_H)
                with urllib.request.urlopen(req, timeout=15) as r:
                    listing = r.read().decode('utf-8','replace')
                # Parse href links from directory listing HTML
                # DWD encodes comma as %2C in hrefs: bda01%2Csynop_bufr
                import urllib.parse as _up
                hrefs = _re.findall(r'href="([^"]+)"', listing)
                entries = []
                for href in hrefs:
                    fname = _up.unquote(href.split('/')[-1])
                    # Z__C_EDZW_ has double underscore — match EDZW_ flexibly
                    m = _re.search(r'EDZW_(\d{14})_.*synop_bufr', fname)
                    if not m: continue
                    ts_str = m.group(1)
                    try:
                        fts = _dt.datetime(int(ts_str[:4]),int(ts_str[4:6]),
                            int(ts_str[6:8]),int(ts_str[8:10]),int(ts_str[10:12]),
                            int(ts_str[12:14]), tzinfo=_dt.timezone.utc)
                    except: continue
                    sm = _re.search(_re.escape(fname)+r'[^\d]*(\d{3,7})', listing)
                    size = int(sm.group(1)) if sm else 0
                    entries.append((fname, fts, size))
                # Take files from last 3.5h (covers previous synoptic hour + current)
                # Sort by size DESC → largest = fullest synoptic batches
                entries.sort(key=lambda x: x[1], reverse=True)
                cutoff35 = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=3.5)
                recent = [(n,t,sz) for n,t,sz in entries if t >= cutoff35]
                if not recent:
                    recent = entries  # fallback: use all
                recent.sort(key=lambda x: x[2], reverse=True)
                top_files = [DWD_BASE + _up.quote(n, safe='_-.,~') for n,_,_ in recent[:12]]
                if not entries:
                    print('  BUFR: no synop_bufr files found')
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Access-Control-Allow-Origin','*')
                    self.end_headers()
                    self.wfile.write(b'[]')
                    return
                newest_ts = entries[0][1]
                age_h = (_dt.datetime.now(_dt.timezone.utc) - newest_ts).total_seconds()/3600
                print(f'  BUFR: {len(entries)} files, newest={newest_ts.strftime("%H:%MZ")}, age={age_h:.1f}h, fetching {len(top_files)}, bbox={bbox}')
                if age_h > 3.0:
                    print(f'  BUFR: data too old ({age_h:.1f}h > 3h), skipping')
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Access-Control-Allow-Origin','*')
                    self.end_headers()
                    self.wfile.write(b'[]')
                    return
                # Decode
                seen = {}
                for url in top_files:
                    req2 = urllib.request.Request(url, headers=DWD_H)
                    with urllib.request.urlopen(req2, timeout=30) as r2:
                        buf_data = r2.read()
                    import tempfile as _tf, os as _os
                    tmp = _tf.NamedTemporaryFile(delete=False, suffix='.bufr')
                    try:
                        tmp.write(buf_data); tmp.flush(); tmp.close()
                        msg_count = 0; decode_ok = 0; decode_err = 0
                        with open(tmp.name, 'rb') as buffile:
                          while True:
                            try:
                              handle = eccodes.codes_bufr_new_from_file(buffile)
                              if handle is None: break
                              msg_count += 1
                              try:
                                n_sub = int(eccodes.codes_get(handle,'numberOfSubsets') or 1)
                              except: n_sub = 1
                              for si in range(1, n_sub + 1):
                                try:
                                  if n_sub > 1:
                                    sub = eccodes.codes_clone(handle)
                                    eccodes.codes_set(sub,'unpack', 1)
                                    eccodes.codes_set(sub,'extractSubset', si)
                                    eccodes.codes_set(sub,'doExtractSubsets', 1)
                                  else:
                                    sub = handle
                                  try:
                                    obs = decode_msg(sub)
                                    if obs and obs.get('wmoId'):
                                      if bbox:
                                        s2,w2,n2,e2 = bbox
                                        if not (s2<=obs['lat']<=n2 and w2<=obs['lon']<=e2):
                                          continue
                                      k = obs['wmoId']
                                      if k not in seen:
                                        seen[k] = obs; decode_ok += 1
                                  finally:
                                    if n_sub > 1: eccodes.codes_release(sub)
                                except Exception as de:
                                  decode_err += 1
                                  if decode_err <= 2:
                                    print(f'  BUFR subset error: {type(de).__name__}: {de}')
                              eccodes.codes_release(handle)
                            except Exception as le:
                              print(f'  BUFR read err: {le}'); break
                        total_decoded = sum(1 for v in seen.values() if True)
                        print(f'  BUFR file: {msg_count} msgs, {decode_ok} in-bbox, {decode_err} err | total so far: {len(seen)}')
                    finally:
                        _os.unlink(tmp.name)
                result = list(seen.values())
                print(f'  BUFR: {len(top_files)} files → {len(result)} stations')
                data = _json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Access-Control-Allow-Origin','*')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                import traceback
                print(f'  BUFR error: {type(e).__name__}: {e}')
                traceback.print_exc()
                try:
                    self.send_error(502, str(e))
                except Exception:
                    pass
            return

        if parsed.path == '/proxy':
            qs  = urllib.parse.parse_qs(parsed.query)
            url = qs.get('url', [None])[0]
            if not url:
                self.send_error(400, "url parameter missing"); return
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'MeteoMap/1.0'})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_error(502, str(e))
        else:
            super().do_GET()

    def log_message(self, fmt, *args):
        # Only log proxy/API calls, suppress static file noise
        first = str(args[0]) if args else ''
        if any(p in first for p in ['/proxy','/ogimet','/dwd','/bufr']):
            print(f"  → {first[:80]}")

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
    srv = ThreadedHTTPServer(('127.0.0.1', port), Handler)
    url = f"http://localhost:{port}/meteomap.html"
    print(f"\n  MeteoMap-Server läuft")
    print(f"  ─────────────────────────────────────")
    print(f"  Browser öffnen: {url}")
    print(f"  Beenden:        Ctrl+C\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server gestoppt.")
