#!/usr/bin/env python3
"""
MeteoMap WIS2 Collector
Subscribes to a WIS2 Global Broker via MQTT and decodes surface-based-observations
(SYNOP) notification messages in near-real-time. Runs as a persistent service
(no timer) — unlike the other collectors, it reacts to push notifications
instead of polling on a fixed cadence.
"""
import sys
import json
import base64
import re
import time

import requests
import eccodes
import paho.mqtt.client as mqtt

from obs_store import open_store
from bufr_decode import decode_msg

# ── Configuration ─────────────────────────────────────────────────────────────

BROKER_HOST = 'globalbroker.meteo.fr'
BROKER_PORT = 8883
BROKER_USER = 'everyone'
BROKER_PASS = 'everyone'

TOPIC = 'origin/a/wis2/+/data/core/weather/surface-based-observations/#'

# Only the classic "0-20000-0-XXXXX" WIGOS scheme maps 1:1 to a 5-digit WMO
# block/station number, which is what wmo_stations.json and the rest of the
# store are keyed on. Stations published under any other WIGOS issuer (newer
# national networks with no classic WMO number) are skipped for now.
WIGOS_CLASSIC_RE = re.compile(r'^0-20000-0-(\d{5})$')

FETCH_TIMEOUT = 15
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; MeteoMap/1.0)'})

STATS_INTERVAL_S = 300  # log a throughput/latency summary every 5 min

# ── Message handling ──────────────────────────────────────────────────────────

def fetch_bufr_bytes(wnm: dict) -> bytes | None:
    """Return the raw BUFR message bytes for a WIS2 Notification Message,
    either inlined (small messages) or via its canonical link."""
    props = wnm.get('properties', {})
    content = props.get('content')
    if content and content.get('encoding') == 'base64':
        try:
            return base64.b64decode(content['value'])
        except Exception:
            return None

    for link in wnm.get('links', []):
        if link.get('rel') == 'canonical':
            try:
                r = SESSION.get(link['href'], timeout=FETCH_TIMEOUT)
                r.raise_for_status()
                return r.content
            except Exception as exc:
                print(f'  WARN: Download fehlgeschlagen ({link.get("href")}): {exc}',
                      file=sys.stderr)
                return None
    return None


def decode_wnm(wnm: dict) -> dict | None:
    """Decode the BUFR message referenced by one WIS2 Notification Message."""
    props = wnm.get('properties', {})
    wigos = props.get('wigos_station_identifier', '')
    m = WIGOS_CLASSIC_RE.match(wigos)
    if not m:
        return None  # no classic WMO ID -> not yet mappable to wmo_stations.json

    fallback_wmo_id = int(m.group(1))

    data = fetch_bufr_bytes(wnm)
    if not data:
        return None

    try:
        handle = eccodes.codes_new_from_message(data)
    except Exception:
        return None
    if handle is None:
        return None

    try:
        obs = decode_msg(handle, metar_type='SYNOP-WIS2', raw_prefix='WIS2 SYNOP')
    finally:
        eccodes.codes_release(handle)

    if obs is None:
        return None
    if obs.get('wmoId') is None:
        # BUFR content used WIGOS-only station identification (no classic
        # blockNumber/stationNumber) — fall back to the WIGOS ID from the
        # notification message itself, which we already confirmed is classic.
        obs['wmoId'] = fallback_wmo_id
        obs['icaoId'] = f'WMO{fallback_wmo_id:05d}'

    # Prefer the notification's own geometry if the BUFR gave no coordinates
    # (shouldn't normally happen, kept for robustness).
    if obs.get('lat') is None or obs.get('lon') is None:
        geom = wnm.get('geometry', {})
        if geom.get('type') == 'Point':
            obs['lon'], obs['lat'] = geom['coordinates'][:2]

    return obs


# ── MQTT callbacks ───────────────────────────────────────────────────────────

_stats = {'received': 0, 'stored': 0, 'skipped': 0, 'errors': 0, 'latency_sum': 0.0, 'latency_n': 0}
_last_stats_log = time.time()
_last_cleanup    = time.time()
CLEANUP_INTERVAL_S = 600


def _log_stats_if_due():
    global _last_stats_log
    now = time.time()
    if now - _last_stats_log < STATS_INTERVAL_S:
        return
    avg_lat = (_stats['latency_sum'] / _stats['latency_n']) if _stats['latency_n'] else None
    print(f"[stats] empfangen={_stats['received']} gespeichert={_stats['stored']} "
          f"übersprungen={_stats['skipped']} fehler={_stats['errors']} "
          f"⌀Latenz(Termin→jetzt)={f'{avg_lat:.0f}s' if avg_lat is not None else 'n/a'}")
    _stats.update(received=0, stored=0, skipped=0, errors=0, latency_sum=0.0, latency_n=0)
    _last_stats_log = now


def _cleanup_if_due(db):
    """Runs on the same (MQTT network) thread as every other DB access,
    since sqlite3 connections here are not safe to share across threads."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < CLEANUP_INTERVAL_S:
        return
    removed = db.cleanup()
    db.commit()
    if removed:
        print(f'SQLite: {removed} alte Zeilen gelöscht')
    _last_cleanup = now


def on_connect(client, userdata, flags, rc, properties=None):
    print(f'WIS2: verbunden mit {BROKER_HOST}:{BROKER_PORT} (rc={rc})')
    client.subscribe(TOPIC, qos=0)
    print(f'WIS2: abonniert auf {TOPIC}')


def on_message(client, userdata, msg):
    _stats['received'] += 1
    try:
        wnm = json.loads(msg.payload)
    except Exception:
        _stats['errors'] += 1
        _log_stats_if_due()
        return

    try:
        obs = decode_wnm(wnm)
    except Exception as exc:
        _stats['errors'] += 1
        print(f'  WARN: Dekodierung fehlgeschlagen: {exc}', file=sys.stderr)
        obs = None

    if obs is None or obs.get('obsTime') is None:
        _stats['skipped'] += 1
        _log_stats_if_due()
        return

    # Sanity check: some national WIS2 nodes have been observed to publish
    # clock-skewed or stale test data. A "future" observation is never valid;
    # reject it rather than let it outrank a correct BUFR/OGIMET row later.
    if obs['obsTime'] > time.time() + 300:
        _stats['skipped'] += 1
        print(f'  WARN: verworfen (Termin in der Zukunft) {obs.get("wmoId")} '
              f'obsTime={obs["obsTime"]}', file=sys.stderr)
        _log_stats_if_due()
        return

    db = userdata['db']
    skey = f'WMO{obs["wmoId"]:05d}'
    try:
        db.upsert('synop-wis2', skey, obs['obsTime'], obs['lat'], obs['lon'], obs)
        db.commit()
        _stats['stored'] += 1
        latency = time.time() - obs['obsTime']
        if 0 <= latency < 6 * 3600:
            _stats['latency_sum'] += latency
            _stats['latency_n']   += 1
    except Exception as exc:
        _stats['errors'] += 1
        print(f'  WARN: SQLite upsert fehlgeschlagen ({skey}): {exc}', file=sys.stderr)

    _cleanup_if_due(db)
    _log_stats_if_due()


def on_disconnect(client, userdata, *args):
    print('WIS2: Verbindung getrennt, paho versucht automatisch neu zu verbinden …',
          file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open_store() as db:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        client.username_pw_set(BROKER_USER, BROKER_PASS)
        client.tls_set()
        client.user_data_set({'db': db})
        client.on_connect    = on_connect
        client.on_message    = on_message
        client.on_disconnect = on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)

        # loop_forever() runs on the calling (main) thread and invokes all
        # callbacks there too, so the sqlite3 connection (db) is only ever
        # touched from this one thread — no locking needed.
        try:
            client.loop_forever()
        except KeyboardInterrupt:
            pass
        finally:
            client.disconnect()


if __name__ == '__main__':
    main()
