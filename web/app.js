"use strict";

(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    webTitle: $("web-title"),
    upsName: $("ups-name"),
    simBadge: $("sim-badge"),
    statusPill: $("status-pill"),
    statusText: $("status-text"),
    toggleBtn: $("toggle-btn"),
    alert: $("alert"),
    alertText: $("alert-text"),
    batteryState: $("battery-state"),
    chargeSource: $("charge-source"),
    batteryGauge: $("battery-gauge"),
    chargeValue: $("charge-value"),
    batteryVoltage: $("battery-voltage"),
    runtime: $("runtime"),
    chargeNote: $("charge-note"),
    topology: $("topology"),
    powerState: $("power-state"),
    powerSub: $("power-sub"),
    statusBits: $("status-bits"),
    powerW: $("power-w"),
    sessionUptime: $("session-uptime"),
    outageCount: $("outage-count"),
    metricInput: $("metric-input"),
    metricInputSub: $("metric-input-sub"),
    metricOutput: $("metric-output"),
    metricOutputSub: $("metric-output-sub"),
    metricLoad: $("metric-load"),
    loadBar: $("load-bar"),
    metricFrequency: $("metric-frequency"),
    metricFrequencySub: $("metric-frequency-sub"),
    metricTemperature: $("metric-temperature"),
    metricUpdated: $("metric-updated"),
    metricAge: $("metric-age"),
    outagesBody: $("outages-body"),
    footerInfo: $("footer-info"),
    toast: $("toast"),
  };

  const state = {
    status: null,
    running: false,
    connected: false,
    simulate: false,
    hours: 1,
    actionBusy: false,
    busyLabel: null,
    historySamples: [],
    statusError: false,
  };

  const COLORS = {
    input: "#38bdf8",
    output: "#a78bfa",
    battery: "#34d399",
    charge: "#fbbf24",
    load: "#fb7185",
    power: "#60a5fa",
    grid: "rgba(148, 163, 184, 0.08)",
    gridSoft: "rgba(148, 163, 184, 0.05)",
  };

  const BATTERY_STATES = {
    charging: {
      label: "Charging",
      note: "Mains power is charging the battery.",
    },
    charged: {
      label: "Charged",
      note: "Battery is fully charged and held at float voltage.",
    },
    recharging: {
      label: "Recharging",
      note: "Mains power is back; the battery is recharging.",
    },
    discharging: {
      label: "Discharging",
      note: "The UPS is running on battery.",
    },
    low: {
      label: "Low",
      note: "Battery voltage is low.",
    },
    test: {
      label: "Test",
      note: "A battery test is in progress.",
    },
    fault: {
      label: "Fault",
      note: "The UPS reports a fault.",
    },
    unknown: {
      label: "Unknown",
      note: "Battery state is not available.",
    },
  };

  Chart.defaults.color = "#8ea0b8";
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 11;

  // Charts are a summary, not a live oscilloscope. Fewer, averaged points and
  // a slower refresh keep the lines readable without hiding real outages.
  const HISTORY_REFRESH_MS = 30000;
  const HISTORY_MAX_POINTS = 360;

  const tooltipStyle = {
    backgroundColor: "rgba(8, 13, 23, 0.96)",
    borderColor: "rgba(148, 163, 184, 0.22)",
    borderWidth: 1,
    titleColor: "#e7eef9",
    bodyColor: "#c7d3e4",
    padding: 10,
    cornerRadius: 8,
    displayColors: true,
    boxPadding: 4,
  };

  function formatAxisNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "";
    return Number.isInteger(number) ? String(number) : number.toFixed(1);
  }

  function baseScales(unitLeft, unitRight) {
    return {
      x: {
        grid: { color: COLORS.gridSoft, drawTicks: false },
        border: { display: false },
        ticks: {
          color: "#64748b",
          maxTicksLimit: 8,
          maxRotation: 0,
          autoSkip: true,
          font: { size: 10.5 },
        },
      },
      y: {
        position: "left",
        grace: "8%",
        grid: { color: COLORS.grid, drawTicks: false },
        border: { display: false },
        ticks: {
          color: "#64748b",
          padding: 6,
          font: { size: 10.5 },
          callback: (value) => `${formatAxisNumber(value)} ${unitLeft}`,
        },
      },
      ...(unitRight
        ? {
            y1: {
              position: "right",
              min: unitRight === "%" ? 0 : undefined,
              max: unitRight === "%" ? 100 : undefined,
              grid: { drawOnChartArea: false },
              border: { display: false },
              ticks: {
                color: "#64748b",
                padding: 6,
                font: { size: 10.5 },
                callback: (value) => `${formatAxisNumber(value)} ${unitRight}`,
              },
            },
          }
        : {}),
    };
  }

  function lineDataset(label, color, unit, extra = {}) {
    return {
      label,
      unit,
      data: [],
      borderColor: color,
      backgroundColor: color,
      borderWidth: 2,
      pointRadius: 0,
      pointHitRadius: 10,
      pointHoverRadius: 3,
      tension: 0.2,
      cubicInterpolationMode: "monotone",
      spanGaps: true,
      ...extra,
    };
  }

  function createCharts() {
    const voltage = new Chart($("voltage-chart"), {
      type: "line",
      data: {
        labels: [],
        datasets: [
          lineDataset("Input", COLORS.input, "V", {
            fill: true,
            backgroundColor: "rgba(56, 189, 248, 0.09)",
          }),
          lineDataset("Output", COLORS.output, "V"),
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { ...tooltipStyle, callbacks: historyTooltipCallbacks() },
        },
        scales: baseScales("V"),
        elements: { line: { borderCapStyle: "round" } },
      },
    });

    const battery = new Chart($("battery-chart"), {
      type: "line",
      data: {
        labels: [],
        datasets: [
          lineDataset("Battery voltage", COLORS.battery, "V", {
            fill: true,
            backgroundColor: "rgba(52, 211, 153, 0.08)",
          }),
          lineDataset("Charge", COLORS.charge, "%", {
            yAxisID: "y1",
            borderDash: [5, 4],
            fill: false,
          }),
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { ...tooltipStyle, callbacks: historyTooltipCallbacks() },
        },
        scales: baseScales("V", "%"),
      },
    });

    const load = new Chart($("load-chart"), {
      type: "line",
      data: {
        labels: [],
        datasets: [
          lineDataset("Load", COLORS.load, "%", {
            fill: true,
            backgroundColor: "rgba(251, 113, 133, 0.07)",
          }),
          lineDataset("Output power", COLORS.power, "W", {
            yAxisID: "y1",
            borderDash: [5, 4],
            fill: false,
          }),
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 250 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { ...tooltipStyle, callbacks: historyTooltipCallbacks() },
        },
        scales: baseScales("%", "W"),
      },
    });

    return { voltage, battery, load };
  }

  function historyTooltipCallbacks() {
    return {
      title: (items) => {
        if (!items.length) return "";
        const sample = state.historySamples[items[0].dataIndex];
        if (!sample) return items[0].label;
        return new Date(sample.t * 1000).toLocaleString();
      },
      label: (ctx) => {
        const value = ctx.parsed.y;
        if (value === null || value === undefined) return `${ctx.dataset.label}: —`;
        const unit = ctx.dataset.unit || "";
        const digits = unit === "W" || unit === "%" ? 0 : 1;
        return `${ctx.dataset.label}: ${Number(value).toFixed(digits)} ${unit}`.trim();
      },
    };
  }

  const charts = createCharts();

  function setText(el, value) {
    if (el) el.textContent = value;
  }

  function isNumber(value) {
    return value !== null && value !== undefined && Number.isFinite(Number(value));
  }

  function roundNumber(value, digits = 1) {
    if (!isNumber(value)) return null;
    const factor = 10 ** digits;
    return Math.round(Number(value) * factor) / factor;
  }

  function historySeries(samples, key, digits = 1) {
    return samples.map((sample) => roundNumber(sample[key], digits));
  }

  function axisValues(samples, keys) {
    const values = [];
    for (const sample of samples) {
      for (const key of keys) {
        if (isNumber(sample[key])) values.push(Number(sample[key]));
      }
    }
    return values;
  }

  function applyAxisRange(axis, values, options) {
    if (!values.length) {
      delete axis.min;
      delete axis.max;
      return;
    }

    const { minSpan, step, floor = null, padding = 0.08 } = options;
    let low = Math.min(...values);
    let high = Math.max(...values);

    if (floor !== null) low = Math.min(low, floor);

    if (high - low < minSpan) {
      const middle = (low + high) / 2;
      low = middle - minSpan / 2;
      high = middle + minSpan / 2;
    } else {
      const pad = (high - low) * padding;
      low -= pad;
      high += pad;
    }

    if (floor !== null && low < floor) {
      const span = high - low;
      low = floor;
      high = Math.max(high, low + span);
    }

    axis.min = Math.floor(low / step) * step;
    axis.max = Math.ceil(high / step) * step;
  }

  function applyStableAxes(samples) {
    applyAxisRange(
      charts.voltage.options.scales.y,
      axisValues(samples, ["input_voltage", "output_voltage"]),
      { minSpan: 8, step: 1 }
    );
    applyAxisRange(
      charts.battery.options.scales.y,
      axisValues(samples, ["battery_voltage"]),
      { minSpan: 1, step: 0.1 }
    );
    applyAxisRange(
      charts.load.options.scales.y,
      axisValues(samples, ["load_percent"]),
      { minSpan: 10, step: 1, floor: 0 }
    );
    applyAxisRange(
      charts.load.options.scales.y1,
      axisValues(samples, ["output_power_w"]),
      { minSpan: 20, step: 5, floor: 0 }
    );
  }

  function fmtNumber(value, digits, unit) {
    if (!isNumber(value)) return "—";
    return `${Number(value).toFixed(digits)} ${unit}`;
  }

  function fmtInt(value, unit) {
    if (!isNumber(value)) return "—";
    return `${Math.round(Number(value))} ${unit}`;
  }

  function fmtRuntime(minutes) {
    if (!isNumber(minutes)) return "—";
    const total = Math.max(0, Math.round(Number(minutes)));
    if (total < 60) return `${total} min`;
    const hours = Math.floor(total / 60);
    const rest = total % 60;
    return `${hours} h ${String(rest).padStart(2, "0")} min`;
  }

  function fmtDuration(seconds) {
    if (!isNumber(seconds)) return "—";
    const total = Math.max(0, Math.round(Number(seconds)));
    if (total < 60) return `${total} s`;
    if (total < 3600) return `${Math.floor(total / 60)} min ${total % 60} s`;
    return `${Math.floor(total / 3600)} h ${String(Math.floor((total % 3600) / 60)).padStart(2, "0")} min`;
  }

  function fmtAge(seconds) {
    if (!isNumber(seconds)) return "No sample yet.";
    const age = Math.max(0, Number(seconds));
    if (age < 2) return "Updated just now.";
    if (age < 60) return `Updated ${Math.round(age)} s ago.`;
    if (age < 3600) return `Updated ${Math.floor(age / 60)} min ago.`;
    return `Updated ${(age / 3600).toFixed(1)} h ago.`;
  }

  function fmtClock(unixSeconds) {
    if (!isNumber(unixSeconds)) return "—";
    return new Date(Number(unixSeconds) * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function fmtOutageStart(unixSeconds) {
    if (!isNumber(unixSeconds)) return "—";
    return new Date(Number(unixSeconds) * 1000).toLocaleString([], {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function setGauge(charge) {
    const value = isNumber(charge) ? Math.max(0, Math.min(100, Number(charge))) : 0;
    els.batteryGauge.style.setProperty("--charge", value.toFixed(1));
    els.batteryGauge.classList.toggle("gauge-low", isNumber(charge) && value < 20);
    els.batteryGauge.classList.toggle(
      "gauge-warn",
      isNumber(charge) && value >= 20 && value < 50
    );
    els.batteryGauge.classList.toggle("gauge-ok", isNumber(charge) && value >= 50);
    setText(els.chargeValue, isNumber(charge) ? `${Math.round(value)}%` : "—");
  }

  function renderBatteryState(batteryState) {
    const info = BATTERY_STATES[batteryState] || BATTERY_STATES.unknown;
    els.batteryState.className =
      "tag tag-state-" + (BATTERY_STATES[batteryState] ? batteryState : "unknown");
    els.batteryState.title = info.note;
    setText(els.batteryState, info.label);
    return info.note;
  }

  function setStatusBits(status) {
    const bits = [];
    if (!status) {
      bits.push(["No sample", "muted"]);
    } else {
      if (status.utility_fail) bits.push(["Mains failure", "bad"]);
      if (status.battery_low) bits.push(["Battery low", "bad"]);
      if (status.bypass) bits.push(["Bypass active", "warn"]);
      if (status.ups_failed) bits.push(["UPS failure", "bad"]);
      if (status.test) bits.push(["Battery test", "info"]);
      if (status.shutdown) bits.push(["Shutdown active", "bad"]);
      if (status.beeper) bits.push(["Beeper enabled", "info"]);
      if (!bits.length) bits.push(["No active alarms", "muted"]);
    }

    els.statusBits.replaceChildren();
    for (const [label, kind] of bits) {
      const span = document.createElement("span");
      span.className = `bit bit-${kind}`;
      span.textContent = label;
      els.statusBits.appendChild(span);
    }
  }

  function setPill(kind, label) {
    els.statusPill.className = `pill pill-${kind}`;
    setText(els.statusText, label);
  }

  function renderButton() {
    if (state.actionBusy) {
      els.toggleBtn.textContent = state.busyLabel || "Working…";
      els.toggleBtn.disabled = true;
      els.toggleBtn.className = "btn btn-disconnect";
      return;
    }

    els.toggleBtn.disabled = false;
    if (state.running) {
      els.toggleBtn.textContent = "Disconnect";
      els.toggleBtn.className = "btn btn-disconnect";
      els.toggleBtn.title = "Stop sampling and release the serial port";
    } else {
      els.toggleBtn.textContent = "Connect";
      els.toggleBtn.className = "btn btn-connect";
      els.toggleBtn.title = "Open the serial port and start sampling";
    }
  }

  function renderAlert(data) {
    let message = "";
    let isError = false;

    if (state.statusError) {
      message = "Cannot reach the monitor API. Retrying…";
      isError = true;
    } else if (state.running && data.error) {
      message = data.error;
      isError = !state.connected;
    }

    if (!message) {
      els.alert.classList.add("hidden");
      return;
    }

    setText(els.alertText, message);
    els.alert.classList.toggle("alert-error", isError);
    els.alert.classList.remove("hidden");
  }

  function renderStatusPill() {
    if (!state.running) {
      setPill("offline", "Disconnected");
      return;
    }
    if (!state.connected) {
      const error = state.status && state.status.error;
      setPill("warning", error ? "Reconnecting" : "Connecting");
      return;
    }

    const status = state.status && state.status.sample ? state.status.sample.status : null;
    if (status && status.ups_failed) {
      setPill("warning", "UPS failure");
    } else if (status && status.utility_fail) {
      setPill("battery", "On battery");
    } else {
      setPill("online", "Online");
    }
  }

  function renderPowerState(sample) {
    els.powerState.classList.remove("state-bad", "state-error", "state-off");

    if (!state.running) {
      setText(els.powerState, "Monitoring stopped");
      els.powerState.classList.add("state-off");
      setText(
        els.powerSub,
        "The serial port is closed and available to other tools."
      );
      return;
    }

    if (!state.connected) {
      const error = state.status && state.status.error;
      setText(
        els.powerState,
        error ? "Reconnecting to UPS" : "Connecting to UPS"
      );
      els.powerState.classList.toggle("state-error", Boolean(error));
      setText(
        els.powerSub,
        error
          ? "The serial port is not available right now."
          : "Opening the serial port and detecting the UPS."
      );
      return;
    }

    if (!sample) {
      setText(els.powerState, "Waiting for data…");
      setText(els.powerSub, "The serial port is open; waiting for the first sample.");
      return;
    }

    const status = sample.status || {};
    if (status.ups_failed) {
      setText(els.powerState, "UPS reports a failure");
      els.powerState.classList.add("state-bad");
      setText(els.powerSub, "Check the UPS hardware and its battery.");
    } else if (status.utility_fail) {
      setText(
        els.powerState,
        status.battery_low ? "Running on battery — low" : "Running on battery"
      );
      els.powerState.classList.add("state-bad");
      setText(
        els.powerSub,
        status.battery_low
          ? "Mains power is lost and the battery is nearly empty."
          : "Mains power is lost. The UPS is supplying the load."
      );
    } else if (status.bypass) {
      setText(els.powerState, "Mains power OK — bypass");
      setText(
        els.powerSub,
        "The UPS is regulating or bypassing the mains supply."
      );
    } else {
      setText(els.powerState, "Mains power OK");
      setText(els.powerSub, "The UPS is protecting the connected load.");
    }
  }

  function renderSample(data) {
    const sample = data.sample;

    if (!sample) {
      setGauge(null);
      els.batteryState.className = "tag hidden";
      els.batteryState.title = "";
      els.chargeSource.className = "tag hidden";
      setText(els.chargeNote, "Waiting for the first sample.");
      setText(els.batteryVoltage, "—");
      setText(els.runtime, "—");
      setText(els.powerW, "—");
      setText(els.metricInput, "—");
      setText(els.metricInputSub, "—");
      setText(els.metricOutput, "—");
      setText(els.metricOutputSub, "—");
      setText(els.metricLoad, "—");
      els.loadBar.style.width = "0%";
      setText(els.metricFrequency, "—");
      setText(els.metricFrequencySub, "—");
      setText(els.metricTemperature, "—");
      setText(els.metricUpdated, "—");
      setText(els.metricAge, "No sample yet.");
      setText(els.sessionUptime, "—");
      setText(els.outageCount, "0");
      els.topology.classList.add("hidden");
      setStatusBits(null);
      renderPowerState(null);
      return;
    }

    const charge = sample.battery_charge;
    setGauge(charge);

    const stateNote = renderBatteryState(sample.battery_state);

    const source = sample.battery_charge_source;
    if (source === "estimated") {
      els.chargeSource.className = "tag tag-estimated";
      setText(els.chargeSource, "estimated");
      setText(
        els.chargeNote,
        `${stateNote} Charge is estimated from battery voltage.`
      );
    } else if (source === "direct") {
      els.chargeSource.className = "tag tag-direct";
      setText(els.chargeSource, "reported");
      setText(
        els.chargeNote,
        `${stateNote} Charge is reported directly by the UPS.`
      );
    } else {
      els.chargeSource.className = "tag hidden";
      setText(els.chargeNote, stateNote);
    }

    setText(els.batteryVoltage, fmtNumber(sample.battery_voltage, 1, "V"));
    setText(els.runtime, fmtRuntime(sample.runtime_minutes));

    setText(els.metricInput, fmtNumber(sample.input_voltage, 1, "V"));
    setText(
      els.metricInputSub,
      isNumber(sample.nominal_input_voltage)
        ? `Nominal ${Number(sample.nominal_input_voltage).toFixed(1)} V`
        : "Nominal voltage unknown"
    );

    setText(els.metricOutput, fmtNumber(sample.output_voltage, 1, "V"));
    setText(
      els.metricOutputSub,
      isNumber(sample.ups_rating_va) && isNumber(sample.ups_rating_watts)
        ? `Rated ${Math.round(sample.ups_rating_va)} VA / ${Math.round(
            sample.ups_rating_watts
          )} W`
        : "UPS rating not configured"
    );

    setText(els.metricLoad, fmtInt(sample.load_percent, "%"));
    const load = isNumber(sample.load_percent)
      ? Math.max(0, Math.min(100, Number(sample.load_percent)))
      : 0;
    els.loadBar.style.width = `${load}%`;

    setText(els.metricFrequency, fmtNumber(sample.input_frequency, 1, "Hz"));
    setText(
      els.metricFrequencySub,
      isNumber(sample.nominal_input_frequency)
        ? `Nominal ${Number(sample.nominal_input_frequency).toFixed(1)} Hz`
        : "Nominal frequency unknown"
    );

    setText(
      els.metricTemperature,
      isNumber(sample.temperature)
        ? `${Number(sample.temperature).toFixed(1)} °C`
        : "N/A"
    );

    setText(els.metricUpdated, fmtClock(sample.timestamp_unix));
    setText(els.metricAge, fmtAge(data.sample_age_s));

    setText(els.powerW, fmtInt(sample.output_power_w, "W"));

    const topology = sample.status && sample.status.offline ? "standby" : "online";
    els.topology.className = "tag tag-neutral";
    setText(els.topology, topology);

    setStatusBits(sample.status);
    renderPowerState(sample);

    const session = sample.session || {};
    setText(els.outageCount, isNumber(session.outages) ? String(session.outages) : "0");
    if (session.started_at) {
      const started = new Date(session.started_at);
      if (!Number.isNaN(started.getTime())) {
        const seconds = (Date.now() - started.getTime()) / 1000;
        setText(els.sessionUptime, fmtDuration(seconds));
      } else {
        setText(els.sessionUptime, "—");
      }
    } else {
      setText(els.sessionUptime, "—");
    }
  }

  function applyStatus(data) {
    state.status = data;
    state.statusError = false;
    state.running = Boolean(data.running);
    state.connected = Boolean(data.connected);
    state.simulate = Boolean(data.simulate);

    if (data.config) {
      const webTitle =
        String(data.config.web_title || "").trim() || "UPS Monitor";
      setText(els.webTitle, webTitle);
      document.title = webTitle;
      setText(els.upsName, data.config.name || "Megatec/Q1 UPS");
      els.simBadge.classList.toggle("hidden", !data.simulate);
      setText(
        els.footerInfo,
        `Sampling every ${Number(data.config.interval).toFixed(1)} s · ` +
          `${data.samples} samples this session · ` +
          `${data.config.retention_days} day retention`
      );
    }

    renderStatusPill();
    renderSample(data);
    renderButton();
    renderAlert(data);
  }

  function renderHistory(payload) {
    const samples = payload.samples || [];
    state.historySamples = samples;
    applyStableAxes(samples);

    const labels = samples.map((sample) => tickLabel(sample.t, payload.hours));

    charts.voltage.data.labels = labels;
    charts.voltage.data.datasets[0].data = historySeries(
      samples,
      "input_voltage"
    );
    charts.voltage.data.datasets[1].data = historySeries(
      samples,
      "output_voltage"
    );
    charts.voltage.update();

    charts.battery.data.labels = labels;
    charts.battery.data.datasets[0].data = historySeries(
      samples,
      "battery_voltage"
    );
    charts.battery.data.datasets[1].data = historySeries(
      samples,
      "battery_charge"
    );
    charts.battery.update();

    charts.load.data.labels = labels;
    charts.load.data.datasets[0].data = historySeries(
      samples,
      "load_percent"
    );
    charts.load.data.datasets[1].data = historySeries(
      samples,
      "output_power_w"
    );
    charts.load.update();

    for (const id of ["voltage-chart", "battery-chart", "load-chart"]) {
      const canvas = $(id);
      const empty = canvas.parentElement.querySelector(".chart-empty");
      if (empty) empty.hidden = samples.length > 0;
    }
  }

  function tickLabel(unixSeconds, hours) {
    const date = new Date(Number(unixSeconds) * 1000);
    if (hours <= 1.5) {
      return date.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
    }
    if (hours <= 24) {
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    if (hours <= 168) {
      return date.toLocaleString([], {
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    }
    return date.toLocaleString([], {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function renderOutages(outages) {
    els.outagesBody.replaceChildren();

    if (!outages.length) {
      const row = document.createElement("tr");
      row.className = "empty-row";
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.textContent = "No outages recorded.";
      row.appendChild(cell);
      els.outagesBody.appendChild(row);
      return;
    }

    for (const outage of outages) {
      const row = document.createElement("tr");
      const values = [
        fmtOutageStart(outage.start_ts),
        fmtDuration(outage.duration_s),
        isNumber(outage.min_charge) ? `${Number(outage.min_charge).toFixed(1)} %` : "—",
        fmtNumber(outage.min_voltage, 2, "V"),
        fmtInt(outage.avg_load_percent, "%"),
        fmtNumber(outage.energy_used_wh_estimate, 1, "Wh"),
      ];
      for (const value of values) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      }
      els.outagesBody.appendChild(row);
    }
  }

  let historyRequest = 0;

  async function refreshHistory() {
    const requestId = ++historyRequest;
    try {
      const response = await fetch(
        `/api/history?hours=${encodeURIComponent(
          state.hours
        )}&max_points=${HISTORY_MAX_POINTS}`,
        { cache: "no-store" }
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (requestId !== historyRequest) return;
      renderHistory(payload);
    } catch (error) {
      console.warn("Could not load history:", error);
    }
  }

  async function refreshOutages() {
    try {
      const response = await fetch("/api/outages?limit=50", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      renderOutages(payload.outages || []);
    } catch (error) {
      console.warn("Could not load outages:", error);
    }
  }

  async function pollStatus() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      applyStatus(await response.json());
    } catch (error) {
      console.warn("Could not reach the monitor API:", error);
      state.statusError = true;
      renderAlert(state.status || {});
      if (!state.status) {
        setText(els.statusText, "API unavailable");
        els.statusPill.className = "pill pill-warning";
      }
    }
  }

  async function toggleSampler() {
    if (state.actionBusy) return;

    const stopping = state.running;
    state.actionBusy = true;
    state.busyLabel = stopping ? "Disconnecting…" : "Connecting…";
    renderButton();

    try {
      const response = await fetch(
        `/api/sampler/${stopping ? "stop" : "start"}`,
        {
          method: "POST",
          headers: { Accept: "application/json" },
        }
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      applyStatus(payload);
      showToast(
        stopping
          ? "Sampler stopped. The serial port is now released."
          : "Sampler started. Opening the serial port…"
      );
      await refreshHistory();
      await refreshOutages();
    } catch (error) {
      showToast(
        `Could not ${stopping ? "disconnect" : "connect"}: ${error.message}`,
        true
      );
    } finally {
      state.actionBusy = false;
      state.busyLabel = null;
      renderButton();
    }
  }

  let toastTimer = null;

  function showToast(message, isError = false) {
    els.toast.textContent = message;
    els.toast.classList.toggle("toast-error", isError);
    els.toast.classList.remove("hidden");
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      els.toast.classList.add("hidden");
    }, 4500);
  }

  els.toggleBtn.addEventListener("click", toggleSampler);

  for (const button of document.querySelectorAll(".range-btn")) {
    button.addEventListener("click", () => {
      const hours = Number(button.dataset.hours);
      if (!Number.isFinite(hours) || hours === state.hours) return;
      state.hours = hours;
      for (const other of document.querySelectorAll(".range-btn")) {
        other.classList.toggle("active", other === button);
      }
      refreshHistory();
    });
  }

  async function statusLoop() {
    await pollStatus();
    window.setTimeout(statusLoop, 1000);
  }

  renderButton();
  statusLoop();
  refreshHistory();
  refreshOutages();
  window.setInterval(refreshHistory, HISTORY_REFRESH_MS);
  window.setInterval(refreshOutages, 60000);

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      pollStatus();
      refreshHistory();
    }
  });
})();
