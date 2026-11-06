"""Unit tests for the Megatec/Q1 parsing and estimation helpers."""

import unittest
from datetime import datetime, timedelta

from sai_monitor import (
    MonitorContext,
    OutageTracker,
    Reading,
    Sample,
    build_sample,
    classify_battery_state,
    estimate_charge,
    estimate_power,
    estimate_runtime,
    parse_charge,
    parse_f,
    parse_q1,
    parse_status,
)

Q1 = "(238.8 238.8 240.8 001 49.9 14.2 --.- 00001001"


def make_context() -> MonitorContext:
    return MonitorContext(
        port="/dev/null",
        name="Test UPS",
        ups_va=650,
        ups_watts=360,
        battery_ah=7,
        nominal_battery=12.0,
    )


def make_sample(
    timestamp: datetime,
    utility_fail: bool,
    voltage: float,
    charge: float,
) -> Sample:
    status_bits = ("1" if utility_fail else "0") + "0000000"
    reading = Reading(
        input_voltage=0.0 if utility_fail else 230.0,
        input_fault_voltage=230.0,
        output_voltage=230.0,
        load_percent=20.0,
        input_frequency=50.0,
        battery_voltage=voltage,
        temperature=None,
        status_bits=status_bits,
    )
    return Sample(
        timestamp=timestamp,
        port="/dev/null",
        reading=reading,
        status=parse_status(status_bits),
        battery_charge_direct=charge,
        battery_charge_estimated=None,
        charge_command="QBL",
        output_power_w=72.0,
        runtime_minutes=30.0,
    )


class ParsingTests(unittest.TestCase):
    def test_q1_parses_all_fields(self) -> None:
        reading = parse_q1(Q1)
        self.assertIsNotNone(reading)
        assert reading is not None
        self.assertEqual(reading.input_voltage, 238.8)
        self.assertEqual(reading.input_fault_voltage, 238.8)
        self.assertEqual(reading.output_voltage, 240.8)
        self.assertEqual(reading.load_percent, 1.0)
        self.assertEqual(reading.input_frequency, 49.9)
        self.assertEqual(reading.battery_voltage, 14.2)
        self.assertIsNone(reading.temperature)
        self.assertEqual(reading.status_bits, "00001001")

    def test_q1_rejects_invalid_frames(self) -> None:
        self.assertIsNone(parse_q1(""))
        self.assertIsNone(parse_q1("(238.8 238.8"))
        self.assertIsNone(parse_q1("garbage"))

    def test_f_rating(self) -> None:
        rating = parse_f("#230.0 002 12.00 50.0")
        self.assertIsNotNone(rating)
        assert rating is not None
        self.assertEqual(rating.input_voltage, 230.0)
        self.assertEqual(rating.input_current, 2.0)
        self.assertEqual(rating.battery_voltage, 12.0)
        self.assertEqual(rating.input_frequency, 50.0)

        self.assertIsNone(parse_f("I"))
        self.assertIsNone(parse_f("#230.0"))

    def test_status_bits(self) -> None:
        status = parse_status("00001001")
        self.assertFalse(status.utility_fail)
        self.assertFalse(status.battery_low)
        self.assertFalse(status.bypass)
        self.assertFalse(status.ups_failed)
        self.assertTrue(status.offline)
        self.assertFalse(status.test)
        self.assertFalse(status.shutdown)
        self.assertTrue(status.beeper)

        status = parse_status("11000000")
        self.assertTrue(status.utility_fail)
        self.assertTrue(status.battery_low)
        self.assertFalse(status.offline)

    def test_charge_replies(self) -> None:
        self.assertEqual(parse_charge("BL 87", "bl"), 87.0)
        self.assertEqual(parse_charge("(87", "bl"), 87.0)
        self.assertEqual(parse_charge("(230.0 14.2 25.0 64", "qbl"), 64.0)
        self.assertIsNone(parse_charge("BL?", "bl"))
        self.assertIsNone(parse_charge("(1 2 3 101", "qbl"))


def battery_state(
    voltage: str,
    bits: str = "00001001",
    nominal: float = 12.0,
) -> str:
    reading = parse_q1(
        f"(230.0 230.0 230.0 010 50.0 {voltage} --.- {bits}"
    )
    assert reading is not None
    return classify_battery_state(reading, parse_status(bits), nominal)


class BatteryStateTests(unittest.TestCase):
    def test_charging_and_charged(self) -> None:
        self.assertEqual(battery_state("14.2"), "charging")
        self.assertEqual(battery_state("13.4"), "charged")

    def test_recharging_after_outage(self) -> None:
        self.assertEqual(battery_state("12.5"), "recharging")
        self.assertEqual(battery_state("12.9"), "recharging")
        self.assertEqual(battery_state("13.0"), "charged")

    def test_discharging_and_low(self) -> None:
        on_battery = "10001001"
        self.assertEqual(battery_state("12.8", on_battery), "discharging")
        self.assertEqual(battery_state("11.5", on_battery), "low")
        self.assertEqual(battery_state("13.4", "01001001"), "low")

    def test_status_priority(self) -> None:
        self.assertEqual(battery_state("14.2", "00001101"), "test")
        self.assertEqual(battery_state("14.2", "00011001"), "fault")

    def test_unknown_without_voltage_or_nominal(self) -> None:
        self.assertEqual(battery_state("--.-"), "unknown")
        self.assertEqual(battery_state("14.2", nominal=None), "unknown")

    def test_thresholds_scale_with_nominal_voltage(self) -> None:
        self.assertEqual(battery_state("28.4", nominal=24.0), "charging")
        self.assertEqual(battery_state("26.8", nominal=24.0), "charged")
        self.assertEqual(battery_state("25.0", nominal=24.0), "recharging")

    def test_per_cell_voltage_is_normalized(self) -> None:
        self.assertEqual(battery_state("2.10"), "recharging")

    def test_explicit_thresholds_override_nominal(self) -> None:
        bits = "00001001"
        reading = parse_q1(
            f"(230.0 230.0 230.0 010 50.0 13.6 --.- {bits}"
        )
        assert reading is not None
        status = parse_status(bits)
        self.assertEqual(
            classify_battery_state(
                reading, status, 12.0, 11.0, 13.5, 14.0
            ),
            "charged",
        )


class EstimationTests(unittest.TestCase):
    def test_charge_estimate_and_clamp(self) -> None:
        reading = parse_q1(Q1)
        assert reading is not None

        self.assertAlmostEqual(estimate_charge(reading, 12.0), 100.0)
        self.assertIsNone(estimate_charge(reading, None))

        reading.battery_voltage = 12.0
        self.assertAlmostEqual(
            estimate_charge(reading, 12.0), 61.538, places=3
        )

        reading.battery_voltage = 10.0
        self.assertEqual(estimate_charge(reading, 12.0), 0.0)

    def test_power_and_runtime(self) -> None:
        self.assertEqual(estimate_power(50.0, 360.0), 180.0)
        self.assertIsNone(estimate_power(None, 360.0))

        runtime = estimate_runtime(180.0, 12.0, 7.0)
        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertAlmostEqual(runtime, 14.0, places=1)
        self.assertIsNone(estimate_runtime(0.0, 12.0, 7.0))
        self.assertIsNone(estimate_runtime(None, 12.0, 7.0))

    def test_build_sample_uses_voltage_estimate(self) -> None:
        reading = parse_q1(Q1)
        assert reading is not None
        sample = build_sample(None, reading, make_context())

        self.assertEqual(sample.charge_source, "estimated")
        self.assertEqual(sample.battery_charge_direct, None)
        self.assertAlmostEqual(sample.battery_charge or 0.0, 100.0)
        self.assertAlmostEqual(sample.output_power_w or 0.0, 3.6)
        self.assertAlmostEqual(sample.runtime_minutes or 0.0, 252.0)


class OutageTrackerTests(unittest.TestCase):
    def test_outage_lifecycle(self) -> None:
        tracker = OutageTracker()
        started = datetime.now().astimezone()

        sample = make_sample(started, True, 13.0, 90.0)
        self.assertIsNone(tracker.update(sample))
        self.assertEqual(tracker.outage_count, 1)
        self.assertIsNotNone(tracker.current)

        sample = make_sample(started + timedelta(seconds=10), True, 12.4, 80.0)
        self.assertIsNone(tracker.update(sample))

        sample = make_sample(
            started + timedelta(seconds=20), False, 13.4, 95.0
        )
        outage = tracker.update(sample)

        self.assertIsNotNone(outage)
        assert outage is not None
        self.assertEqual(outage.duration_s, 20.0)
        self.assertEqual(outage.min_voltage, 12.4)
        self.assertEqual(outage.min_charge, 80.0)
        self.assertEqual(outage.end_voltage, 13.4)
        self.assertEqual(tracker.current, None)
        self.assertIs(tracker.last_outage, outage)


if __name__ == "__main__":
    unittest.main()
