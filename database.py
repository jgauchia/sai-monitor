#!/usr/bin/env python3
"""SQLite persistence for the SAI monitor.

The database stores one row per Q1 sample and one row per finished grid
outage. It is intentionally small and dependency-free so the web container
only needs pyserial, FastAPI and uvicorn.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from sai_monitor import Outage, Sample


SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    input_voltage REAL,
    input_fault_voltage REAL,
    output_voltage REAL,
    load_percent REAL,
    input_frequency REAL,
    battery_voltage REAL,
    temperature REAL,
    status_bits TEXT,
    utility_fail INTEGER NOT NULL,
    battery_low INTEGER NOT NULL,
    bypass INTEGER NOT NULL,
    ups_failed INTEGER NOT NULL,
    offline INTEGER NOT NULL,
    test INTEGER NOT NULL,
    shutdown INTEGER NOT NULL,
    beeper INTEGER NOT NULL,
    battery_charge REAL,
    battery_charge_source TEXT,
    output_power_w REAL,
    runtime_minutes REAL
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS outages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL NOT NULL,
    end_ts REAL,
    duration_s REAL,
    start_voltage REAL,
    end_voltage REAL,
    min_voltage REAL,
    start_charge REAL,
    end_charge REAL,
    min_charge REAL,
    avg_load_percent REAL,
    avg_output_power_w REAL,
    energy_used_wh_estimate REAL
);

CREATE INDEX IF NOT EXISTS idx_outages_start_ts ON outages(start_ts);
"""


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


class Database:
    """Small SQLite repository.

    A fresh connection is opened for each operation. WAL mode allows the HTTP
    readers to keep working while the sampler thread writes a new row.
    """

    def __init__(self, path: str, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    def insert_sample(self, sample: Sample) -> None:
        reading = sample.reading
        status = sample.status
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO samples (
                    ts, input_voltage, input_fault_voltage, output_voltage,
                    load_percent, input_frequency, battery_voltage,
                    temperature, status_bits, utility_fail, battery_low,
                    bypass, ups_failed, offline, test, shutdown, beeper,
                    battery_charge, battery_charge_source, output_power_w,
                    runtime_minutes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?)
                """,
                (
                    sample.timestamp.timestamp(),
                    reading.input_voltage,
                    reading.input_fault_voltage,
                    reading.output_voltage,
                    reading.load_percent,
                    reading.input_frequency,
                    reading.battery_voltage,
                    reading.temperature,
                    reading.status_bits,
                    int(status.utility_fail),
                    int(status.battery_low),
                    int(status.bypass),
                    int(status.ups_failed),
                    int(status.offline),
                    int(status.test),
                    int(status.shutdown),
                    int(status.beeper),
                    _round(sample.battery_charge, 1),
                    sample.charge_source,
                    _round(sample.output_power_w, 1),
                    _round(sample.runtime_minutes, 0),
                ),
            )

    def insert_outage(self, outage: Outage) -> None:
        if outage.duration_s is None:
            return

        avg_load = (
            sum(outage.loads) / len(outage.loads) if outage.loads else None
        )
        avg_power = (
            sum(outage.powers) / len(outage.powers) if outage.powers else None
        )
        energy_wh = (
            avg_power * outage.duration_s / 3600.0
            if avg_power is not None
            else None
        )

        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO outages (
                    start_ts, end_ts, duration_s, start_voltage,
                    end_voltage, min_voltage, start_charge, end_charge,
                    min_charge, avg_load_percent, avg_output_power_w,
                    energy_used_wh_estimate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outage.start.timestamp(),
                    (
                        outage.end.timestamp()
                        if outage.end is not None
                        else None
                    ),
                    _round(outage.duration_s, 1),
                    _round(outage.start_voltage, 2),
                    _round(outage.end_voltage, 2),
                    _round(outage.min_voltage, 2),
                    _round(outage.start_charge, 1),
                    _round(outage.end_charge, 1),
                    _round(outage.min_charge, 1),
                    _round(avg_load, 1),
                    _round(avg_power, 1),
                    _round(energy_wh, 1),
                ),
            )

    def history(
        self, hours: float, max_points: int = 600
    ) -> dict[str, Any]:
        """Return downsampled samples for the requested time range."""
        hours = max(0.001, min(hours, 24 * 90))
        max_points = max(10, min(max_points, 5000))
        since = time.time() - hours * 3600
        bucket_s = max(1, int(hours * 3600 / max_points))

        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    CAST(ts / ? AS INTEGER) * ? AS bucket,
                    AVG(input_voltage) AS input_voltage,
                    AVG(input_fault_voltage) AS input_fault_voltage,
                    AVG(output_voltage) AS output_voltage,
                    AVG(load_percent) AS load_percent,
                    AVG(input_frequency) AS input_frequency,
                    AVG(battery_voltage) AS battery_voltage,
                    AVG(temperature) AS temperature,
                    AVG(battery_charge) AS battery_charge,
                    AVG(output_power_w) AS output_power_w,
                    AVG(runtime_minutes) AS runtime_minutes,
                    MAX(utility_fail) AS utility_fail,
                    MAX(battery_low) AS battery_low,
                    MAX(bypass) AS bypass,
                    MAX(ups_failed) AS ups_failed
                FROM samples
                WHERE ts >= ?
                GROUP BY bucket
                ORDER BY bucket
                """,
                (bucket_s, bucket_s, since),
            ).fetchall()

        samples = []
        for row in rows:
            samples.append(
                {
                    "t": int(row["bucket"]),
                    "input_voltage": _round(row["input_voltage"], 2),
                    "input_fault_voltage": _round(
                        row["input_fault_voltage"], 2
                    ),
                    "output_voltage": _round(row["output_voltage"], 2),
                    "load_percent": _round(row["load_percent"], 2),
                    "input_frequency": _round(
                        row["input_frequency"], 2
                    ),
                    "battery_voltage": _round(row["battery_voltage"], 2),
                    "temperature": _round(row["temperature"], 2),
                    "battery_charge": _round(row["battery_charge"], 1),
                    "output_power_w": _round(row["output_power_w"], 1),
                    "runtime_minutes": _round(
                        row["runtime_minutes"], 0
                    ),
                    "utility_fail": bool(row["utility_fail"]),
                    "battery_low": bool(row["battery_low"]),
                    "bypass": bool(row["bypass"]),
                    "ups_failed": bool(row["ups_failed"]),
                }
            )

        return {
            "hours": hours,
            "bucket_s": bucket_s,
            "samples": samples,
        }

    def outages(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM outages
                ORDER BY start_ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        result = []
        for row in rows:
            result.append(
                {
                    "id": row["id"],
                    "start_ts": row["start_ts"],
                    "end_ts": row["end_ts"],
                    "duration_s": row["duration_s"],
                    "start_voltage": row["start_voltage"],
                    "end_voltage": row["end_voltage"],
                    "min_voltage": row["min_voltage"],
                    "start_charge": row["start_charge"],
                    "end_charge": row["end_charge"],
                    "min_charge": row["min_charge"],
                    "avg_load_percent": row["avg_load_percent"],
                    "avg_output_power_w": row["avg_output_power_w"],
                    "energy_used_wh_estimate": row[
                        "energy_used_wh_estimate"
                    ],
                }
            )
        return result

    def prune(self) -> None:
        if self.retention_days <= 0:
            return
        cutoff = time.time() - self.retention_days * 86400
        with self._connection() as conn:
            conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            conn.execute(
                "DELETE FROM outages WHERE end_ts IS NOT NULL AND end_ts < ?",
                (cutoff,),
            )
