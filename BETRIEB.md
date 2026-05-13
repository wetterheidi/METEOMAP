# MeteoMap – Betriebsdokumentation

## Architekturüberblick

```
┌─────────────────────────────────────────────────────────┐
│                    Hetzner-Server                        │
│                                                         │
│  ┌──────────────┐   ┌──────────────┐  ┌─────────────┐  │
│  │ SYNOP-       │   │ BUFR-        │  │ DMI-        │  │
│  │ Collector    │   │ Collector    │  │ Collector   │  │
│  │ (alle 30min) │   │ (alle 30min) │  │ (alle 15min)│  │
│  └──────┬───────┘   └──────┬───────┘  └──────┬──────┘  │
│         │                  │                  │         │
│         └──────────────────┴──────────────────┘         │
│                            │                            │
│                    ┌───────▼────────┐                   │
│                    │  obs.sqlite3   │  (12h Verlauf)    │
│                    └───────┬────────┘                   │
│                            │                            │
│                    ┌───────▼────────┐                   │
│                    │  API-Server    │  Port 8001         │
│                    │  (uvicorn)     │  (hinter nginx)    │
│                    └───────┬────────┘                   │
└────────────────────────────┼────────────────────────────┘
                             │ HTTPS
                    ┌────────▼────────┐
                    │    Browser      │
                    │  (Frontend)     │
                    └─────────────────┘
```

**Datenfluss:**
1. Die drei Collector-Skripte holen regelmäßig Daten von externen Quellen
2. Alle Beobachtungen landen in einer zentralen SQLite-Datenbank (`obs.sqlite3`)
3. Der API-Server liefert auf Anfrage die zur gewünschten Zeit passenden Daten aus
4. Das Frontend (einzige HTML-Datei) visualisiert die Daten auf der Karte

---

## Systemd-Dienste

### Übersicht

| Dienst | Typ | Takt | Beschreibung |
|--------|-----|------|--------------|
| `meteomap-api.service` | dauerhaft laufend | — | API-Server (uvicorn) |
| `meteomap-collector.timer` | Timer | alle 30 min | Startet SYNOP-Collector |
| `meteomap-collector.service` | oneshot | — | OGIMET SYNOP holen & speichern |
| `meteomap-bufr-collector.timer` | Timer | alle 30 min | Startet BUFR-Collector |
| `meteomap-bufr-collector.service` | oneshot | — | DWD BUFR holen & speichern |

> DMI (Grönland) läuft derzeit ohne eigenen Timer – falls ein separater
> `meteomap-dmi-collector.service` eingerichtet ist, gilt analoges.

### Status prüfen

```bash
systemctl status meteomap-api.service
systemctl status meteomap-collector.timer
systemctl status meteomap-bufr-collector.timer

# Letzte Collector-Läufe ansehen:
journalctl -u meteomap-collector.service -n 50
journalctl -u meteomap-bufr-collector.service -n 50
```

### Manueller Neustart

```bash
# API-Server (z.B. nach Code-Update):
sudo systemctl restart meteomap-api.service

# Collector einmalig manuell anstoßen:
sudo systemctl start meteomap-collector.service
sudo systemctl start meteomap-bufr-collector.service
```

---

## Datenhaltung

### Datenbank

**Pfad:** `/apps/MeteoMap/data/obs.sqlite3`  
**Format:** SQLite 3, WAL-Mode  
**Verlauf:** 12 Stunden (konfigurierbar in `server/obs_store.py`, `RETAIN_HOURS`)

**Schema:**
```sql
observations (
    source   TEXT,    -- 'synop-ogimet' | 'synop-bufr' | 'synop-dmi'
    skey     TEXT,    -- Stations-Key, z.B. 'WMO10501'
    obs_time INTEGER, -- Unix-Timestamp UTC
    lat      REAL,
    lon      REAL,
    data     TEXT     -- JSON mit allen dekodierten Feldern
)
```

**Größe prüfen:**
```bash
ls -lh /apps/MeteoMap/data/obs.sqlite3
sqlite3 /apps/MeteoMap/data/obs.sqlite3 \
  "SELECT source, COUNT(*), MIN(datetime(obs_time,'unixepoch')), MAX(datetime(obs_time,'unixepoch')) FROM observations GROUP BY source;"
```

### Cleanup

Jeder Collector-Lauf löscht am Ende automatisch alle Zeilen älter als 12 Stunden.
Kein manuelles Eingreifen notwendig.

### Legacy-JSON-Dateien

Die alten Dateien (`bufr_latest.json`, `dmi_latest.json`) werden weiterhin
erzeugt und von den Legacy-API-Routen (`/meteomap/bufr`, `/meteomap/dmi`) bedient.
Sie können nach vollständiger Migration entfernt werden.

---

## Konfigurationsparameter

### `server/obs_store.py`
| Parameter | Wert | Bedeutung |
|-----------|------|-----------|
| `RETAIN_HOURS` | 12 | Wie viele Stunden Verlauf gespeichert werden |
| `WINDOW_S` | 5400 | Zeitfenster der Datenbankabfrage (±90 min) |

### `server/synop_collector.py`
| Parameter | Wert | Bedeutung |
|-----------|------|-----------|
| `HOURS_BACK` | 3.0 | Wie weit OGIMET zurückgefragt wird |
| `REQUEST_PAUSE` | 2.5 s | Pause zwischen OGIMET-Requests (Ratenlimit) |
| `PREFETCH_BLOCKS` | Liste | WMO-Blöcke, die vorgeladen werden (Europa + N-Afrika) |

### `server/bufr_collector.py`
| Parameter | Wert | Bedeutung |
|-----------|------|-----------|
| `MAX_FILES` | 30 | Max. BUFR-Dateien pro Lauf |
| `MAX_AGE_H` | 1.5 | Maximales Alter der BUFR-Dateien |

---

## Datensourcen

### METAR (Aviation Weather Center / AWC)
- **Abruf:** direkt vom Browser via CORS-Proxy
- **Quelle:** `https://aviationweather.gov/api/data/metar`
- **Serverberührung:** keine (läuft vollständig client-seitig)
- **Abdeckung:** global (Flughafenstationen)

### SYNOP OGIMET
- **Abruf:** Collector alle 30 min, OGIMET-API
- **Abdeckung:** Europa, Nordafrika (konfigurierte WMO-Blöcke)
- **Meldungsrhythmus:** stündlich (Haupttermine) oder 3-stündlich

### DWD BUFR
- **Abruf:** Collector alle 30 min, DWD OpenData
- **Abdeckung:** International (Europa-Schwerpunkt)
- **Besonderheit:** höhere Datenqualität als OGIMET (originale BUFR-Rohdaten),
  daher bei Doppelerfassung bevorzugt

### DMI (Danish Meteorological Institute)
- **Abruf:** Collector alle 15 min, DMI Open Data API
- **Abdeckung:** Grönland
- **Besonderheit:** ergänzt Bereiche, die von OGIMET/BUFR nicht abgedeckt werden

---

## Was zu überwachen ist

### Normalbetrieb – Anzeichen, dass alles läuft

```bash
# Alle Timer aktiv?
systemctl list-timers | grep meteomap

# DB aktuell?
sqlite3 /apps/MeteoMap/data/obs.sqlite3 \
  "SELECT source, MAX(datetime(obs_time,'unixepoch')) FROM observations GROUP BY source;"
# → jüngste Einträge sollten < 40 min alt sein
```

### Typische Probleme

| Symptom | Wahrscheinliche Ursache | Abhilfe |
|---------|------------------------|---------|
| Keine SYNOP-Stationen im Browser | Collector ausgefallen | `journalctl -u meteomap-collector.service -n 30` |
| API antwortet nicht | uvicorn gestoppt | `sudo systemctl restart meteomap-api.service` |
| Collector schlägt fehl | OGIMET nicht erreichbar | Temporär — nächster Lauf in 30 min |
| DB leer nach Server-Neustart | Rechte-Problem auf `/apps/MeteoMap/data/` | `sudo chown -R heidi:heidi /apps/MeteoMap/data/` |
| `attempt to write a readonly database` | DB-Datei gehört root | `sudo chown heidi:heidi /apps/MeteoMap/data/obs.sqlite3*` |

---

## Update-Workflow (Code-Änderungen einspielen)

```bash
cd /apps/MeteoMap/repo
git pull

# API-Server immer neu starten (lädt Python-Code neu):
sudo systemctl restart meteomap-api.service

# Collector werden beim nächsten Timer-Tick automatisch mit neuem Code gestartet.
# Für sofortigen Test:
sudo systemctl start meteomap-collector.service
```

---

## Verzeichnisstruktur

```
/apps/MeteoMap/
├── repo/                        # Git-Repository
│   ├── meteomap_52.html         # Frontend (einzige HTML-Datei)
│   ├── wmo_stations.json        # WMO-Stationsliste (statisch)
│   ├── geo/
│   │   └── ne_countries.geojson # Ländergrenzen
│   └── server/
│       ├── api_server.py        # FastAPI-Anwendung
│       ├── obs_store.py         # SQLite-Storage-Layer
│       ├── synop_collector.py   # OGIMET-Collector + FM-12-Parser
│       ├── bufr_collector.py    # DWD BUFR-Collector
│       └── dmi_collector.py     # DMI-Collector
├── data/
│   ├── obs.sqlite3              # Hauptdatenbank (12h Verlauf)
│   ├── bufr_latest.json         # Legacy (BUFR-Fallback)
│   └── dmi_latest.json          # Legacy (DMI-Fallback)
└── venv/                        # Python-Virtualenv
```
