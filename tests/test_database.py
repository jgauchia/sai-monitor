"""Tests for SQLite persistence, downsampling and retention."""

import tempfile
import time
import unittest
from datetime import datetime, timedelta

from database import Database
from sai_monitor import Reading, Sample, Status, parse_status


def make_sample(
    ts: datetime, input_voltage: float = 230.0, load: float = 20.0
) -> Sample:
    bits = "00001001"
    reading = Reading(
        input_voltage=input_voltage,
        input_fault_voltage=230.0,
        output_voltage=230.0,
        load_percent=load,
        input_frequency=50.0,
        battery_voltage=13.0,
        temperature=28.0,
        status_bits=bits,
    )
    return Sample(
        timestamp=ts,
        port="/dev/null",
        reading=reading,
        status=parse_status(bits),
        battery_charge_direct=None,
        battery_charge_estimated=90.0,
        charge_command=None,
        output_power_w=72.0,
        runtime_minutes=30.0,
    )


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/sai.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_samples_history_and_outages(self) -> None:
        db = Database(self.path)
        now = datetime.now().astimezone()

        for offset in (3, 2, 1):
            db.insert_sample(
                make_sample(
                    now - timedelta(seconds=offset),
                    input_voltage=230.0 + offset,
                    load=20.0 + offset,
                )
            )

        history = db.history(1.0, 900)
        self.assertGreaterEqual(len(history["samples"]), 1)
        self.assertEqual(history["hours"], 1.0)
        self.assertGreaterEqual(history["bucket_s"], 1)
        first = history["samples"][0]
        self.assertIn("input_voltage", first)
        self.assertIn("battery_charge", first)
        self.assertEqual(first["utility_fail"], False)

        self.assertEqual(db.outages(), [])

    def test_prune_removes_old_rows(self) -> None:
        db = Database(self.path, retention_days=1)
        now = datetime.now().astimezone()
        db.insert_sample(make_sample(now - timedelta(days=2)))
        db.insert_sample(make_sample(now))

        history = db.history(24.0 * 90, 5000)
        self.assertEqual(len(history["samples"]), 2)

        db.prune()
        history = db.history(24.0 * 90, 5000)
        self.assertEqual(len(history["samples"]), 1)

    def test_invalid_path_raises(self) -> None:
        with self.assertRaises(OSError):
            Database("/proc/does-not-exist/sai.db")


if __name__ == "__main__":
    unittest.main()
