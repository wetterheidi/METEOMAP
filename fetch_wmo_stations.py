#!/usr/bin/env python3
"""
fetch_wmo_stations.py  —  v2
Quellen: Meteostat Bulk (primär) + AWC (Ergänzung)
"""
import gzip, json, sys, time, urllib.request
from pathlib import Path

OUT_FILE   = Path(__file__).parent / "wmo_stations.json"
METEOSTAT  = "https://bulk.meteostat.net/v2/stations/full.json.gz"
AWC_BASE   = "https://aviationweather.gov/api/data/stationinfo"
HEADERS    = {"User-Agent":"MeteoMap-StationFetcher/2.0","Referer":"http://localhost:8765/"}

def fetch_meteostat(stations):
    print("  Lade Meteostat Stationsliste ...", end=" ", flush=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(METEOSTAT, headers=HEADERS), timeout=60) as r:
            data = json.loads(gzip.decompress(r.read()).decode("utf-8"))
    except Exception as e:
        print(f"FEHLER: {e}"); return 0
    added = 0
    for st in data:
        try:
            ids = st.get("identifiers") or {}
            loc = st.get("location")    or {}
            nam = st.get("name")        or {}
            lat, lon = loc.get("latitude"), loc.get("longitude")
            if lat is None or lon is None: continue
            wmo = None
            if ids.get("wmo"):
                try: wmo = int(str(ids["wmo"]).strip())
                except: pass
            icao = (ids.get("icao") or "").strip().upper() or None
            key  = f"wmo{wmo}" if wmo else (f"icao{icao}" if icao else None)
            if not key or key in stations: continue
            stations[key] = {
                "wmo": wmo, "icao": icao,
                "name": (nam.get("de") or nam.get("en") or "").strip(),
                "country": (st.get("country") or "").strip().upper(),
                "state": (st.get("region") or "").strip(),
                "lat": round(float(lat),4), "lon": round(float(lon),4),
                "elev": (int(loc["elevation"]) if loc.get("elevation") is not None else None),
            }
            added += 1
        except: continue
    print(f"OK ({added} Stationen)"); return added

def fetch_awc_tile(s,w,n,e):
    url = f"{AWC_BASE}?bbox={s},{w},{n},{e}&format=json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=20) as r:
            d = json.load(r)
            return d if isinstance(d,list) else []
    except: return []

def fetch_awc(stations):
    tiles = [(s,w,min(s+30,90),min(w+45,180)) for s in range(-90,90,30) for w in range(-180,180,45)]
    added = 0
    for i,(s,w,n,e) in enumerate(tiles,1):
        sys.stdout.write(f"\r  AWC Kachel {i:2d}/{len(tiles)} | {added} neu ..."); sys.stdout.flush()
        for raw in fetch_awc_tile(s,w,n,e):
            try:
                lat,lon = raw.get("lat"), raw.get("lon")
                if lat is None or lon is None: continue
                wmo = None
                if raw.get("wmoId"):
                    try: wmo = int(str(raw["wmoId"]))
                    except: pass
                icao = (raw.get("icaoId") or "").strip().upper() or None
                key  = f"wmo{wmo}" if wmo else (f"icao{icao}" if icao else None)
                if not key or key in stations: continue
                stations[key] = {
                    "wmo": wmo, "icao": icao,
                    "name": (raw.get("name") or raw.get("site") or "").strip(),
                    "country": (raw.get("country") or "").strip().upper(),
                    "state": (raw.get("state") or "").strip(),
                    "lat": round(float(lat),4), "lon": round(float(lon),4),
                    "elev": raw.get("elev"),
                }
                added += 1
            except: continue
        time.sleep(0.3)
    print(f"\n  AWC: {added} zusätzlich"); return added

def main():
    print(f"\n  MeteoMap Stations-Fetch v2\n  Output: {OUT_FILE}\n")
    stations = {}
    ms  = fetch_meteostat(stations)
    print("  Ergänze mit AWC ...")
    awc = fetch_awc(stations)
    result = sorted(stations.values(), key=lambda x:(x['wmo'] or 999999, x['icao'] or 'ZZZZ'))
    print(f"\n  Meteostat: {ms}  AWC neu: {awc}  Gesamt: {len(result)}")
    print(f"  WMO: {sum(1 for s in result if s['wmo'])}  ICAO: {sum(1 for s in result if s['icao'])}")
    with open(OUT_FILE,"w",encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",",":"))
    print(f"  Gespeichert: {OUT_FILE} ({OUT_FILE.stat().st_size/1024:.0f} KB)\n")

if __name__ == "__main__":
    main()
