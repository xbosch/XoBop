#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "memory.db"


def percentile(values: list[int], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = int(round((len(values) - 1) * p))
    return float(values[k])


def main() -> int:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS op_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            chat_id INTEGER NOT NULL DEFAULT 0,
            event_type TEXT NOT NULL,
            route TEXT DEFAULT '',
            command TEXT DEFAULT '',
            status TEXT DEFAULT 'ok',
            latency_ms INTEGER,
            retries INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS op_signals_daily (
            day TEXT PRIMARY KEY,
            events_total INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            command_unknown INTEGER NOT NULL DEFAULT 0,
            retries_total INTEGER NOT NULL DEFAULT 0,
            median_latency_ms REAL NOT NULL DEFAULT 0,
            p95_latency_ms REAL NOT NULL DEFAULT 0,
            updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    days = [r[0] for r in con.execute("SELECT DISTINCT date(timestamp) AS d FROM op_events ORDER BY d")]
    upserts = 0
    for day in days:
        rows = con.execute(
            "SELECT event_type,status,COALESCE(latency_ms,0) AS latency_ms,COALESCE(retries,0) AS retries FROM op_events WHERE date(timestamp)=?",
            (day,),
        ).fetchall()
        if not rows:
            continue
        total = len(rows)
        failures = sum(1 for r in rows if (r["status"] or "ok") not in ("ok", "skipped"))
        unknown = sum(1 for r in rows if r["event_type"] == "unknown_command")
        retries_total = sum(int(r["retries"]) for r in rows)
        latencies = [int(r["latency_ms"]) for r in rows if int(r["latency_ms"]) > 0]
        median = percentile(latencies, 0.5)
        p95 = percentile(latencies, 0.95)

        con.execute(
            """
            INSERT INTO op_signals_daily(day,events_total,failures,command_unknown,retries_total,median_latency_ms,p95_latency_ms,updated_ts)
            VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(day) DO UPDATE SET
              events_total=excluded.events_total,
              failures=excluded.failures,
              command_unknown=excluded.command_unknown,
              retries_total=excluded.retries_total,
              median_latency_ms=excluded.median_latency_ms,
              p95_latency_ms=excluded.p95_latency_ms,
              updated_ts=CURRENT_TIMESTAMP
            """,
            (day, total, failures, unknown, retries_total, median, p95),
        )
        upserts += 1

    con.commit()
    con.close()
    print(f"SIGNALS_UPDATED: {upserts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
