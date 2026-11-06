#!/usr/bin/env python3
r"""
Megatec/Q1 UPS monitor.

The UPS is queried with "Q1\r" and replies with a frame such as:

    (MMM.M NNN.N PPP.P QQQ RR.R SS.S TT.T b7b6b5b4b3b2b1b0\r

Fields:
    1. Input voltage (V)
    2. Input fault voltage (V)
    3. Output voltage (V)
    4. Output load (% of rated current)
    5. Input frequency (Hz)
    6. Battery voltage (V; per cell on online UPSes)
    7. Temperature (C)
    8. Status bits

Battery charge is not part of Q1. The monitor probes the Megatec commands BL?,
BL, QBL and QBV. If the UPS supports one of them, the real percentage is shown.
Otherwise the charge is estimated from the battery voltage and the nominal pack
voltage (from F, or from --battery-nominal when F is not supported) and marked
as "(estimated)".

The Woxter UPS 650 VA uses one 12 V / 7 Ah battery, so 12 V is the default
nominal voltage.

Additional features:
    - Command/support diagnostics (--diagnose).
    - Estimated output power and runtime.
    - Grid outage detection with a summary when mains returns.
    - CSV logging (--log) and JSON Lines output (--json).
    - HTTP API with /status and /metrics (--http).
    - Automatic reconnect if the serial port fails.

Usage:
    python3 sai_monitor.py                  # /dev/ttyUSB0, one reading per second
    python3 sai_monitor.py /dev/ttyUSB1
    python3 sai_monitor.py --once           # one reading and exit
    python3 sai_monitor.py --diagnose       # probe supported commands
    python3 sai_monitor.py --log sai.csv --http 8080
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

try:
    import serial
except ImportError:
    sys.exit("pyserial is required. Install it with: python3 -m pip install pyserial")

VERSION = "2.0"

BAUD = 2400
QUERY_TIMEOUT = 2.0          # maximum wait for the Q1 reply
CHARGE_TIMEOUT = 1.0         # maximum wait for a battery charge reply
PROBE_TIMEOUT = 1.0          # maximum wait per probe command
SERIAL_TIMEOUT = 0.25        # partial read timeout
RECONNECT_DELAY = 2.0        # seconds between reconnect attempts
MAX_CONSECUTIVE_ERRORS = 5   # reconnect after this many failed readings
LABEL_WIDTH = 28

DEFAULT_BATTERY_NOMINAL = 12.0  # Woxter UPS 650 VA: one 12 V battery
DEFAULT_BATTERY_AH = 7.0
DEFAULT_UPS_VA = 650.0
DEFAULT_UPS_WATTS = 360.0

# Battery state thresholds, relative to the nominal pack voltage. For a 12 V
# battery these are 11.6 V (low), 13.0 V (float/charged) and 13.6 V (charging).
BATTERY_STATE_LOW_RATIO = 0.97
BATTERY_STATE_CHARGED_RATIO = 130 / 120
BATTERY_STATE_CHARGING_RATIO = 1.133

# Runtime is a very rough estimate. This factor accounts for usable depth of
# discharge, inverter efficiency and battery age. RUNTIME_IDLE_WATTS models the
# UPS own consumption so that a 1 % load does not produce an absurd runtime.
RUNTIME_EFFECTIVE_FACTOR = 0.5
RUNTIME_IDLE_WATTS = 10.0
RUNTIME_MAX_MINUTES = 24 * 60


@dataclass
class Reading:
    input_voltage: Optional[float]
    input_fault_voltage: Optional[float]
    output_voltage: Optional[float]
    load_percent: Optional[float]
    input_frequency: Optional[float]
    battery_voltage: Optional[float]
    temperature: Optional[float]
    status_bits: str


@dataclass
class Status:
    utility_fail: bool       # b0: mains failure, UPS is on battery
    battery_low: bool        # b1: battery low
    bypass: bool             # b2: bypass / boost / buck active
    ups_failed: bool         # b3: UPS failure
    offline: bool            # b4: 1 = offline/standby, 0 = online
    test: bool               # b5: battery test in progress
    shutdown: bool           # b6: shutdown active
    beeper: bool             # b7: beeper enabled


@dataclass(frozen=True)
class ChargeQuery:
    command: bytes
    mode: str


@dataclass
class DeviceRating:
    input_voltage: Optional[float]
    input_current: Optional[float]
    battery_voltage: Optional[float]
    input_frequency: Optional[float]
    raw: str


@dataclass
class MonitorContext:
    port: str
    name: str
    ups_va: float
    ups_watts: float
    battery_ah: float
    nominal_battery: Optional[float]
    nominal_source: str = "config"
    battery_low: Optional[float] = None
    battery_high: Optional[float] = None
    battery_state_low: Optional[float] = None
    battery_state_charged: Optional[float] = None
    battery_state_charging: Optional[float] = None
    nominal_input_voltage: Optional[float] = None
    nominal_input_current: Optional[float] = None
    nominal_input_frequency: Optional[float] = None
    charge_query: Optional[ChargeQuery] = None
    device_info: Optional[str] = None


@dataclass
class Sample:
    timestamp: datetime
    port: str
    reading: Reading
    status: Status
    battery_charge_direct: Optional[float]
    battery_charge_estimated: Optional[float]
    charge_command: Optional[str]
    output_power_w: Optional[float]
    runtime_minutes: Optional[float]

    @property
    def battery_charge(self) -> Optional[float]:
        if self.battery_charge_direct is not None:
            return self.battery_charge_direct
        return self.battery_charge_estimated

    @property
    def charge_source(self) -> str:
        if self.battery_charge_direct is not None:
            return "direct"
        if self.battery_charge_estimated is not None:
            return "estimated"
        return "none"


@dataclass
class Outage:
    start: datetime
    start_voltage: Optional[float]
    start_charge: Optional[float]
    last_voltage: Optional[float] = None
    min_voltage: Optional[float] = None
    min_charge: Optional[float] = None
    end: Optional[datetime] = None
    end_voltage: Optional[float] = None
    end_charge: Optional[float] = None
    duration_s: Optional[float] = None
    powers: list[float] = field(default_factory=list)
    loads: list[float] = field(default_factory=list)

    def add_sample(self, sample: Sample) -> None:
        voltage = sample.reading.battery_voltage
        charge = sample.battery_charge

        self.last_voltage = voltage
        if voltage is not None:
            self.min_voltage = (
                voltage if self.min_voltage is None else min(self.min_voltage, voltage)
            )
        if charge is not None:
            self.min_charge = (
                charge if self.min_charge is None else min(self.min_charge, charge)
            )
        if sample.output_power_w is not None:
            self.powers.append(sample.output_power_w)
        if sample.reading.load_percent is not None:
            self.loads.append(sample.reading.load_percent)


# Battery charge queries, ordered from most to least common in Megatec devices.
#   BL? -> "098"                 (Megatec / Ippon / Salicru)
#   BL  -> "BL100"               (Hunnox)
#   QBL -> "(026.5 02 01 068 255" (Voltronic)
#   QBV -> same as QBL on some models
CHARGE_QUERIES = (
    ChargeQuery(b"BL?", "bl"),
    ChargeQuery(b"BL", "bl"),
    ChargeQuery(b"QBL", "qbl"),
    ChargeQuery(b"QBV", "qbl"),
)

DIAGNOSTIC_COMMANDS = (
    ("Q1", b"Q1\r"),
    ("F", b"F\r"),
    ("I", b"I\r"),
    ("BL?", b"BL?\r"),
    ("BL", b"BL\r"),
    ("QBL", b"QBL\r"),
    ("QBV", b"QBV\r"),
)


def parse_status(bits: str) -> Status:
    """Decode the eight status bits of a Q1 frame.

    The first character is b0 (mains failure) and the last one is b7 (beeper),
    matching the de-facto Megatec/Q1 implementation.
    """
    bits = "".join(c for c in bits if c in "01").ljust(8, "0")[:8]
    return Status(
        utility_fail=bits[0] == "1",
        battery_low=bits[1] == "1",
        bypass=bits[2] == "1",
        ups_failed=bits[3] == "1",
        offline=bits[4] == "1",
        test=bits[5] == "1",
        shutdown=bits[6] == "1",
        beeper=bits[7] == "1",
    )


def parse_q1(line: str) -> Optional[Reading]:
    """Convert a Q1 frame into a Reading; return None if it is not valid."""
    line = line.strip()
    if not line.startswith("("):
        return None

    fields = line[1:].split()
    if len(fields) < 8:
        return None

    def number(index: int) -> Optional[float]:
        try:
            return float(fields[index])
        except (ValueError, IndexError):
            return None

    return Reading(
        input_voltage=number(0),
        input_fault_voltage=number(1),
        output_voltage=number(2),
        load_percent=number(3),
        input_frequency=number(4),
        battery_voltage=number(5),
        temperature=number(6),
        status_bits=fields[7],
    )


def parse_f(line: str) -> Optional[DeviceRating]:
    """Parse the F rating reply: #230.0 000 012.0 50.0."""
    line = line.strip()
    if not line.startswith("#"):
        return None

    fields = line[1:].split()
    if len(fields) < 4:
        return None

    def number(index: int) -> Optional[float]:
        try:
            return float(fields[index])
        except (ValueError, IndexError):
            return None

    return DeviceRating(
        input_voltage=number(0),
        input_current=number(1),
        battery_voltage=number(2),
        input_frequency=number(3),
        raw=line,
    )


def parse_info(line: str) -> Optional[str]:
    """Return the text part of an I reply, or None for empty/echo replies."""
    line = line.strip()
    if not line.startswith("#"):
        return None
    text = line[1:].strip()
    if not text or text.upper() in {"NAK", "N/A"}:
        return None
    return text


def query_q1(port: serial.Serial) -> str:
    """Send Q1 and return the first reply starting with '('."""
    port.reset_input_buffer()
    port.write(b"Q1\r")
    port.flush()

    deadline = time.monotonic() + QUERY_TIMEOUT
    while time.monotonic() < deadline:
        raw = port.read_until(b"\r")
        if not raw:
            continue
        line = raw.decode("ascii", errors="replace").strip()
        if line.startswith("("):
            return line
    return ""


def parse_charge(response: str, mode: str) -> Optional[float]:
    """Extract a battery percentage from BL?/BL/QBL/QBV replies."""
    text = response.strip()

    if mode == "bl":
        text = text.strip("() ")
        if text.upper().startswith("BL"):
            text = text[2:].strip(" ()")
        text = text.rstrip("%").strip()
        if not text.isdigit():
            return None
        value = int(text)
    else:
        if not text.startswith("("):
            return None
        fields = text[1:].split()
        if len(fields) < 4:
            return None
        try:
            value = int(float(fields[3]))
        except ValueError:
            return None

    if 0 <= value <= 100:
        return float(value)
    return None


def query_charge(
    port: serial.Serial, query: ChargeQuery, timeout: float = CHARGE_TIMEOUT
) -> Optional[float]:
    """Query battery charge; return None if the UPS does not support it."""
    port.reset_input_buffer()
    port.write(query.command + b"\r")
    port.flush()

    command_echo = query.command.decode("ascii").upper()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = port.read_until(b"\r")
        if not raw:
            continue
        line = raw.decode("ascii", errors="replace").strip()
        # Many UPSes echo unsupported commands verbatim.
        if line.upper() == command_echo:
            return None
        value = parse_charge(line, query.mode)
        if value is not None:
            return value
    return None


def detect_charge_query(port: serial.Serial) -> Optional[ChargeQuery]:
    """Probe once which battery charge command the UPS accepts."""
    for query in CHARGE_QUERIES:
        if query_charge(port, query, timeout=PROBE_TIMEOUT) is not None:
            return query
    return None


def query_f(port: serial.Serial) -> Optional[DeviceRating]:
    """Query the UPS rating with F."""
    port.reset_input_buffer()
    port.write(b"F\r")
    port.flush()

    deadline = time.monotonic() + CHARGE_TIMEOUT
    while time.monotonic() < deadline:
        raw = port.read_until(b"\r")
        if not raw:
            continue
        line = raw.decode("ascii", errors="replace").strip()
        if line.upper() == "F":
            return None
        rating = parse_f(line)
        if rating is not None:
            return rating
    return None


def query_info(port: serial.Serial) -> Optional[str]:
    """Query the UPS identification string with I."""
    port.reset_input_buffer()
    port.write(b"I\r")
    port.flush()

    deadline = time.monotonic() + CHARGE_TIMEOUT
    while time.monotonic() < deadline:
        raw = port.read_until(b"\r")
        if not raw:
            continue
        line = raw.decode("ascii", errors="replace").strip()
        if line.upper() == "I":
            return None
        info = parse_info(line)
        if info is not None:
            return info
    return None


def raw_query(port: serial.Serial, command: bytes, timeout: float = 1.0) -> list[str]:
    """Send a command and collect every reply line until the timeout."""
    port.reset_input_buffer()
    port.write(command)
    port.flush()

    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while time.monotonic() < deadline:
        raw = port.read_until(b"\r")
        if not raw:
            continue
        line = raw.decode("ascii", errors="replace").strip()
        if line:
            lines.append(line)
    return lines


def pack_battery_voltage(
    voltage: float, nominal_battery: Optional[float]
) -> float:
    """Convert a per-cell reading to the full pack voltage when needed.

    Some online UPSes report battery voltage per cell. A value clearly below a
    quarter of the nominal pack voltage is treated as a per-cell reading.
    """
    if nominal_battery is not None and nominal_battery > 0:
        if voltage < nominal_battery / 4:
            return voltage * nominal_battery / 2.0
    return voltage


def classify_battery_state(
    reading: Reading,
    status: Status,
    nominal_battery: Optional[float],
    low_voltage: Optional[float] = None,
    charged_voltage: Optional[float] = None,
    charging_voltage: Optional[float] = None,
) -> str:
    """Classify the battery state from Q1 voltage and status bits.

    Possible values: ``charging``, ``charged``, ``recharging``,
    ``discharging``, ``low``, ``test``, ``fault`` and ``unknown``.

    Voltage thresholds scale with the nominal pack voltage. For a 12 V battery
    they are approximately 11.6 V (low), 13.0 V (charged/float) and 13.6 V
    (charging). Explicit thresholds override the scaled defaults. Status bits
    always take precedence over voltage.
    """
    if status.ups_failed:
        return "fault"
    if status.test:
        return "test"

    nominal = (
        nominal_battery
        if nominal_battery is not None and nominal_battery > 0
        else None
    )
    voltage = reading.battery_voltage
    if voltage is not None and nominal is not None:
        voltage = pack_battery_voltage(voltage, nominal)

    if low_voltage is None and nominal is not None:
        low_voltage = nominal * BATTERY_STATE_LOW_RATIO
    if charged_voltage is None and nominal is not None:
        charged_voltage = nominal * BATTERY_STATE_CHARGED_RATIO
    if charging_voltage is None and nominal is not None:
        charging_voltage = nominal * BATTERY_STATE_CHARGING_RATIO

    if status.battery_low or (
        voltage is not None
        and low_voltage is not None
        and voltage < low_voltage
    ):
        return "low"

    if status.utility_fail:
        return "discharging"

    if (
        voltage is None
        or charged_voltage is None
        or charging_voltage is None
    ):
        return "unknown"

    if voltage >= charging_voltage:
        return "charging"
    if voltage >= charged_voltage:
        return "charged"
    return "recharging"


def sample_battery_state(sample: Sample, context: MonitorContext) -> str:
    """Return the classified battery state for a sample and its context."""
    return classify_battery_state(
        sample.reading,
        sample.status,
        context.nominal_battery,
        context.battery_state_low,
        context.battery_state_charged,
        context.battery_state_charging,
    )


def estimate_charge(
    reading: Reading,
    nominal_battery: Optional[float],
    low_voltage: Optional[float] = None,
    high_voltage: Optional[float] = None,
) -> Optional[float]:
    """Estimate state of charge from voltage (NUT/blazer-style formula)."""
    if nominal_battery is None or nominal_battery <= 0:
        return None
    if reading.battery_voltage is None:
        return None

    voltage = pack_battery_voltage(reading.battery_voltage, nominal_battery)

    low = low_voltage if low_voltage is not None else nominal_battery * 104 / 120
    high = high_voltage if high_voltage is not None else nominal_battery * 130 / 120
    if high <= low:
        return None

    charge = (voltage - low) / (high - low) * 100
    return max(0.0, min(100.0, charge))


def estimate_power(
    load_percent: Optional[float], ups_watts: float
) -> Optional[float]:
    """Estimate output power from the Q1 load percentage."""
    if load_percent is None:
        return None
    return max(0.0, load_percent) * ups_watts / 100.0


def estimate_runtime(
    output_power_w: Optional[float],
    nominal_battery: Optional[float],
    battery_ah: float,
) -> Optional[float]:
    """Very rough runtime estimate in minutes.

    This is not a replacement for a real runtime calibration. It only gives a
    ballpark figure using the nominal battery energy and a fixed efficiency.
    """
    if output_power_w is None or output_power_w <= 0:
        return None
    if nominal_battery is None or nominal_battery <= 0 or battery_ah <= 0:
        return None

    usable_wh = nominal_battery * battery_ah * RUNTIME_EFFECTIVE_FACTOR
    effective_power = max(output_power_w, RUNTIME_IDLE_WATTS)
    minutes = usable_wh / effective_power * 60
    return min(RUNTIME_MAX_MINUTES, minutes)


class OutageTracker:
    """Track grid outages and per-session battery minima."""

    def __init__(self) -> None:
        self.started_at = datetime.now().astimezone()
        self.current: Optional[Outage] = None
        self.last_outage: Optional[Outage] = None
        self.outage_count = 0
        self.min_battery_voltage: Optional[float] = None
        self.min_battery_charge: Optional[float] = None

    def update(self, sample: Sample) -> Optional[Outage]:
        """Update the tracker and return a finished outage, if any."""
        voltage = sample.reading.battery_voltage
        if voltage is not None:
            self.min_battery_voltage = (
                voltage
                if self.min_battery_voltage is None
                else min(self.min_battery_voltage, voltage)
            )

        charge = sample.battery_charge
        if charge is not None:
            self.min_battery_charge = (
                charge
                if self.min_battery_charge is None
                else min(self.min_battery_charge, charge)
            )

        if sample.status.utility_fail:
            if self.current is None:
                self.current = Outage(
                    start=sample.timestamp,
                    start_voltage=voltage,
                    start_charge=charge,
                )
                self.outage_count += 1
            self.current.add_sample(sample)
            return None

        if self.current is not None:
            outage = self.current
            self.current = None
            outage.end = sample.timestamp
            outage.end_voltage = voltage
            outage.end_charge = charge
            outage.duration_s = max(
                0.0, (outage.end - outage.start).total_seconds()
            )
            self.last_outage = outage
            return outage

        return None


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def outage_to_dict(outage: Outage) -> dict[str, Any]:
    duration_s = outage.duration_s or 0.0
    avg_load = (
        sum(outage.loads) / len(outage.loads) if outage.loads else None
    )
    avg_power = (
        sum(outage.powers) / len(outage.powers) if outage.powers else None
    )
    energy_wh = (
        avg_power * duration_s / 3600.0
        if avg_power is not None and duration_s > 0
        else None
    )
    return {
        "start": outage.start.isoformat(timespec="seconds"),
        "end": (
            outage.end.isoformat(timespec="seconds")
            if outage.end is not None
            else None
        ),
        "duration_s": _round(duration_s, 1),
        "start_voltage": _round(outage.start_voltage, 2),
        "end_voltage": _round(outage.end_voltage, 2),
        "min_voltage": _round(outage.min_voltage, 2),
        "start_charge": _round(outage.start_charge, 1),
        "end_charge": _round(outage.end_charge, 1),
        "min_charge": _round(outage.min_charge, 1),
        "avg_load_percent": _round(avg_load, 1),
        "avg_output_power_w": _round(avg_power, 1),
        "energy_used_wh_estimate": _round(energy_wh, 1),
    }


def sample_to_dict(
    sample: Sample, context: MonitorContext, tracker: OutageTracker
) -> dict[str, Any]:
    reading = sample.reading
    status = sample.status
    battery_state = sample_battery_state(sample, context)
    return {
        "event": "sample",
        "timestamp": sample.timestamp.isoformat(timespec="seconds"),
        "timestamp_unix": round(sample.timestamp.timestamp(), 3),
        "ups_name": context.name,
        "port": sample.port,
        "input_voltage": _round(reading.input_voltage),
        "input_fault_voltage": _round(reading.input_fault_voltage),
        "output_voltage": _round(reading.output_voltage),
        "load_percent": _round(reading.load_percent),
        "input_frequency": _round(reading.input_frequency),
        "battery_voltage": _round(reading.battery_voltage),
        "temperature": _round(reading.temperature),
        "status_bits": reading.status_bits,
        "status": {
            "utility_fail": status.utility_fail,
            "battery_low": status.battery_low,
            "bypass": status.bypass,
            "ups_failed": status.ups_failed,
            "offline": status.offline,
            "test": status.test,
            "shutdown": status.shutdown,
            "beeper": status.beeper,
        },
        "battery_state": battery_state,
        "battery_charge": _round(sample.battery_charge, 1),
        "battery_charge_source": sample.charge_source,
        "battery_charge_direct": _round(sample.battery_charge_direct, 1),
        "battery_charge_estimated": _round(
            sample.battery_charge_estimated, 1
        ),
        "battery_charge_command": sample.charge_command,
        "output_power_w": _round(sample.output_power_w, 1),
        "runtime_minutes": _round(sample.runtime_minutes, 0),
        "nominal_battery_voltage": _round(context.nominal_battery, 2),
        "nominal_battery_source": context.nominal_source,
        "nominal_input_voltage": _round(
            context.nominal_input_voltage, 1
        ),
        "nominal_input_current": _round(
            context.nominal_input_current, 2
        ),
        "nominal_input_frequency": _round(
            context.nominal_input_frequency, 1
        ),
        "ups_rating_va": context.ups_va,
        "ups_rating_watts": context.ups_watts,
        "battery_capacity_ah": context.battery_ah,
        "device_info": context.device_info,
        "session": {
            "started_at": tracker.started_at.isoformat(timespec="seconds"),
            "outages": tracker.outage_count,
            "outage_active": tracker.current is not None,
            "min_battery_voltage": _round(
                tracker.min_battery_voltage, 2
            ),
            "min_battery_charge": _round(
                tracker.min_battery_charge, 1
            ),
            "last_outage": (
                outage_to_dict(tracker.last_outage)
                if tracker.last_outage is not None
                else None
            ),
        },
    }


def error_event(port: str, code: str, message: str) -> dict[str, Any]:
    return {
        "event": "error",
        "timestamp": now_iso(),
        "port": port,
        "error": code,
        "message": message,
    }


CSV_FIELDS = (
    "timestamp",
    "port",
    "input_voltage",
    "input_fault_voltage",
    "output_voltage",
    "load_percent",
    "input_frequency",
    "battery_voltage",
    "temperature",
    "status_bits",
    "utility_fail",
    "battery_low",
    "bypass",
    "ups_failed",
    "offline",
    "test",
    "shutdown",
    "beeper",
    "battery_charge",
    "battery_charge_source",
    "battery_charge_direct",
    "battery_charge_estimated",
    "output_power_w",
    "runtime_minutes",
)


class CsvLogger:
    """Append one CSV row per sample."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        self._file = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=CSV_FIELDS)
        if new_file:
            self._writer.writeheader()
            self._file.flush()

    def write(self, sample: Sample) -> None:
        reading = sample.reading
        status = sample.status
        self._writer.writerow(
            {
                "timestamp": sample.timestamp.isoformat(timespec="seconds"),
                "port": sample.port,
                "input_voltage": reading.input_voltage,
                "input_fault_voltage": reading.input_fault_voltage,
                "output_voltage": reading.output_voltage,
                "load_percent": reading.load_percent,
                "input_frequency": reading.input_frequency,
                "battery_voltage": reading.battery_voltage,
                "temperature": reading.temperature,
                "status_bits": reading.status_bits,
                "utility_fail": int(status.utility_fail),
                "battery_low": int(status.battery_low),
                "bypass": int(status.bypass),
                "ups_failed": int(status.ups_failed),
                "offline": int(status.offline),
                "test": int(status.test),
                "shutdown": int(status.shutdown),
                "beeper": int(status.beeper),
                "battery_charge": _round(sample.battery_charge, 1),
                "battery_charge_source": sample.charge_source,
                "battery_charge_direct": _round(
                    sample.battery_charge_direct, 1
                ),
                "battery_charge_estimated": _round(
                    sample.battery_charge_estimated, 1
                ),
                "output_power_w": _round(sample.output_power_w, 1),
                "runtime_minutes": _round(sample.runtime_minutes, 0),
            }
        )
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class MonitorState:
    """Latest sample shared with the HTTP server."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Optional[dict[str, Any]] = None
        self.samples = 0

    def update(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._latest = payload
            self.samples += 1

    def snapshot(self) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._latest

    def metrics(self) -> str:
        with self._lock:
            payload = self._latest
            samples = self.samples

        lines = [
            "# HELP sai_up Whether the monitor has a sample (1 = yes).",
            "# TYPE sai_up gauge",
        ]
        if payload is None:
            lines.append("sai_up 0")
            return "\n".join(lines) + "\n"

        lines.extend(
            [
                "sai_up 1",
                "# TYPE sai_samples_total counter",
                f"sai_samples_total {samples}",
            ]
        )

        for name, key in (
            ("sai_input_voltage", "input_voltage"),
            ("sai_input_fault_voltage", "input_fault_voltage"),
            ("sai_output_voltage", "output_voltage"),
            ("sai_load_percent", "load_percent"),
            ("sai_input_frequency", "input_frequency"),
            ("sai_battery_voltage", "battery_voltage"),
            ("sai_temperature_celsius", "temperature"),
            ("sai_output_power_watts", "output_power_w"),
            ("sai_runtime_minutes", "runtime_minutes"),
        ):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lines.append(f"{name} {value}")

        status = payload.get("status", {})
        for name, key in (
            ("sai_status_utility_fail", "utility_fail"),
            ("sai_status_battery_low", "battery_low"),
            ("sai_status_bypass", "bypass"),
            ("sai_status_ups_failed", "ups_failed"),
            ("sai_status_offline", "offline"),
            ("sai_status_test", "test"),
            ("sai_status_shutdown", "shutdown"),
            ("sai_status_beeper", "beeper"),
        ):
            lines.append(f"{name} {1 if status.get(key) else 0}")

        charge = payload.get("battery_charge")
        source = payload.get("battery_charge_source") or "unknown"
        if charge is not None:
            lines.append(f'sai_battery_charge{{source="{source}"}} {charge}')
        lines.append(f'sai_battery_charge_source{{source="{source}"}} 1')

        session = payload.get("session", {})
        lines.append(
            f"sai_outages_total {session.get('outages', 0)}"
        )
        for name, key in (
            ("sai_battery_voltage_min", "min_battery_voltage"),
            ("sai_battery_charge_min", "min_battery_charge"),
        ):
            value = session.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lines.append(f"{name} {value}")

        timestamp = payload.get("timestamp_unix")
        if isinstance(timestamp, (int, float)):
            lines.append(
                f"sai_sample_age_seconds {max(0.0, time.time() - timestamp):.3f}"
            )
        return "\n".join(lines) + "\n"


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class StatusHandler(BaseHTTPRequestHandler):
    server_version = f"SAIMonitor/{VERSION}"

    def do_GET(self) -> None:  # noqa: N802 (http.server naming convention)
        path = self.path.split("?", 1)[0]
        state = getattr(self.server, "state", None)
        if state is None:
            self._send_json(500, {"error": "state unavailable"})
            return

        if path in ("/", "/status", "/status.json"):
            payload = state.snapshot()
            if payload is None:
                self._send_json(503, {"error": "no sample yet"})
            else:
                self._send_json(200, payload)
            return

        if path == "/health":
            payload = state.snapshot()
            if payload is None:
                self._send_json(503, {"ok": False, "error": "no sample yet"})
            else:
                self._send_json(200, {"ok": True})
            return

        if path == "/metrics":
            self._send_text(200, state.metrics())
            return

        self._send_json(404, {"error": "not found"})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type", "text/plain; version=0.0.4; charset=utf-8"
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the monitor output clean; HTTP requests are not logged.
        del format, args


def start_http_server(
    port: int, bind: str, state: MonitorState
) -> MonitorHTTPServer:
    httpd = MonitorHTTPServer((bind, port), StatusHandler)
    httpd.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=httpd.serve_forever, name="sai-http", daemon=True
    )
    thread.start()
    return httpd


def fmt(value: Optional[float], decimals: int, unit: str) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f} {unit}"


def yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def row(label: str, value: str) -> str:
    return f"  {label:<{LABEL_WIDTH}}: {value}"


def fmt_runtime(minutes: Optional[float]) -> str:
    if minutes is None:
        return "N/A"
    if minutes >= 120:
        return f"~{minutes / 60:.1f} h (rough)"
    return f"~{minutes:.0f} min (rough)"


def render(
    sample: Sample,
    context: MonitorContext,
    tracker: OutageTracker,
    clear: bool = True,
    footer: bool = True,
) -> None:
    if clear and sys.stdout.isatty():
        print("\033[2J\033[H", end="", flush=True)
    else:
        print()

    reading = sample.reading
    status = sample.status

    header = f"{context.name} · {sample.port} · {BAUD} baud · 8N1"
    if context.ups_va > 0:
        header += f" · {context.ups_va:.0f} VA / {context.ups_watts:.0f} W"
    print(header)
    print(f"{sample.timestamp:%Y-%m-%d %H:%M:%S}")
    print("─" * 62)

    print(row("Input voltage", fmt(reading.input_voltage, 1, "V")))
    print(row("Input fault voltage", fmt(reading.input_fault_voltage, 1, "V")))
    print(row("Output voltage", fmt(reading.output_voltage, 1, "V")))
    print(row("Output load", fmt(reading.load_percent, 0, "%")))

    if sample.output_power_w is not None:
        power_text = f"~{sample.output_power_w:.1f} W (estimated)"
    else:
        power_text = "N/A"
    print(row("Estimated output power", power_text))

    print(row("Input frequency", fmt(reading.input_frequency, 1, "Hz")))
    if status.offline:
        battery_text = fmt(reading.battery_voltage, 2, "V")
    else:
        battery_text = fmt(reading.battery_voltage, 2, "V/cell")
    print(row("Battery voltage", battery_text))

    if sample.battery_charge_direct is not None:
        charge_text = f"{sample.battery_charge_direct:.0f} %"
        source_text = f"Direct query ({sample.charge_command})"
    elif sample.battery_charge_estimated is not None:
        charge_text = f"~{sample.battery_charge_estimated:.0f} % (estimated)"
        if status.utility_fail:
            source_text = "Estimated from voltage (discharging)"
        else:
            source_text = "Estimated from voltage (mains/float)"
    else:
        charge_text = "N/A"
        source_text = "N/A"
    print(row("Battery charge", charge_text))
    print(row("Charge source", source_text))
    print(row("Battery state", sample_battery_state(sample, context).title()))
    print(row("Estimated runtime", fmt_runtime(sample.runtime_minutes)))
    print(row("Temperature", fmt(reading.temperature, 1, "°C")))

    if context.device_info:
        info = context.device_info
        if len(info) > 64:
            info = info[:61] + "..."
        print(row("Device info", info))

    print("─" * 62)

    if status.utility_fail:
        state_text = "On battery (mains failure)"
    elif status.bypass:
        state_text = "Bypass / AVR active"
    else:
        state_text = "Online (mains OK)"

    print(row("State", state_text))
    print(row("UPS type", "Offline / Standby" if status.offline else "Online"))
    print(row("Battery low", yes_no(status.battery_low)))
    print(row("Bypass / AVR", yes_no(status.bypass)))
    print(row("UPS failure", yes_no(status.ups_failed)))
    print(row("Test in progress", yes_no(status.test)))
    print(row("Shutdown active", yes_no(status.shutdown)))
    print(row("Beeper", "Enabled" if status.beeper else "Muted"))

    warnings: list[str] = []
    if reading.load_percent is not None and reading.load_percent > 100:
        warnings.append("output overload")
    if reading.temperature is not None and reading.temperature >= 45:
        warnings.append("high temperature")
    if warnings:
        print(row("Warnings", " · ".join(warnings)))

    nominal_parts: list[str] = []
    if context.nominal_input_voltage is not None:
        nominal_parts.append(fmt(context.nominal_input_voltage, 1, "V"))
    if context.nominal_input_frequency is not None:
        nominal_parts.append(fmt(context.nominal_input_frequency, 1, "Hz"))
    if context.nominal_input_current is not None:
        nominal_parts.append(fmt(context.nominal_input_current, 1, "A"))
    if nominal_parts:
        print(row("Nominal input", " · ".join(nominal_parts)))

    nominal_text = fmt(context.nominal_battery, 1, "V")
    nominal_text += " (from F)" if context.nominal_source == "F" else " (configured)"
    print(row("Nominal battery", nominal_text))
    print(row("Grid outages", str(tracker.outage_count)))
    if tracker.min_battery_voltage is not None:
        print(
            row(
                "Minimum battery voltage",
                fmt(tracker.min_battery_voltage, 2, "V"),
            )
        )
    if tracker.min_battery_charge is not None:
        print(
            row(
                "Minimum battery charge",
                f"{tracker.min_battery_charge:.0f} %",
            )
        )
    if (
        tracker.last_outage is not None
        and tracker.last_outage.duration_s is not None
    ):
        last = tracker.last_outage
        print(
            row(
                "Last grid outage",
                f"{last.duration_s:.0f} s · min "
                f"{fmt(last.min_voltage, 2, 'V')}",
            )
        )
    print(row("Status bits", reading.status_bits))

    if footer and sys.stdout.isatty():
        print()
        print("  Ctrl+C to quit")
    sys.stdout.flush()


def print_outage_summary(outage: Outage) -> None:
    print()
    print("  Grid outage ended")
    print(row("Duration", f"{outage.duration_s or 0:.0f} s"))
    print(
        row(
            "Battery voltage",
            f"{fmt(outage.start_voltage, 2, 'V')} -> "
            f"{fmt(outage.end_voltage, 2, 'V')} "
            f"(min {fmt(outage.min_voltage, 2, 'V')})",
        )
    )
    if (
        outage.start_charge is not None
        or outage.end_charge is not None
        or outage.min_charge is not None
    ):
        print(
            row(
                "Battery charge",
                f"{fmt(outage.start_charge, 0, '%')} -> "
                f"{fmt(outage.end_charge, 0, '%')} "
                f"(min {fmt(outage.min_charge, 0, '%')})",
            )
        )
    if outage.loads:
        avg_load = sum(outage.loads) / len(outage.loads)
        print(row("Average load", fmt(avg_load, 0, "%")))
    if outage.powers and outage.duration_s:
        avg_power = sum(outage.powers) / len(outage.powers)
        energy_wh = avg_power * outage.duration_s / 3600
        print(row("Estimated energy used", f"~{energy_wh:.1f} Wh"))
    sys.stdout.flush()


def open_serial(port_name: str) -> serial.Serial:
    return serial.Serial(
        port_name,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=SERIAL_TIMEOUT,
        write_timeout=1.0,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )


def command_supported(name: str, responses: list[str]) -> bool:
    if not responses:
        return False
    if name == "Q1":
        return any(line.startswith("(") for line in responses)
    if name in ("F", "I"):
        return any(line.startswith("#") for line in responses)
    mode = "bl" if name.startswith("BL") else "qbl"
    return any(parse_charge(line, mode) is not None for line in responses)


def run_diagnostics(args: argparse.Namespace) -> int:
    """Probe every known query and print the replies."""
    results: list[dict[str, Any]] = []
    try:
        with open_serial(args.port) as port:
            for name, command in DIAGNOSTIC_COMMANDS:
                responses = raw_query(port, command)
                results.append(
                    {
                        "command": name,
                        "responses": responses,
                        "supported": command_supported(name, responses),
                    }
                )
    except serial.SerialException as exc:
        print(f"Could not open {args.port}: {exc}", file=sys.stderr)
        return 1

    q1_supported = any(
        result["command"] == "Q1" and result["supported"]
        for result in results
    )
    charge_command = next(
        (
            result["command"]
            for result in results
            if result["supported"]
            and result["command"] in ("BL?", "BL", "QBL", "QBV")
        ),
        None,
    )
    f_result = next(
        (result for result in results if result["command"] == "F"), None
    )
    i_result = next(
        (result for result in results if result["command"] == "I"), None
    )
    f_supported = bool(f_result and f_result["supported"])
    i_supported = bool(i_result and i_result["supported"])

    f_raw = None
    f_rating = None
    if f_supported and f_result is not None:
        f_raw = next(
            (
                line
                for line in f_result["responses"]
                if line.startswith("#")
            ),
            None,
        )
        if f_raw is not None:
            f_rating = parse_f(f_raw)

    if args.json:
        for result in results:
            print(
                json.dumps(
                    {"event": "diagnostic", **result},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        print(
            json.dumps(
                {
                    "event": "diagnostic_summary",
                    "port": args.port,
                    "q1_supported": q1_supported,
                    "battery_charge_command": charge_command,
                    "f_supported": f_supported,
                    "f_response": f_raw,
                    "f_rating": (
                        {
                            "input_voltage": f_rating.input_voltage,
                            "input_current": f_rating.input_current,
                            "battery_voltage": f_rating.battery_voltage,
                            "input_frequency": f_rating.input_frequency,
                        }
                        if f_rating is not None
                        else None
                    ),
                    "i_supported": i_supported,
                    "device_info": (
                        " | ".join(i_result["responses"])
                        if i_supported and i_result is not None
                        else None
                    ),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0

    print(f"{args.name} · command diagnostic · {args.port} · {BAUD} baud")
    print("─" * 62)
    for result in results:
        responses = result["responses"]
        if not responses:
            value = "no response"
        elif result["supported"]:
            value = " | ".join(responses)
        else:
            value = "not supported (echo/NAK): " + " | ".join(responses)
        print(row(result["command"], value))
    print("─" * 62)
    print(row("Q1 support", "Yes" if q1_supported else "No"))
    print(
        row(
            "Direct battery charge",
            charge_command or "No (voltage estimate will be used)",
        )
    )
    if f_supported and f_raw is not None:
        print(row("F rating", f_raw))
        if f_rating is not None:
            print(
                row(
                    "Nominal input",
                    f"{fmt(f_rating.input_voltage, 1, 'V')} · "
                    f"{fmt(f_rating.input_frequency, 1, 'Hz')} · "
                    f"{fmt(f_rating.input_current, 1, 'A')}",
                )
            )
            print(
                row(
                    "Nominal battery",
                    fmt(f_rating.battery_voltage, 2, "V"),
                )
            )
    if i_supported and i_result is not None:
        print(row("Device info", " | ".join(i_result["responses"])))
    return 0


def configure_device(
    port: serial.Serial, context: MonitorContext, args: argparse.Namespace
) -> None:
    """Read static device information and detect supported queries."""
    rating = query_f(port)
    if rating is not None:
        context.nominal_input_voltage = rating.input_voltage
        context.nominal_input_current = rating.input_current
        context.nominal_input_frequency = rating.input_frequency
    else:
        context.nominal_input_voltage = None
        context.nominal_input_current = None
        context.nominal_input_frequency = None

    if (
        rating is not None
        and rating.battery_voltage is not None
        and rating.battery_voltage > 0
    ):
        context.nominal_battery = rating.battery_voltage
        context.nominal_source = "F"
    else:
        context.nominal_battery = args.battery_nominal
        context.nominal_source = "config"

    context.charge_query = detect_charge_query(port)
    context.device_info = query_info(port)


def build_sample(
    port: Optional[serial.Serial],
    reading: Reading,
    context: MonitorContext,
) -> Sample:
    status = parse_status(reading.status_bits)

    direct_charge: Optional[float] = None
    command: Optional[str] = None
    if port is not None and context.charge_query is not None:
        direct_charge = query_charge(port, context.charge_query)
        if direct_charge is not None:
            command = context.charge_query.command.decode("ascii")

    estimated_charge: Optional[float] = None
    if direct_charge is None:
        estimated_charge = estimate_charge(
            reading,
            context.nominal_battery,
            context.battery_low,
            context.battery_high,
        )

    power = estimate_power(reading.load_percent, context.ups_watts)
    runtime = estimate_runtime(
        power, context.nominal_battery, context.battery_ah
    )

    return Sample(
        timestamp=datetime.now().astimezone(),
        port=context.port,
        reading=reading,
        status=status,
        battery_charge_direct=direct_charge,
        battery_charge_estimated=estimated_charge,
        charge_command=command,
        output_power_w=power,
        runtime_minutes=runtime,
    )


def emit_error(
    port: str, code: str, message: str, json_mode: bool
) -> None:
    if json_mode:
        print(
            json.dumps(
                error_event(port, code, message), ensure_ascii=False
            ),
            flush=True,
        )
    else:
        print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def run_monitor(
    args: argparse.Namespace,
    context: MonitorContext,
    tracker: OutageTracker,
    state: MonitorState,
    logger: Optional[CsvLogger],
) -> bool:
    """Run the monitor until --once completes or the user stops it."""
    while True:
        try:
            with open_serial(args.port) as port:
                configure_device(port, context, args)
                failures = 0

                while True:
                    started = time.monotonic()
                    raw = query_q1(port)

                    if not raw:
                        failures += 1
                        emit_error(
                            args.port,
                            "timeout",
                            f"No response from UPS after "
                            f"{QUERY_TIMEOUT:.0f} s.",
                            args.json,
                        )
                        if args.once:
                            return False
                        if failures >= MAX_CONSECUTIVE_ERRORS:
                            raise serial.SerialException(
                                "too many consecutive read failures"
                            )
                    else:
                        reading = parse_q1(raw)
                        if reading is None:
                            failures += 1
                            emit_error(
                                args.port,
                                "invalid_response",
                                f"Unrecognized Q1 response: {raw!r}",
                                args.json,
                            )
                            if args.once:
                                return False
                            if failures >= MAX_CONSECUTIVE_ERRORS:
                                raise serial.SerialException(
                                    "too many invalid Q1 responses"
                                )
                        else:
                            failures = 0
                            sample = build_sample(port, reading, context)
                            outage = tracker.update(sample)
                            payload = sample_to_dict(sample, context, tracker)
                            state.update(payload)
                            if logger is not None:
                                logger.write(sample)

                            if args.json:
                                print(
                                    json.dumps(payload, ensure_ascii=False),
                                    flush=True,
                                )
                                if outage is not None:
                                    print(
                                        json.dumps(
                                            {
                                                "event": "outage_summary",
                                                **outage_to_dict(outage),
                                            },
                                            ensure_ascii=False,
                                        ),
                                        flush=True,
                                    )
                            else:
                                render(
                                    sample,
                                    context,
                                    tracker,
                                    clear=(
                                        not args.once and not args.no_clear
                                    ),
                                    footer=not args.once,
                                )
                                if outage is not None and not sys.stdout.isatty():
                                    print_outage_summary(outage)

                            if args.once:
                                return True

                    elapsed = time.monotonic() - started
                    time.sleep(max(0.0, args.interval - elapsed))

        except serial.SerialException as exc:
            if args.once:
                print(
                    f"Serial error on {args.port}: {exc}",
                    file=sys.stderr,
                )
                return False
            print(
                f"[{datetime.now():%H:%M:%S}] Serial error on "
                f"{args.port}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            print(
                f"Reconnecting in {RECONNECT_DELAY:.0f} s...",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(RECONNECT_DELAY)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor a Megatec/Q1 UPS over a serial port."
    )
    parser.add_argument(
        "port",
        nargs="?",
        default="/dev/ttyUSB0",
        help="serial port (default: /dev/ttyUSB0)",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="seconds between readings (default: 1)",
    )
    parser.add_argument(
        "--name",
        default="Megatec/Q1 UPS",
        metavar="NAME",
        help="display name for the UPS (default: Megatec/Q1 UPS)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="take one reading and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON object per sample instead of the dashboard",
    )
    parser.add_argument(
        "--log",
        metavar="FILE",
        help="append samples to a CSV file",
    )
    parser.add_argument(
        "--http",
        type=int,
        metavar="PORT",
        help="serve /status and /metrics over HTTP on this port",
    )
    parser.add_argument(
        "--http-bind",
        default="127.0.0.1",
        metavar="ADDRESS",
        help="HTTP bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--battery-nominal",
        type=float,
        default=DEFAULT_BATTERY_NOMINAL,
        metavar="VOLTS",
        help=(
            "nominal battery pack voltage used when F is not supported "
            f"(default: {DEFAULT_BATTERY_NOMINAL:.0f})"
        ),
    )
    parser.add_argument(
        "--battery-low",
        type=float,
        metavar="VOLTS",
        help="battery voltage corresponding to 0 %% (default: nominal * 104/120)",
    )
    parser.add_argument(
        "--battery-high",
        type=float,
        metavar="VOLTS",
        help="battery voltage corresponding to 100 %% (default: nominal * 130/120)",
    )
    parser.add_argument(
        "--ups-va",
        type=float,
        default=DEFAULT_UPS_VA,
        metavar="VA",
        help=f"UPS apparent power rating (default: {DEFAULT_UPS_VA:.0f})",
    )
    parser.add_argument(
        "--ups-watts",
        type=float,
        default=DEFAULT_UPS_WATTS,
        metavar="WATTS",
        help=f"UPS active power rating (default: {DEFAULT_UPS_WATTS:.0f})",
    )
    parser.add_argument(
        "--battery-ah",
        type=float,
        default=DEFAULT_BATTERY_AH,
        metavar="AH",
        help=f"battery capacity in Ah (default: {DEFAULT_BATTERY_AH:.0f})",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="do not clear the terminal between dashboard updates",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="probe supported Megatec commands and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("--interval must be greater than zero")
    if args.battery_nominal <= 0:
        parser.error("--battery-nominal must be greater than zero")
    if args.ups_va <= 0 or args.ups_watts <= 0:
        parser.error("--ups-va and --ups-watts must be greater than zero")
    if args.battery_ah <= 0:
        parser.error("--battery-ah must be greater than zero")
    if args.http is not None and not (1 <= args.http <= 65535):
        parser.error("--http must be a valid TCP port (1-65535)")

    low = (
        args.battery_low
        if args.battery_low is not None
        else args.battery_nominal * 104 / 120
    )
    high = (
        args.battery_high
        if args.battery_high is not None
        else args.battery_nominal * 130 / 120
    )
    if high <= low:
        parser.error("--battery-high must be greater than --battery-low")

    if args.diagnose:
        return run_diagnostics(args)

    context = MonitorContext(
        port=args.port,
        name=args.name,
        ups_va=args.ups_va,
        ups_watts=args.ups_watts,
        battery_ah=args.battery_ah,
        nominal_battery=args.battery_nominal,
        nominal_source="config",
        battery_low=args.battery_low,
        battery_high=args.battery_high,
    )
    tracker = OutageTracker()
    state = MonitorState()
    logger: Optional[CsvLogger] = None
    httpd: Optional[MonitorHTTPServer] = None

    try:
        if args.http is not None:
            try:
                httpd = start_http_server(args.http, args.http_bind, state)
            except OSError as exc:
                print(
                    f"Cannot start HTTP server on "
                    f"{args.http_bind}:{args.http}: {exc}",
                    file=sys.stderr,
                )
                return 1
            print(
                f"HTTP API listening on "
                f"http://{args.http_bind}:{args.http}/status",
                file=sys.stderr,
                flush=True,
            )

        if args.log:
            try:
                logger = CsvLogger(args.log)
            except OSError as exc:
                print(
                    f"Cannot open log file {args.log}: {exc}",
                    file=sys.stderr,
                )
                return 1
            print(
                f"Logging samples to {args.log}",
                file=sys.stderr,
                flush=True,
            )

        ok = run_monitor(args, context, tracker, state, logger)
        return 0 if ok else 1

    except KeyboardInterrupt:
        print("\nMonitor stopped.", file=sys.stderr, flush=True)
        return 130

    finally:
        if logger is not None:
            logger.close()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
