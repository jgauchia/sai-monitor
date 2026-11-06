"""Integration-style tests for the sampler lifecycle without hardware."""

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from database import Database
from sai_monitor import Reading, Sample, parse_status
from webapp import Sampler, Settings


def make_sample(
    timestamp: datetime, utility_fail: bool, voltage: float
) -> Sample:
    bits = ("1" if utility_fail else "0") + "0001001"
    reading = Reading(
        input_voltage=0.0 if utility_fail else 230.0,
        input_fault_voltage=230.0,
        output_voltage=230.0,
        load_percent=20.0,
        input_frequency=50.0,
        battery_voltage=voltage,
        temperature=None,
        status_bits=bits,
    )
    return Sample(
        timestamp=timestamp,
        port="/dev/null",
        reading=reading,
        status=parse_status(bits),
        battery_charge_direct=None,
        battery_charge_estimated=90.0,
        charge_command=None,
        output_power_w=72.0,
        runtime_minutes=30.0,
    )


class SettingsEnvTests(unittest.TestCase):
    def test_custom_ups_parameters_from_env(self) -> None:
        env = {
            "SAI_NAME": "Rack UPS 1500",
            "SAI_WEB_TITLE": "Rack Room Monitor",
            "SAI_UPS_VA": "1500",
            "SAI_UPS_WATTS": "900",
            "SAI_BATTERY_NOMINAL": "24",
            "SAI_BATTERY_AH": "9",
            "SAI_BATTERY_STATE_LOW_V": "23.5",
            "SAI_BATTERY_STATE_CHARGED_V": "26.0",
            "SAI_BATTERY_STATE_CHARGING_V": "27.2",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.name, "Rack UPS 1500")
        self.assertEqual(settings.web_title, "Rack Room Monitor")
        self.assertEqual(
            settings.public_config()["web_title"], "Rack Room Monitor"
        )
        self.assertEqual(settings.ups_va, 1500.0)
        self.assertEqual(settings.ups_watts, 900.0)
        self.assertEqual(settings.battery_nominal, 24.0)
        self.assertEqual(settings.battery_ah, 9.0)
        self.assertEqual(settings.battery_state_low, 23.5)
        self.assertEqual(settings.battery_state_charged, 26.0)
        self.assertEqual(settings.battery_state_charging, 27.2)

    def test_empty_optional_thresholds_are_derived(self) -> None:
        env = {
            "SAI_BATTERY_STATE_LOW_V": "",
            "SAI_BATTERY_STATE_CHARGED_V": "",
            "SAI_BATTERY_STATE_CHARGING_V": "",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertIsNone(settings.battery_state_low)
        self.assertIsNone(settings.battery_state_charged)
        self.assertIsNone(settings.battery_state_charging)

    def test_web_title_defaults_and_is_public(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.web_title, "UPS Monitor")
        self.assertEqual(
            settings.public_config()["web_title"], "UPS Monitor"
        )

        with patch.dict(
            os.environ, {"SAI_WEB_TITLE": "   "}, clear=True
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.web_title, "UPS Monitor")

    def test_invalid_state_threshold_order_is_rejected(self) -> None:
        env = {
            "SAI_BATTERY_STATE_LOW_V": "14",
            "SAI_BATTERY_STATE_CHARGED_V": "13",
            "SAI_BATTERY_STATE_CHARGING_V": "13.6",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()


class SamplerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            name="Test UPS",
            interval=0.05,
            db_path=f"{self.temp.name}/sai.db",
            retention_days=1,
            simulate=True,
        )
        self.store = Database(self.settings.db_path)
        self.sampler = Sampler(self.settings, self.store)

    def tearDown(self) -> None:
        self.sampler.stop()
        self.temp.cleanup()

    def test_start_stop_and_restart(self) -> None:
        self.sampler.start()
        time.sleep(0.25)

        status = self.sampler.status()
        self.assertTrue(status["running"])
        self.assertTrue(status["connected"])
        self.assertGreater(status["samples"], 0)
        self.assertIsNotNone(status["sample"])
        self.assertEqual(status["sample"]["battery_charge_source"], "estimated")

        self.assertTrue(self.sampler.stop())
        status = self.sampler.status()
        self.assertFalse(status["running"])
        self.assertFalse(status["connected"])
        self.assertEqual(status["error"], "Disconnected by the user")
        samples_after_stop = status["samples"]

        self.sampler.start()
        time.sleep(0.2)
        status = self.sampler.status()
        self.assertTrue(status["running"])
        self.assertTrue(status["connected"])
        self.assertGreater(status["samples"], samples_after_stop)

        self.assertTrue(self.sampler.stop())

    def test_stop_is_idempotent(self) -> None:
        self.sampler.start()
        time.sleep(0.1)
        self.assertTrue(self.sampler.stop())
        self.assertTrue(self.sampler.stop())
        self.assertFalse(self.sampler.status()["running"])

    def test_outage_is_persisted(self) -> None:
        started = datetime.now().astimezone()
        self.sampler._process_sample(
            make_sample(started, True, 13.0)
        )
        self.sampler._process_sample(
            make_sample(started + timedelta(seconds=5), True, 12.5)
        )
        self.sampler._process_sample(
            make_sample(started + timedelta(seconds=10), False, 13.4)
        )

        outages = self.store.outages()
        self.assertEqual(len(outages), 1)
        self.assertEqual(outages[0]["duration_s"], 10.0)
        self.assertEqual(outages[0]["min_voltage"], 12.5)
        self.assertEqual(
            self.sampler.status()["sample"]["session"]["outages"], 1
        )


if __name__ == "__main__":
    unittest.main()
