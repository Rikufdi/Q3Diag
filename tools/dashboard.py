#!/usr/bin/env python3
"""dashboard.py - live local web dashboard for a running `cell.py monitor` session.

Usage:
  python tools/cell.py monitor <run_id> 10800      # in one shell
  python tools/dashboard.py <run_id> [--port 8765] # in another; open http://127.0.0.1:8765/

Reads the same TSVs `monitor` is appending to (quest_wifi_samples.tsv, quest_net_samples.tsv,
quest_env_samples.tsv, pc_samples.tsv, headset_logcat.txt, ping_samples.txt) and re-derives a live
snapshot every request -- no state of its own, so it can be started/stopped/restarted freely without
disturbing the monitor. Meant for watching link/encoder/frame health in real time while testing AP
placement, headset position, or cable routing (pair with `cell.py linktest`/`watch --beep` for audio
alerts when hands are busy moving hardware instead of looking at a screen).

If runs/<run_id>/fingerprint_diff.json exists (written by `cell.py fingerprint`), any flagged metrics
are shown as a banner so a live session can be checked against the last known-good baseline for this
configuration, not just eyeballed.
"""
import sys, os, json, time, http.server
import qsite
import cell

BASE = qsite.base_dir()
RUN_ID = None


# retry_pct/lost_pct are deltas over this many seconds of wifi samples, not an instantaneous reading.
# The wifi sampler runs at ~2s intervals, so a plain last-two-samples delta (the old behaviour) is a
# ~2s window -- too few packets for a stable ratio, hence visibly jumpy. 10s (~5 samples) cuts that
# noise substantially while staying responsive enough to show a real degradation from moving the
# headset/AP within a few seconds; 30s would smooth further but starts fighting the dashboard's other
# job as a live AP-placement-testing readout (see cell.py linktest/watch --beep for the same tradeoff).
RETRY_WINDOW_S = 10

# delivered_mbps had the same problem retry_pct/lost_pct used to have: cell._rate_series() yields one
# point per pair of consecutive ~2s wlan0-byte-counter samples, and encoder VBR + Wi-Fi frame
# aggregation both make that raw 2s figure swing well outside the true average. Same fix, same
# reasoning as RETRY_WINDOW_S above: average the last few points instead of showing the latest one raw.
DELIVERED_WINDOW_S = 6


def _last_wifi_rate(run_dir, window_s=RETRY_WINDOW_S):
    rows = cell.read_tsv(os.path.join(run_dir, cell.ARTIFACTS["wifi"]))
    if not rows:
        return {}
    out = {"rssi": rows[-1].get("rssi"), "tx_link_mbps": rows[-1].get("tx_link_mbps"),
           "freq_mhz": rows[-1].get("freq_mhz")}
    try:
        t_last = time.mktime(time.strptime(rows[-1]["timestamp"][:19], "%Y-%m-%d %H:%M:%S"))
    except (KeyError, ValueError):
        return out
    window = []
    for r in reversed(rows):
        try:
            t = time.mktime(time.strptime(r["timestamp"][:19], "%Y-%m-%d %H:%M:%S"))
        except (KeyError, ValueError):
            continue
        window.append(r)
        if t_last - t > window_s:
            break
    if len(window) < 2:
        return out
    a, b = window[-1], window[0]  # oldest, newest within the window
    try:
        dtx = int(b["tx_success"]) - int(a["tx_success"])
        dtr = int(b["tx_retries"]) - int(a["tx_retries"])
        dtl = int(b["tx_lost"]) - int(a["tx_lost"])
        if dtx > 0:
            out["retry_pct"] = round(dtr / dtx * 100, 2)
            out["lost_pct"] = round(dtl / dtx * 100, 3)
    except (KeyError, ValueError):
        pass
    return out


def _dir_size_mb(path):
    """Total bytes currently written under a run folder, in MB. Metadata only (no file is opened), so
    it is cheap enough to call on every dashboard poll -- which is the point: the operator watches the
    folder grow against the estimate instead of discovering at the end that a trace filled the disk."""
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return round(total / (1024 * 1024), 1)


def _shell_pids(run_dir):
    """pids monitor() recorded for the home shell. Its VrApi lines sit next to the app's/client's, so the
    live fps tile must skip them (same exclusion as cell.vr_api_reduce) -- otherwise a standalone session
    shows vrshell's healthy home-environment frame numbers instead of the game's."""
    try:
        return set(json.load(open(os.path.join(run_dir, "clock.json"))).get("shell_pid") or ())
    except (OSError, ValueError):
        return set()


CPU_HISTORY = 90    # CPU samples kept for the chart; the sampler cadence is 2 s, so ~3 minutes


def _headset_cpu(run_dir):
    """Latest headset CPU total and per-core utilization, plus a short history for the chart.

    Source is monitor's always-on `_cpu_sampler` (quest_cpu.tsv, /proc/stat deltas) -- OVR's own CPU
    columns are only readable after a run has been reduced, which is useless live."""
    rows = cell.read_tsv(os.path.join(run_dir, cell.ARTIFACTS["cpu"]))
    if not rows:
        return {}
    last = rows[-1]
    cores = sorted((k for k in last if k.startswith("cpu") and k != "cpu"),
                   key=lambda k: int(k[3:] or 0))
    out = {"cpu_core_labels": [c.replace("cpu", "C") for c in cores],
           "cpu_cores": [last.get(c, "") for c in cores]}
    hist = []
    for r in rows[-CPU_HISTORY:]:
        try:
            hist.append(round(float(r["cpu"]), 1))
        except (KeyError, TypeError, ValueError):
            continue
    out["cpu_history"] = [None] * (CPU_HISTORY - len(hist)) + hist
    try:
        out["cpu_pct"] = round(float(last["cpu"]), 1)
    except (KeyError, TypeError, ValueError):
        out["cpu_pct"] = None
    return out


def _pc_cpu(run_dir):
    """Latest PC CPU total and per-core utilization, plus a short history (Sample-PC.ps1's
    pc_cpu_pct / pc_cpu_cores, from the single `\\Processor(*)\\% Processor Time` counter call)."""
    rows = cell.read_tsv(os.path.join(run_dir, "pc_samples.tsv"))
    if not rows:
        return {}
    last = rows[-1]
    out = {}
    try:
        out["pc_cpu_pct"] = round(float(last["pc_cpu_pct"]), 1)
    except (KeyError, TypeError, ValueError):
        out["pc_cpu_pct"] = None
    cores = []
    for part in (last.get("pc_cpu_cores") or "").split(","):
        try:
            cores.append(round(float(part), 1))
        except ValueError:
            continue
    if cores:
        out["pc_cpu_cores"] = cores
        out["pc_core_labels"] = [f"C{i}" for i in range(len(cores))]
    hist = []
    for r in rows[-CPU_HISTORY:]:
        try:
            hist.append(round(float(r["pc_cpu_pct"]), 1))
        except (KeyError, TypeError, ValueError):
            continue
    out["pc_cpu_history"] = [None] * (CPU_HISTORY - len(hist)) + hist
    return out


def status(run_id):
    run_dir = os.path.join(BASE, "runs", run_id)
    out = {"run_id": run_id, "updated_at": time.strftime("%H:%M:%S"),
           "exists": os.path.isdir(run_dir), "run_dir": run_dir}
    if not out["exists"]:
        return out
    out["data_rates"] = cell.DATA_RATES_MB_PER_MIN
    out["written_mb"] = _dir_size_mb(run_dir)
    out["presentmon_on"] = (os.path.exists(os.path.join(run_dir, "presentmon_target.json"))
                            or os.path.exists(os.path.join(run_dir, "presentmon.csv")))
    out["trace_on"] = (os.path.exists(os.path.join(run_dir, "trace-state.json"))
                       or os.path.exists(os.path.join(run_dir, "trace.etl")))
    out.update(_last_wifi_rate(run_dir))
    net = cell.tail_row(os.path.join(run_dir, cell.ARTIFACTS["net"]))
    out.update({"wlan0_rx_errs": net.get("wlan0_rx_errs"), "wlan0_rx_drop": net.get("wlan0_rx_drop"),
                "tcp_retrans_segs": net.get("tcp_retrans_segs"), "p2p0_tx_errs": net.get("p2p0_tx_errs"),
                "p2p0_rx_errs": net.get("p2p0_rx_errs")})
    env = cell.tail_row(os.path.join(run_dir, cell.ARTIFACTS["env"]))
    out.update({"soc_c": env.get("soc_usr_c"), "gpuss_c": env.get("gpuss_max_c"),
                "sta_tx_power_dbm": env.get("sta_tx_power_dbm"), "hmd_state": env.get("hmd_state")})
    pc = cell.tail_row(os.path.join(run_dir, "pc_samples.tsv"))
    out.update({"enc_util_pct": pc.get("enc_util_pct"), "gpu_util_pct": pc.get("gpu_util_pct"),
                "pc_tcp_retrans_per_s": pc.get("tcp_retrans_per_s"), "streamer_ws_mb": pc.get("streamer_ws_mb")})
    out.update(_headset_cpu(run_dir))
    out.update(_pc_cpu(run_dir))
    out["vrapi"] = cell.vrapi_tail(os.path.join(run_dir, cell.HEADSET_LOGCAT_NAME),
                                   exclude_pids=_shell_pids(run_dir))
    pr, pt = cell.ping_counts(os.path.join(run_dir, "ping_samples.txt"))
    out["ping_replies"], out["ping_timeouts"] = pr, pt
    try:
        rate = cell._rate_series(run_dir)[-180:]
    except Exception:
        rate = []
    t0 = rate[0][0] if rate else 0
    out["rate_series"] = [[round(t - t0, 1), round(r, 1)] for t, r in rate]
    if rate:
        t_last = rate[-1][0]
        window = [r for t, r in rate if t_last - t <= DELIVERED_WINDOW_S]
        out["delivered_mbps"] = round(sum(window) / len(window), 1)
    else:
        out["delivered_mbps"] = None
    sess_path = os.path.join(run_dir, "session.json")
    if os.path.exists(sess_path):
        try:
            out["session"] = json.load(open(sess_path))
        except (OSError, ValueError):
            pass
    fp_path = os.path.join(run_dir, "fingerprint_diff.json")
    if os.path.exists(fp_path):
        try:
            out["fingerprint"] = json.load(open(fp_path))
        except (OSError, ValueError):
            pass
    pm_path = os.path.join(run_dir, "presentmon_target.json")
    if os.path.exists(pm_path):
        try:
            out["presentmon_target"] = json.load(open(pm_path))
        except (OSError, ValueError):
            pass
    return out


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Q3Diag dashboard</title>
<style>
:root { color-scheme: light dark; }
body { font: 14px/1.4 ui-monospace, Consolas, monospace; margin: 0; padding: 16px;
       background: light-dark(#f6f7f9, #111318); color: light-dark(#1a1d23, #e6e8eb); }
h1 { font-size: 16px; margin: 0 0 4px; }
#meta { color: light-dark(#666, #999); margin-bottom: 4px; }
#where { color: light-dark(#666, #999); font-size: 12px; margin-bottom: 12px; white-space: pre-wrap; }
#banner { display: none; background: #b3261e; color: #fff; padding: 8px 12px; border-radius: 6px;
          margin-bottom: 12px; white-space: pre-wrap; }
#banner.show { display: block; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px;
        margin-bottom: 16px; align-items: stretch; }
.tile { background: light-dark(#fff, #1b1e25); border: 1px solid light-dark(#ddd, #2a2e37);
        border-radius: 8px; padding: 10px 12px 12px; display: flex; flex-direction: column; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
           color: light-dark(#777, #8b93a0); margin-bottom: 4px; }
.tile .v { font-size: 22px; font-weight: 600; line-height: 1.15; word-break: break-word;
           overflow-wrap: anywhere; }
.tile .v.long { font-size: 15px; }
.tile .v.xlong { font-size: 12px; }
.tile .e { font-size: 11.5px; line-height: 1.35; color: light-dark(#666, #9096a0); margin-top: 6px;
           font-family: ui-sans-serif, system-ui, sans-serif; }
.tile.warn .v { color: #c9761b; }
.tile.bad .v { color: #b3261e; }
.tile.wide { grid-column: 1 / -1; }
.cores { display: grid; grid-template-columns: repeat(auto-fit, minmax(96px, 1fr)); gap: 6px 12px; }
.core { display: flex; align-items: center; gap: 6px; font-size: 11px; }
.core .cl { width: 26px; color: light-dark(#777, #8b93a0); }
.core .cb { flex: 1; height: 6px; border-radius: 3px; overflow: hidden;
            background: light-dark(#e6e8eb, #262a33); }
.core .cb i { display: block; height: 100%; background: #4c8bf5; }
.core .cb i.warn { background: #c9761b; }
.core .cb i.bad { background: #b3261e; }
.core .cv { width: 34px; text-align: right; color: light-dark(#444, #c8ccd3); }
#cpu { color: light-dark(#666, #999); font-size: 11px; margin: 4px 0 12px; }
canvas { width: 100%; height: 140px; background: light-dark(#fff, #1b1e25);
         border: 1px solid light-dark(#ddd, #2a2e37); border-radius: 8px; }
#cpuchart { height: 90px; }
#log { color: light-dark(#666, #999); font-size: 12px; margin-top: 8px; }
</style></head>
<body>
<h1>Q3Diag live dashboard</h1>
<div id="meta">run: <span id="run_id">-</span> | last update <span id="updated_at">-</span></div>
<div id="where">-</div>
<div id="banner"></div>
<div class="grid" id="grid"></div>
<canvas id="chart" width="800" height="140"></canvas>
<canvas id="cpuchart" width="800" height="90"></canvas>
<div id="cpu">CPU % (last ~3 min): headset = solid, PC = dashed</div>
<div id="log">waiting for data...</div>
<script>
// [key, short label, plain-language explanation, formatter, status-class fn]
const TILES = [
  ["rssi", "Wi-Fi signal (RSSI)", "How strong the headset's Wi-Fi signal is, in dBm. Closer to 0 is stronger: roughly -30 excellent, -60 usable, -70 or below weak/marginal.",
    v => v + " dBm", v => v !== null && v < -65 ? "warn" : ""],
  ["tx_link_mbps", "Radio link rate", "The PHY rate the Wi-Fi radio negotiated with the AP. This is a ceiling, not the actual data flowing -- see Delivered rate.",
    v => v + " Mbps", () => ""],
  ["delivered_mbps", "Delivered rate", "The video data rate landing on the headset, measured from Wi-Fi byte counters and averaged over the last ~6s so normal VBR/burst jitter doesn't dominate the number.",
    v => v + " Mbps", () => ""],
  ["retry_pct", "Wi-Fi retry rate", "Percent of Wi-Fi packets that needed a retransmit (missed ACK), averaged over the last ~10s so it isn't jumpy. Some retries are normal; above ~5% suggests interference or a weak link.",
    v => v + "%", v => v > 5 ? "bad" : v > 2 ? "warn" : ""],
  ["lost_pct", "Wi-Fi packet loss", "Percent of packets dropped after retries ran out, averaged over the last ~10s. Should stay near 0%; anything sustained above ~0.1% is a real problem.",
    v => v + "%", v => v > 0.1 ? "bad" : v > 0.02 ? "warn" : ""],
  ["enc_util_pct", "PC video encoder load", "How busy the PC's NVENC hardware video encoder is (0-100%). Near 0 while streaming can mean the encoder stalled.",
    v => v + "%", () => ""],
  ["gpu_util_pct", "PC GPU load", "Overall PC GPU utilization (0-100%), from the game rendering plus the encoder.",
    v => v + "%", () => ""],
  ["pc_cpu_pct", "PC CPU load", "Whole-machine CPU utilization on the PC (0-100%), from the Processor(_Total) counter. In a standalone session the PC renders nothing, so this should sit near idle -- a busy PC there is something else running.",
    v => v + "%", v => v > 85 ? "bad" : v > 70 ? "warn" : ""],
  ["pc_tcp_retrans_per_s", "PC network retransmits", "TCP segments per second the PC had to resend. Sustained spikes point to network congestion on the PC side.",
    v => v + "/s", v => v > 20 ? "bad" : v > 5 ? "warn" : ""],
  ["tcp_retrans_segs", "Headset TCP retransmits", "Total TCP segments the headset has had to resend so far this session (running total, not a rate).",
    v => v, () => ""],
  ["ping_replies", "Ping replies received", "How many PC-to-headset pings have been answered so far this session -- a basic 'is the link alive' counter.",
    v => v, () => ""],
  ["ping_timeouts", "Ping timeouts", "PC-to-headset pings that got no reply. Should stay at 0; any timeout means the headset missed a beat.",
    v => v, v => v > 0 ? "bad" : ""],
  ["cpu_pct", "Headset CPU load", "Whole-device CPU utilization on the headset, from /proc/stat deltas every ~2 s. This is the machine that actually renders a standalone title.",
    v => v + "%", v => v > 85 ? "bad" : v > 70 ? "warn" : ""],
  ["soc_c", "Headset chip temp", "The headset's main chip (SoC) temperature in Celsius. Thermal throttling on Quest 3 typically starts around 89-92 C.",
    v => v + " °C", v => v > 75 ? "bad" : v > 68 ? "warn" : ""],
  ["sta_tx_power_dbm", "Headset TX power", "How much transmit power the headset's Wi-Fi radio is using, in dBm -- rises automatically as the link gets harder to maintain.",
    v => v + " dBm", () => ""],
  ["hmd_state", "Headset worn?", "Whether the headset's proximity sensor thinks it's being worn right now (from the mount-state sensor).",
    v => v, () => ""],
  ["p2p0_tx_errs", "Controller link errors", "Transmit errors on the dedicated Wi-Fi link the touch controllers use (separate from the main headset Wi-Fi). Should be 0.",
    v => v, v => v > 0 ? "warn" : ""],
  ["vrapi", "Compositor fps / stale", "Frames-per-second the headset's compositor is actually rendering, and how many frames were 'stale' (repeated because a new one didn't arrive in time) in the last second.",
    v => v, () => ""],
  ["presentmon_target_display", "Game exe found", "Which PC process PresentMon locked onto for game frame-rate capture, confirmed by name and PID (not guessed) -- see the game-name prompt at session start.",
    v => v, v => (typeof v === "string" && v.startsWith("not found")) ? "warn" : ""],
];

function presentmonTargetDisplay(pm) {
  if (!pm) return undefined;
  if (pm.pid) return pm.process + " (PID " + pm.pid + ")";
  if (pm.searching) return "searching for '" + (pm.hint || pm.target || "") + "'...";
  return "not found: '" + (pm.hint || pm.target || "") + "'";
}

function fmtClass(text) {
  const s = String(text);
  if (s.length > 18) return "xlong";
  if (s.length > 8) return "long";
  return "";
}

function fmtMB(mb) {
  return mb >= 1024 ? (mb / 1024).toFixed(2) + " GB" : Math.round(mb) + " MB";
}

function render(d) {
  d.presentmon_target_display = presentmonTargetDisplay(d.presentmon_target);
  document.getElementById("run_id").textContent = d.run_id;
  document.getElementById("updated_at").textContent = d.updated_at + (d.exists ? "" : "  (run not found yet)");
  const r = d.data_rates || {};
  const perMin = (r.samplers || 0) + (d.presentmon_on ? (r.presentmon || 0) : 0) +
                 (d.trace_on ? (r.trace || 0) : 0);
  const parts = ["samplers ~" + (r.samplers || 0).toFixed(2) + " MB/min"];
  if (d.presentmon_on) parts.push("PresentMon ~" + (r.presentmon || 0) + " MB/min -> presentmon.csv");
  if (d.trace_on) parts.push("WPR trace ~" + ((r.trace || 0) / 1024).toFixed(2) + " GB/min -> trace.etl (staged in %TEMP%)");
  document.getElementById("where").textContent =
    (d.exists ? "saving to " + d.run_dir + "   |   written so far " + fmtMB(d.written_mb || 0)
              : "run folder not created yet: " + (d.run_dir || "?")) +
    "   |   " + parts.join("  \u00b7  ") + "   (~" + perMin.toFixed(2) + " MB/min total)";
  const banner = document.getElementById("banner");
  const fp = d.fingerprint;
  if (fp && fp.flags && fp.flags.length) {
    banner.className = "show";
    banner.textContent = "FINGERPRINT DRIFT vs baseline (" + fp.tag + "): " +
      fp.flags.map(f => f.metric + " " + f.baseline + " -> " + f.current).join("  |  ");
  } else {
    banner.className = "";
  }
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  for (const [key, label, explain, fmt, cls] of TILES) {
    const v = d[key];
    const hasVal = v !== undefined && v !== null && v !== "";
    const text = hasVal ? fmt(v) : "-";
    const div = document.createElement("div");
    div.className = "tile " + (hasVal ? cls(v) : "");
    div.innerHTML = '<div class="k">' + label + '</div><div class="v ' + fmtClass(text) + '">' +
      text + '</div><div class="e">' + explain + "</div>";
    grid.appendChild(div);
  }
  const hcpu = coreTile("Headset CPU per core", d.cpu_core_labels, d.cpu_cores,
    "Per-core utilization on the headset (/proc/stat, ~2 s). One hot core with an idle GPU is a single-threaded workload, not a GPU limit.");
  if (hcpu) grid.appendChild(hcpu);
  const pcpu = coreTile("PC CPU per core", d.pc_core_labels, d.pc_cpu_cores,
    "Per-core utilization on the PC. One pegged core while the total looks calm means a single thread is the limit.");
  if (pcpu) grid.appendChild(pcpu);
  drawChart(d.rate_series || []);
  drawCpu(d.cpu_history || [], d.pc_cpu_history || []);
  document.getElementById("log").textContent = d.session
    ? "session: " + (d.session.proc || "") + (d.session.end_dev_s ? "  (ended)" : "  (live)")
    : "no session detected yet";
}

function coreTile(title, labels, values, explain) {
  if (!labels || !labels.length) return null;
  const rows = labels.map((lab, i) => {
    const v = parseFloat(values[i]);
    const pct = isNaN(v) ? 0 : Math.max(2, Math.min(100, v));
    const cls = isNaN(v) ? "" : v >= 90 ? "bad" : v >= 70 ? "warn" : "";
    return '<div class="core"><span class="cl">' + lab + '</span><span class="cb"><i class="' +
           cls + '" style="width:' + pct + '%"></i></span><span class="cv">' +
           (isNaN(v) ? "-" : v.toFixed(0) + "%") + '</span></div>';
  }).join("");
  const div = document.createElement("div");
  div.className = "tile wide";
  div.innerHTML = '<div class="k">' + title + '</div><div class="cores">' + rows +
                  '</div><div class="e">' + explain + '</div>';
  return div;
}

function drawCpu(hs, pc) {
  const c = document.getElementById("cpuchart");
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  const line = (ys, color, dash) => {
    if (!ys || ys.length < 2) return;
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.setLineDash(dash || []);
    ctx.beginPath();
    let started = false;
    ys.forEach((v, i) => {
      if (v === null || v === undefined) { started = false; return; }
      const x = (i / (ys.length - 1)) * c.width;
      const y = c.height - (Math.max(0, Math.min(100, v)) / 100) * (c.height - 14) - 7;
      if (started) { ctx.lineTo(x, y); } else { ctx.moveTo(x, y); started = true; }
    });
    ctx.stroke();
    ctx.setLineDash([]);
  };
  ctx.fillStyle = "#888"; ctx.font = "11px monospace";
  ctx.fillText("100%", 4, 12);
  ctx.fillText("0%", 4, c.height - 4);
  line(hs, "#4c8bf5");
  line(pc, "#c9761b", [6, 4]);
}

function drawChart(series) {
  const c = document.getElementById("chart");
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  if (series.length < 2) return;
  const ys = series.map(p => p[1]);
  const max = Math.max(...ys, 1), min = 0;
  ctx.strokeStyle = "#4c8bf5"; ctx.lineWidth = 2; ctx.beginPath();
  series.forEach((p, i) => {
    const x = (i / (series.length - 1)) * c.width;
    const y = c.height - ((p[1] - min) / (max - min || 1)) * (c.height - 10) - 5;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "#888"; ctx.font = "11px monospace";
  ctx.fillText(max.toFixed(0) + " Mbps", 4, 12);
}

async function tick() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    render(await r.json());
  } catch (e) {
    document.getElementById("log").textContent = "fetch error: " + e;
  }
  setTimeout(tick, 1000);
}
tick();
</script>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/status"):
            data = json.dumps(status(RUN_ID)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        else:
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def make_server(run_id, port):
    """Build (but don't start) a dashboard HTTP server for run_id. Split out from __main__ so a caller
    that already has a Python interpreter running -- wizard.py, importing this module directly -- can
    serve_forever() it on a background thread instead of spawning `python dashboard.py` as a
    subprocess. That subprocess path breaks under a frozen/bundled build: sys.executable there is the
    bundled exe itself, not a plain python.exe that can be handed a sibling .py file to run."""
    global RUN_ID
    RUN_ID = run_id
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    port = int(qsite.get("dashboard_port", 8765))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    srv = make_server(sys.argv[1], port)
    print(f"dashboard: http://127.0.0.1:{port}/  (run_id={RUN_ID}, Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
