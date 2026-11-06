#!/usr/bin/env python3
"""FastAPI web application for the SAI monitor.

The application samples the UPS in a background thread, stores each sample in
SQLite, and exposes a small JSON API plus a static dashboard. It is designed to
run either directly with uvicorn or inside the provided Docker image.

Configuration is done through environment variables so the same image can be
deployed as a Portainer stack without code changes.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import random
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import serial
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from database import Database
from sai_monitor import (
    BATTERY_STATE_CHARGED_RATIO,
    BATTERY_STATE_CHARGING_RATIO,
    BATTERY_STATE_LOW_RATIO,
    QUERY_TIMEOUT,
    MonitorContext,
    MonitorState,
    OutageTracker,
    Reading,
    Sample,
    VERSION,
    build_sample,
    configure_device,
    now_iso,
    open_serial,
    parse_q1,
    query_q1,
    sample_to_dict,
)

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DEFAULT_DB = BASE_DIR / "data" / "sai.db"

RECONNECT_DELAY = 2.0
MAX_CONSECUTIVE_ERRORS = 5
STOP_TIMEOUT = 10.0
PRUNE_INTERVAL = 3600.0

LOG_LEVEL = os.getenv("SAI_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sai.webapp")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_optional_float(name: str) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


@dataclass
class Settings:
    port: str = "/dev/ttyUSB0"
    name: str = "Megatec/Q1 UPS"
    web_title: str = "UPS Monitor"
    interval: float = 1.0
    db_path: str = str(DEFAULT_DB)
    retention_days: int = 30
    battery_nominal: float = 12.0
    battery_ah: float = 7.0
    ups_va: float = 650.0
    ups_watts: float = 360.0
    battery_low: Optional[float] = None
    battery_high: Optional[float] = None
    battery_state_low: Optional[float] = None
    battery_state_charged: Optional[float] = None
    battery_state_charging: Optional[float] = None
    simulate: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            port=os.getenv("SAI_PORT", "/dev/ttyUSB0"),
            name=os.getenv("SAI_NAME", "Megatec/Q1 UPS"),
            web_title=os.getenv("SAI_WEB_TITLE", "UPS Monitor").strip()
            or "UPS Monitor",
            interval=_env_float("SAI_INTERVAL", 1.0),
            db_path=os.getenv("SAI_DB", str(DEFAULT_DB)),
            retention_days=_env_int("SAI_RETENTION_DAYS", 30),
            battery_nominal=_env_float("SAI_BATTERY_NOMINAL", 12.0),
            battery_ah=_env_float("SAI_BATTERY_AH", 7.0),
            ups_va=_env_float("SAI_UPS_VA", 650.0),
            ups_watts=_env_float("SAI_UPS_WATTS", 360.0),
            battery_low=_env_optional_float("SAI_BATTERY_LOW"),
            battery_high=_env_optional_float("SAI_BATTERY_HIGH"),
            battery_state_low=_env_optional_float(
                "SAI_BATTERY_STATE_LOW_V"
            ),
            battery_state_charged=_env_optional_float(
                "SAI_BATTERY_STATE_CHARGED_V"
            ),
            battery_state_charging=_env_optional_float(
                "SAI_BATTERY_STATE_CHARGING_V"
            ),
            simulate=_env_bool("SAI_SIMULATE", False),
        )
        if settings.interval <= 0:
            raise ValueError("SAI_INTERVAL must be greater than zero")
        if settings.retention_days < 0:
            raise ValueError("SAI_RETENTION_DAYS cannot be negative")
        if settings.battery_nominal <= 0:
            raise ValueError("SAI_BATTERY_NOMINAL must be greater than zero")
        if settings.battery_ah <= 0:
            raise ValueError("SAI_BATTERY_AH must be greater than zero")
        if settings.ups_va <= 0 or settings.ups_watts <= 0:
            raise ValueError(
                "SAI_UPS_VA and SAI_UPS_WATTS must be greater than zero"
            )
        low = (
            settings.battery_low
            if settings.battery_low is not None
            else settings.battery_nominal * 104 / 120
        )
        high = (
            settings.battery_high
            if settings.battery_high is not None
            else settings.battery_nominal * 130 / 120
        )
        if high <= low:
            raise ValueError(
                "SAI_BATTERY_HIGH must be greater than SAI_BATTERY_LOW"
            )
        state_low = (
            settings.battery_state_low
            if settings.battery_state_low is not None
            else settings.battery_nominal * BATTERY_STATE_LOW_RATIO
        )
        state_charged = (
            settings.battery_state_charged
            if settings.battery_state_charged is not None
            else settings.battery_nominal * BATTERY_STATE_CHARGED_RATIO
        )
        state_charging = (
            settings.battery_state_charging
            if settings.battery_state_charging is not None
            else settings.battery_nominal * BATTERY_STATE_CHARGING_RATIO
        )
        if not state_low < state_charged < state_charging:
            raise ValueError(
                "SAI_BATTERY_STATE_LOW_V, SAI_BATTERY_STATE_CHARGED_V and "
                "SAI_BATTERY_STATE_CHARGING_V must be strictly increasing"
            )
        return settings

    def public_config(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "web_title": self.web_title,
            "port": self.port,
            "interval": self.interval,
            "simulate": self.simulate,
            "retention_days": self.retention_days,
            "battery_nominal": self.battery_nominal,
            "battery_ah": self.battery_ah,
            "ups_va": self.ups_va,
            "ups_watts": self.ups_watts,
        }


class Sampler:
    """Background serial sampler with a reconnect loop and SQLite storage."""

    def __init__(self, settings: Settings, store: Database) -> None:
        self.settings = settings
        self.store = store
        self.context = MonitorContext(
            port=settings.port,
            name=settings.name,
            ups_va=settings.ups_va,
            ups_watts=settings.ups_watts,
            battery_ah=settings.battery_ah,
            nominal_battery=settings.battery_nominal,
            nominal_source="config",
            battery_low=settings.battery_low,
            battery_high=settings.battery_high,
            battery_state_low=settings.battery_state_low,
            battery_state_charged=settings.battery_state_charged,
            battery_state_charging=settings.battery_state_charging,
        )
        self.tracker = OutageTracker()
        self.state = MonitorState()
        self.started_at = datetime.now().astimezone()
        self.samples = 0
        self.running = False
        self.connected = False
        self.last_error: Optional[str] = None
        self._status_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_prune = 0.0

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            with self._status_lock:
                self.running = True
                self.last_error = None
            self._thread = threading.Thread(
                target=self._run, name="sai-sampler", daemon=True
            )
            self._thread.start()
            logger.info("Sampler started")

    def stop(self) -> bool:
        """Stop the sampler and wait for the serial port to be released.

        Returns True only when the sampler thread has finished. If it is still
        alive after the timeout, the port may still be open and the caller must
        not assume it is available to other processes.
        """
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
            if thread is not None:
                thread.join(timeout=STOP_TIMEOUT)
                if thread.is_alive():
                    message = (
                        "Sampler thread did not stop within "
                        f"{STOP_TIMEOUT:.0f} s; the serial port may still be "
                        "in use."
                    )
                    with self._status_lock:
                        self.last_error = message
                    logger.error(message)
                    return False
                self._thread = None

            with self._status_lock:
                self.running = False
                self.connected = False
                self.last_error = "Disconnected by the user"

        logger.info("Sampler stopped; serial port released")
        return True

    def _set_status(self, connected: bool, error: Optional[str]) -> None:
        with self._status_lock:
            self.connected = connected
            self.last_error = error

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            running = self.running
            connected = self.connected
            error = self.last_error
            samples = self.samples

        sample = self.state.snapshot()
        sample_age = None
        if sample is not None and sample.get("timestamp_unix") is not None:
            sample_age = max(
                0.0, time.time() - float(sample["timestamp_unix"])
            )

        return {
            "server_time": now_iso(),
            "running": running,
            "connected": connected,
            "error": error,
            "simulate": self.settings.simulate,
            "samples": samples,
            "sample_age_s": sample_age,
            "config": self.settings.public_config(),
            "sample": sample,
        }

    def metrics(self) -> str:
        with self._status_lock:
            running = self.running
            connected = self.connected
        text = self.state.metrics()
        text += f"sai_sampler_running {1 if running else 0}\n"
        text += f"sai_serial_connected {1 if connected else 0}\n"
        text += f"sai_simulation {1 if self.settings.simulate else 0}\n"
        return text

    def _run(self) -> None:
        try:
            self.store.prune()
            self._last_prune = time.monotonic()
            if self.settings.simulate:
                logger.info("Simulation mode enabled; no serial port is used")
                self._run_simulation()
                return
            self._run_serial()
        finally:
            with self._status_lock:
                self.running = False
                self.connected = False

    def _run_serial(self) -> None:
        args = argparse.Namespace(
            battery_nominal=self.settings.battery_nominal
        )

        while not self._stop.is_set():
            try:
                with open_serial(self.settings.port) as port:
                    configure_device(port, self.context, args)
                    self._set_status(True, None)
                    logger.info("Connected to %s", self.settings.port)
                    if self.context.charge_query is not None:
                        command = self.context.charge_query.command.decode(
                            "ascii", errors="replace"
                        )
                        logger.info("Battery charge query: %s", command)
                    else:
                        logger.info(
                            "No direct battery charge query; using "
                            "voltage estimation"
                        )

                    failures = 0
                    while not self._stop.is_set():
                        started = time.monotonic()
                        raw = query_q1(port)

                        if not raw:
                            failures += 1
                            message = (
                                "No response from UPS after "
                                f"{QUERY_TIMEOUT:.1f} s."
                            )
                            self._set_status(True, message)
                            logger.warning(message)
                            if failures >= MAX_CONSECUTIVE_ERRORS:
                                raise serial.SerialException(
                                    "too many consecutive read failures"
                                )
                        else:
                            reading = parse_q1(raw)
                            if reading is None:
                                failures += 1
                                message = (
                                    "Unrecognized Q1 response: "
                                    f"{raw!r}"
                                )
                                self._set_status(True, message)
                                logger.warning(message)
                                if failures >= MAX_CONSECUTIVE_ERRORS:
                                    raise serial.SerialException(
                                        "too many invalid Q1 responses"
                                    )
                            else:
                                failures = 0
                                sample = build_sample(
                                    port, reading, self.context
                                )
                                self._process_sample(sample)
                                self._set_status(True, None)

                        elapsed = time.monotonic() - started
                        self._stop.wait(
                            max(0.0, self.settings.interval - elapsed)
                        )

            except serial.SerialException as exc:
                self._set_status(False, str(exc))
                logger.warning(
                    "Serial error on %s: %s", self.settings.port, exc
                )
                if self._stop.wait(RECONNECT_DELAY):
                    break
            except Exception:
                self._set_status(False, "unexpected sampler error")
                logger.exception("Unexpected sampler error")
                if self._stop.wait(RECONNECT_DELAY):
                    break

    def _run_simulation(self) -> None:
        self.context.nominal_input_voltage = 230.0
        self.context.nominal_input_frequency = 50.0
        self.context.nominal_input_current = None
        self.context.nominal_battery = self.settings.battery_nominal
        self.context.nominal_source = "config"
        self.context.charge_query = None
        self.context.device_info = "Simulation mode"
        self._set_status(True, None)

        started = time.monotonic()
        while not self._stop.is_set():
            started_sample = time.monotonic()
            elapsed = started_sample - started
            sample = build_sample(
                None, self._simulated_reading(elapsed), self.context
            )
            self._process_sample(sample)
            self._set_status(True, None)

            elapsed_sample = time.monotonic() - started_sample
            self._stop.wait(
                max(0.0, self.settings.interval - elapsed_sample)
            )

    def _simulated_reading(self, elapsed: float) -> Reading:
        cycle = elapsed % 300
        outage = 240 <= cycle < 300

        input_voltage = (
            0.0
            if outage
            else 230.0 + 2.0 * math.sin(elapsed / 17.0)
            + random.uniform(-0.4, 0.4)
        )
        output_voltage = (
            230.0 + 1.5 * math.sin(elapsed / 11.0)
            + random.uniform(-0.2, 0.2)
        )
        load_percent = max(
            0.0,
            min(
                100.0,
                25.0
                + 8.0 * math.sin(elapsed / 7.0)
                + random.uniform(-1.5, 1.5),
            ),
        )
        input_frequency = 50.0 + 0.03 * math.sin(elapsed / 5.0)

        if outage:
            battery_voltage = (
                13.0
                - 0.02 * (cycle - 240.0)
                + random.uniform(-0.02, 0.02)
            )
        elif cycle < 30.0:
            # Recharging after the previous cycle outage.
            battery_voltage = 12.6 + random.uniform(-0.02, 0.02)
        elif cycle < 180.0:
            # Absorption / boost phase.
            battery_voltage = 14.2 + random.uniform(-0.02, 0.02)
        else:
            # Float phase.
            battery_voltage = 13.4 + random.uniform(-0.02, 0.02)

        temperature = 28.0 + 1.5 * math.sin(elapsed / 60.0)

        bits = ["0"] * 8
        if outage:
            bits[0] = "1"
        if outage and battery_voltage < 11.9:
            bits[1] = "1"
        bits[4] = "1"  # offline / standby topology
        bits[7] = "1"  # beeper enabled

        return Reading(
            input_voltage=input_voltage,
            input_fault_voltage=230.0,
            output_voltage=output_voltage,
            load_percent=load_percent,
            input_frequency=input_frequency,
            battery_voltage=battery_voltage,
            temperature=temperature,
            status_bits="".join(bits),
        )

    def _process_sample(self, sample: Sample) -> None:
        outage = self.tracker.update(sample)
        payload = sample_to_dict(sample, self.context, self.tracker)
        self.state.update(payload)

        try:
            self.store.insert_sample(sample)
            if outage is not None:
                self.store.insert_outage(outage)
                logger.info(
                    "Grid outage ended: %.1f s, min battery %.2f V",
                    outage.duration_s or 0.0,
                    outage.min_voltage
                    if outage.min_voltage is not None
                    else float("nan"),
                )
        except Exception:
            logger.exception("Could not persist sample or outage")

        with self._status_lock:
            self.samples += 1

        now = time.monotonic()
        if now - self._last_prune >= PRUNE_INTERVAL:
            self._last_prune = now
            try:
                self.store.prune()
            except Exception:
                logger.exception("Could not prune old samples")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    logger.info(
        "Starting SAI monitor: name=%r port=%s interval=%.2fs db=%s "
        "simulate=%s",
        settings.name,
        settings.port,
        settings.interval,
        settings.db_path,
        settings.simulate,
    )
    store = Database(settings.db_path, settings.retention_days)
    sampler = Sampler(settings, store)
    app.state.settings = settings
    app.state.store = store
    app.state.sampler = sampler
    sampler.start()
    try:
        yield
    finally:
        logger.info("Stopping SAI monitor")
        sampler.stop()


app = FastAPI(
    title="SAI Monitor",
    version=VERSION,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    sampler: Sampler = app.state.sampler
    status = sampler.status()
    return {
        "ok": True,
        "running": status["running"],
        "connected": status["connected"],
        "error": status["error"],
        "simulate": status["simulate"],
        "sample_age_s": status["sample_age_s"],
        "server_time": now_iso(),
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    sampler: Sampler = app.state.sampler
    return sampler.status()


@app.get("/api/history")
def history(
    hours: float = Query(1.0, gt=0, le=24 * 90),
    max_points: int = Query(600, ge=10, le=5000),
) -> dict[str, Any]:
    store: Database = app.state.store
    return store.history(hours, max_points)


@app.get("/api/outages")
def outages(
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    store: Database = app.state.store
    return {"outages": store.outages(limit)}


@app.post("/api/sampler/start")
def sampler_start() -> dict[str, Any]:
    sampler: Sampler = app.state.sampler
    sampler.start()
    return {"ok": True, **sampler.status()}


@app.post("/api/sampler/stop")
def sampler_stop() -> dict[str, Any]:
    sampler: Sampler = app.state.sampler
    if not sampler.stop():
        raise HTTPException(
            status_code=503,
            detail=(
                "The sampler did not stop in time; the serial port may still "
                "be in use."
            ),
        )
    return {"ok": True, **sampler.status()}


@app.get("/metrics", include_in_schema=False)
def metrics() -> PlainTextResponse:
    sampler: Sampler = app.state.sampler
    return PlainTextResponse(
        sampler.metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
