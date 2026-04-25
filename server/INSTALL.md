# MeteoMap Server – Installationsanleitung

Alle Befehle werden auf dem Hetzner-Server als `root` ausgeführt,
sofern nicht anders angegeben.

---

## 1. Verzeichnisse anlegen

```bash
mkdir -p /apps/MeteoMap/data/synop
chown -R heidi:heidi /apps/MeteoMap
```

---

## 2. Repository klonen

```bash
su - heidi -s /bin/bash -c "git clone https://github.com/wetterheidi/METEOMAP.git /apps/MeteoMap/repo"
```

---

## 3. Python-Umgebung einrichten

```bash
python3 -m venv /apps/MeteoMap/venv
/apps/MeteoMap/venv/bin/pip install -r /apps/MeteoMap/repo/server/requirements.txt
chown -R heidi:heidi /apps/MeteoMap/venv
```

---

## 4. Systemd-Services installieren

```bash
cp /apps/MeteoMap/repo/server/deploy/meteomap-api.service       /etc/systemd/system/
cp /apps/MeteoMap/repo/server/deploy/meteomap-collector.service  /etc/systemd/system/
cp /apps/MeteoMap/repo/server/deploy/meteomap-collector.timer    /etc/systemd/system/

systemctl daemon-reload

# API-Server starten und beim Booten aktivieren
systemctl enable --now meteomap-api.service

# Collector-Timer starten (läuft alle 30 min)
systemctl enable --now meteomap-collector.timer
```

Prüfen ob alles läuft:

```bash
systemctl status meteomap-api.service
systemctl status meteomap-collector.timer
```

---

## 5. nginx konfigurieren

Den Inhalt von `deploy/nginx-meteomap.conf` in die bestehende nginx-Konfiguration einfügen:

```bash
nano /etc/nginx/sites-available/windscope
```

Den `location /meteomap/` Block **innerhalb** des `server { }` Blocks einfügen
(vor der schließenden `}`), dann:

```bash
nginx -t && systemctl reload nginx
```

---

## 6. Cache erstmalig befüllen

Den Collector einmal manuell ausführen, damit sofort Daten vorhanden sind:

```bash
systemctl start meteomap-collector.service

# Fortschritt beobachten:
journalctl -u meteomap-collector.service -f
```

Das dauert ca. 1–2 Minuten (20 Blöcke × 2,5 s Pause + OGIMET-Antwortzeit).

---

## 7. Funktionstest

```bash
# API-Health-Check
curl http://localhost:8001/health

# Einen Block abrufen (Block 10 = Deutschland)
curl "http://localhost:8001/meteomap/ogimet/10?hours=3" | head -5

# Über nginx
curl "http://localhost/meteomap/ogimet/10?hours=3" | head -5
```

---

## Updates einspielen

Nach einem `git push` vom Mac:

```bash
su - heidi -s /bin/bash -c "git -C /apps/MeteoMap/repo pull"
systemctl restart meteomap-api.service
```

---

## Logs

```bash
# API-Server-Logs
journalctl -u meteomap-api.service -f

# Collector-Logs (letzter Lauf)
journalctl -u meteomap-collector.service -n 50

# Nächster geplanter Collector-Lauf
systemctl list-timers meteomap-collector.timer
```
