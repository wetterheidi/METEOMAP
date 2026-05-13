#!/usr/bin/env python3
"""
Persistent observation store — SQLite backend.

Schema: one row per (source, station_key, obs_time).
Retains the last RETAIN_HOURS of data; older rows are purged on cleanup().

Usage (collectors):
    from obs_store import open_store
    with open_store() as db:
        db.upsert(source, skey, obs_time, lat, lon, obs_dict)
        db.cleanup()

Usage (API):
    from obs_store import open_store
    with open_store() as db:
        obs_list = db.query(target_t, south, west, north, east)
"""

import json
import sqlite3
import time
import pathlib
from contextlib import contextmanager

DB_PATH      = pathlib.Path('/apps/MeteoMap/data/obs.sqlite3')
RETAIN_HOURS = 12   # hours of history kept
WINDOW_S     = 5400  # ±90 min query window (SYNOP hourly + collector delay + buffer)


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS observations (
    id       INTEGER PRIMARY KEY,
    source   TEXT    NOT NULL,
    skey     TEXT    NOT NULL,
    obs_time INTEGER NOT NULL,
    lat      REAL    NOT NULL,
    lon      REAL    NOT NULL,
    data     TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_uq   ON observations(source, skey, obs_time);
CREATE INDEX        IF NOT EXISTS idx_time ON observations(obs_time);
"""


# ── Store class ───────────────────────────────────────────────────────────────

class ObsStore:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def upsert(self, source: str, skey: str, obs_time: int,
               lat: float, lon: float, data: dict) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO observations(source, skey, obs_time, lat, lon, data)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source, skey, obs_time, lat, lon, json.dumps(data, ensure_ascii=False)),
        )

    def cleanup(self) -> int:
        cutoff = int(time.time()) - RETAIN_HOURS * 3600
        cur = self._conn.execute(
            'DELETE FROM observations WHERE obs_time < ?', (cutoff,))
        return cur.rowcount

    def query(self, target_t: int,
              south: float, west: float, north: float, east: float,
              window_s: int = WINDOW_S) -> list[dict]:
        """Return one observation per (source, station) closest to target_t within ±window_s."""
        t_min = target_t - window_s
        t_max = target_t + window_s
        rows = self._conn.execute(
            """SELECT data FROM (
                   SELECT data,
                          ROW_NUMBER() OVER (
                              PARTITION BY source, skey
                              ORDER BY ABS(obs_time - ?)
                          ) AS rn
                   FROM observations
                   WHERE lat  BETWEEN ? AND ?
                     AND lon  BETWEEN ? AND ?
                     AND obs_time BETWEEN ? AND ?
               ) WHERE rn = 1""",
            (target_t, south, north, west, east, t_min, t_max),
        ).fetchall()
        return [json.loads(r[0]) for r in rows]


# ── Context manager ───────────────────────────────────────────────────────────

@contextmanager
def open_store(path: pathlib.Path = DB_PATH):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    for stmt in _DDL.strip().split(';'):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()
    store = ObsStore(conn)
    try:
        yield store
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
