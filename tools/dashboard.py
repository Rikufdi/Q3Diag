#!/usr/bin/env python3
"""dashboard.py - live local web dashboard for a running `cell.py monitor` session.

Usage:
  python tools/cell.py monitor <run_id> 10800      # in one shell
  python tools/dashboard.py <run_id> [--port 8765] # in another; open http://127.0.0.1:8765/

Reads the same TSVs `monitor` is appending to (quest_wifi_samples.tsv, quest_net_samples.tsv,
quest_env_samples.tsv, pc_samples.tsv, vr_api_logcat.txt, ping_samples.txt) and re-derives a live
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
    rows = cell.read_tsv(os.path.join(run_dir, "quest_wifi_samples.tsv"))
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


def status(run_id):
    run_dir = os.path.join(BASE, "runs", run_id)
    out = {"run_id": run_id, "updated_at": time.strftime("%H:%M:%S"), "exists": os.path.isdir(run_dir)}
    if not out["exists"]:
        return out
    out.update(_last_wifi_rate(run_dir))
    net = cell.tail_row(os.path.join(run_dir, "quest_net_samples.tsv"))
    out.update({"wlan0_rx_errs": net.get("wlan0_rx_errs"), "wlan0_rx_drop": net.get("wlan0_rx_drop"),
                "tcp_retrans_segs": net.get("tcp_retrans_segs"), "p2p0_tx_errs": net.get("p2p0_tx_errs"),
                "p2p0_rx_errs": net.get("p2p0_rx_errs")})
    env = cell.tail_row(os.path.join(run_dir, "quest_env_samples.tsv"))
    out.update({"soc_c": env.get("soc_usr_c"), "gpuss_c": env.get("gpuss_max_c"),
                "sta_tx_power_dbm": env.get("sta_tx_power_dbm"), "hmd_state": env.get("hmd_state")})
    pc = cell.tail_row(os.path.join(run_dir, "pc_samples.tsv"))
    out.update({"enc_util_pct": pc.get("enc_util_pct"), "gpu_util_pct": pc.get("gpu_util_pct"),
                "pc_tcp_retrans_per_s": pc.get("tcp_retrans_per_s"), "streamer_ws_mb": pc.get("streamer_ws_mb")})
    out["vrapi"] = cell.vrapi_tail(os.path.join(run_dir, "vr_api_logcat.txt"))
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
    return out


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Q3Diag dashboard</title>
<style>
:root { color-scheme: light dark; }
body { font: 14px/1.4 ui-monospace, Consolas, monospace; margin: 0; padding: 16px;
       background: light-dark(#f6f7f9, #111318); color: light-dark(#1a1d23, #e6e8eb); }
h1 { font-size: 16px; margin: 0 0 4px; }
#meta { color: light-dark(#666, #999); margin-bottom: 12px; }
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
canvas { width: 100%; height: 140px; background: light-dark(#fff, #1b1e25);
         border: 1px solid light-dark(#ddd, #2a2e37); border-radius: 8px; }
#log { color: light-dark(#666, #999); font-size: 12px; margin-top: 8px; }
</style></head>
<body>
<h1>Q3Diag live dashboard</h1>
<div id="meta">run: <span id="run_id">-</span> | last update <span id="updated_at">-</span></div>
<div id="banner"></div>
<div class="grid" id="grid"></div>
<canvas id="chart" width="800" height="140"></canvas>
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
  ["pc_tcp_retrans_per_s", "PC network retransmits", "TCP segments per second the PC had to resend. Sustained spikes point to network congestion on the PC side.",
    v => v + "/s", v => v > 20 ? "bad" : v > 5 ? "warn" : ""],
  ["tcp_retrans_segs", "Headset TCP retransmits", "Total TCP segments the headset has had to resend so far this session (running total, not a rate).",
    v => v, () => ""],
  ["ping_replies", "Ping replies received", "How many PC-to-headset pings have been answered so far this session -- a basic 'is the link alive' counter.",
    v => v, () => ""],
  ["ping_timeouts", "Ping timeouts", "PC-to-headset pings that got no reply. Should stay at 0; any timeout means the headset missed a beat.",
    v => v, v => v > 0 ? "bad" : ""],
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
];

function fmtClass(text) {
  const s = String(text);
  if (s.length > 18) return "xlong";
  if (s.length > 8) return "long";
  return "";
}

function render(d) {
  document.getElementById("run_id").textContent = d.run_id;
  document.getElementById("updated_at").textContent = d.updated_at + (d.exists ? "" : "  (run not found yet)");
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
  drawChart(d.rate_series || []);
  document.getElementById("log").textContent = d.session
    ? "session: " + (d.session.proc || "") + (d.session.end_dev_s ? "  (ended)" : "  (live)")
    : "no session detected yet";
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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    RUN_ID = sys.argv[1]
    port = int(qsite.get("dashboard_port", 8765))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}/  (run_id={RUN_ID}, Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
