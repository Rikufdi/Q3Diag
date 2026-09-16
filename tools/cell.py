#!/usr/bin/env python3
"""cell.py - one-cell capture + results orchestration.

Usage:
  python cell.py serial                          # print the resolved wireless-adb endpoint
  python cell.py capture <run_id> [duration_s]   # pktmon + device sampler + ping + VrApi logcat
  python cell.py results <run_id> [overlay.json] # analyze + deltas + ovr + write results

overlay.json keys (optional): fps, lat_total, lat_game, lat_encode, lat_network, lat_decode,
                              bitrate_mbps, wifi_mbps

Per-cell device-side instrumentation (all optional files, written into runs/<run_id>/):
  quest_wifi_samples.tsv  MAC counters + link state            (Sample-Quest.ps1 core)
  quest_net_samples.tsv   /proc/net/dev + /proc/net/snmp       (wlan0 bytes/errs, TCP retransmits)
  quest_env_samples.tsv   OculusWifi STA state + thermals + GPU busy
  sf_latency_samples.tsv  SurfaceFlinger --latency advance of the active panel layer
  ping_samples.txt        PC->headset ICMP at 1 Hz (under-load latency/jitter, previously missing)
  vr_api_logcat.txt       logcat -s VrApi: per-second FPS/Stale/TW/App/CFL/ICFL/PoseAge (any stack)
  clock.json              headset<->PC clock offset + device uptime at capture time
"""
import sys, os, re, time, json, csv, socket, subprocess
import qsite

ADB = qsite.path("adb")
QUEST_IP = qsite.get("quest_ip")
SAMPLER = qsite.script("quest_sampler")
PC_SAMPLER = qsite.script("pc_sampler")
BASE = qsite.base_dir()
TASK = qsite.get("elev_task", "PCVR-Elev")
RES = os.path.join(qsite.TOOLS_DIR, "elev-do-res.txt")
CMD = os.path.join(qsite.TOOLS_DIR, "elev-do-cmd.txt")
OVR_DIR = qsite.get("ovr_metrics_dir")

# results.csv schema: identity columns + everything the reductions can produce.
ID_COLS = ["run_id", "stack", "codec", "bitrate_mbps", "content", "band"]
MEAS_COLS = [
    "wire_tx_mbps", "wire_rx_mbps", "wire_pkts_per_s", "retry_rate_pct", "lost_rate_pct",
    "quest_tx_packets_delta", "quest_retried_tx_delta", "quest_lost_tx_delta", "quest_rx_packets_delta",
    "overlay_fps", "overlay_latency_total_ms", "overlay_latency_game_ms", "overlay_latency_encode_ms",
    "overlay_latency_network_ms", "overlay_latency_decode_ms", "overlay_bitrate_mbps", "overlay_wifi_mbps",
    "ovr_rows", "ovr_window_s", "ovr_avg_fps", "ovr_fps_min", "ovr_seconds_below_85fps", "ovr_stale_frames_window",
    "ovr_stale_seconds", "ovr_max_consecutive_stale", "ovr_max_repeated_frames", "ovr_skipped_frames",
    "ovr_throttle_seconds", "ovr_battery_temp_c", "ovr_gpu_util_pct", "ovr_cpu_util_pct",
    "ovr_source_file", "ovr_stale", "ovr_file_mtime_dev_s",
    "ping_n", "ping_rtt_p50_ms", "ping_rtt_p90_ms", "ping_rtt_p95_ms", "ping_rtt_p99_ms",
    "ping_rtt_max_ms", "ping_loss_pct",
    "vr_api_lines", "vr_api_asw_lines", "vr_api_lines_skipped", "vr_api_pid",
    "vr_api_lines_ignored_other_pid", "vr_api_fps_mean", "vr_api_fps_min",
    "vr_api_seconds_below_85fps", "vr_api_stale_total", "vr_api_stale_seconds",
    "vr_api_stale_max_consecutive", "vr_api_tw_ms_mean", "vr_api_app_ms_mean",
    "vr_api_cfl_ms_mean", "vr_api_icfl_p95_ms_mean", "vr_api_icfl_p95_ms_max",
    "vr_api_pose_age_p95_max", "vr_api_temp_c_max", "vr_api_gpu_pct_mean",
    "tcp_retrans_segs", "tcp_in_errs", "wlan0_rx_errs", "wlan0_rx_drop", "wlan0_tx_errs", "wlan0_tx_drop",
    "p2p0_rx_mbps", "p2p0_tx_mbps", "p2p0_rx_errs", "p2p0_rx_drop", "p2p0_tx_errs", "p2p0_tx_drop",
    "cm_snapshots", "cm_events_session", "cm_ctrl_last_left", "cm_ctrl_last_right",
    "cm_ctrl_connected_active", "cm_ctrl_connected_inactive", "cm_ctrl_connecting", "cm_ctrl_searching",
    "cm_ctrl_disabled", "cm_p2p_gc_connect", "cm_p2p_channel_switch", "cm_p2p_go_create_success",
    "cm_low_latency_toggle", "cm_concurrency_change",
    "cm_map_share_sends", "cm_map_share_kib", "cm_map_share_gap_median_s", "cm_map_share_gap_max_s",
    "pc_samples", "pc_enc_util_mean", "pc_enc_util_min", "pc_enc_util_max", "pc_gpu_util_mean",
    "pc_tcp_retrans_mean", "pc_tcp_retrans_max_per_s", "pc_tcp_sent_mean_per_s",
    "decay_rate_median_mbps", "decay_episodes", "decay_first_start_min", "decay_total_min", "decay_min_mbps",
    "decay_episode_enc_util_mean", "decay_episode_retrans_max", "decay_baseline_enc_util_mean",
    "decay_baseline_retrans_mean",
    "env_samples", "env_soc_max_c", "env_gpu_max_c", "env_cpu_max_c", "env_batt_virt_max_c",
    "env_gpu_busy_mean_pct", "env_sta_tx_power_dbm", "env_sta_rssi_min", "env_sta_rssi_max",
    "sf_samples", "sf_frames_total", "sf_frames_per_sample_mean", "sf_frames_per_sample_min",
    "codec_events_total", "codec_events_session", "codec_starts_session", "codec_restarts_session",
    "passive_avg_down_mbps", "passive_avg_up_mbps",
    "quest_clock_offset_s", "quest_uptime_s", "quest_cell_start_dev_s",
    "quest_session_start_dev_s", "quest_session_min",
]

_serial_cache = None


def monitor(run_id, max_seconds=10800, status_every=30, stack="vd"):
    """Open-ended monitoring of a live session (no pktmon: the elevated task is intentionally absent).
    Samples until killed or max_seconds, prints one status line per status_every, and detects the
    streaming client's start/stop so the reduction can window on the session instead of the monitor."""
    run_dir = os.path.join(BASE, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    if not os.path.exists(os.path.join(run_dir, "settings.json")):
        json.dump({"run_id": run_id, "stack": stack, "codec": "live", "bitrate_mbps": None,
                   "content": "motion", "band": "6g"},
                  open(os.path.join(run_dir, "settings.json"), "w"), indent=1)

    ser = adb_serial()
    clock = clock_sample(ser)
    off = clock.get("offset_s") or 0.0
    start_dev = time.time() + off
    # Write the window immediately: a hard kill (hub stop) skips the finally block, and without this file
    # the run has no clock offset and no cell window in its results.
    clock["cell_start_dev_s"] = round(start_dev, 3)
    json.dump(clock, open(os.path.join(run_dir, "clock.json"), "w"), indent=1)
    print(f"monitor started: {run_id} serial={ser} clock_offset={off:.3f}s uptime={clock.get('dev_uptime_s')}")

    files = {k: os.path.join(run_dir, v) for k, v in
             {"wifi": "quest_wifi_samples.tsv", "net": "quest_net_samples.tsv",
              "env": "quest_env_samples.tsv", "sf": "sf_latency_samples.tsv",
              "layers": "sf_layers.log", "logcat": "vr_api_logcat.txt",
              "cm": "cm_wifi_snapshots.txt", "pc": "pc_samples.tsv",
              "ping": "ping_samples.txt", "session": "session.json"}.items()}

    sampler = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SAMPLER,
         "-Adb", ADB, "-Serial", ser, "-OutFile", files["wifi"], "-NetFile", files["net"],
         "-EnvFile", files["env"], "-CmFile", files["cm"],
         "-Seconds", str(max_seconds + 60)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logcat = subprocess.Popen([ADB, "-s", ser, "logcat", "-v", "time", "-s", "VrApi"],
                              stdout=open(files["logcat"], "a", encoding="utf-8"), stderr=subprocess.DEVNULL)
    pc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PC_SAMPLER,
                           "-OutFile", files["pc"], "-Seconds", str(max_seconds + 60)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ping = subprocess.Popen(["ping", "-n", str(max_seconds), "-w", "1000", QUEST_IP],
                            stdout=open(files["ping"], "a"), stderr=subprocess.DEVNULL)

    start_dev = time.time() + off
    sf_layer, sf_prev, sf_next, sf_found_at, session = None, 0, 0.0, 0.0, {}
    samples = {"wifi_prev": None, "ping_replies": 0, "ping_timeouts": 0, "sf_frames": 0.0}
    last_status = 0.0
    try:
        while time.time() - (start_dev - off) < max_seconds:
            now = time.time()
            if now >= sf_next:
                if sf_layer is None or now - sf_found_at > 20:
                    found = find_sf_layer(ser, settle_s=1.0)
                    if found != sf_layer and found:
                        open(files["layers"], "a").write(f"{now - off:.0f}\t{found}\n")
                    sf_layer, sf_found_at = found, now
                if sf_layer:
                    cur = sf_max_actual(ser, sf_layer)
                    frames = round((cur - sf_prev) / 11111111, 2) if (sf_prev and cur > sf_prev) else ""
                    if frames:
                        samples["sf_frames"] += float(frames)
                    if frames or sf_prev:
                        new = (not os.path.exists(files["sf"])) or os.path.getsize(files["sf"]) == 0
                        with open(files["sf"], "a", encoding="utf-8") as f:
                            if new:
                                f.write("timestamp\tnonzero_rows\tmax_actual_present_ns\tframes_since_prev\n")
                            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}.000\t"
                                    f"{127 if cur else 0}\t{cur}\t{frames}\n")
                    sf_prev = max(sf_prev, cur)
                sf_next = now + 1.0

            if now - last_status >= status_every:
                last_status = now
                procs = _adb("-s", ser, "shell",
                             "ps -A -o NAME | grep -E 'VirtualDesktop|xrstreamingclient' | tr '\\n' ' '").strip()
                if procs and not session:
                    session = {"stack": stack, "proc": procs, "start_dev_s": round(time.time() + off, 3)}
                    json.dump(session, open(files["session"], "w"), indent=1)
                    print(f"[{time.strftime('%H:%M:%S')}] SESSION DETECTED: {procs}")
                elif session and not procs and "end_dev_s" not in session:
                    session["end_dev_s"] = round(time.time() + off, 3)
                    json.dump(session, open(files["session"], "w"), indent=1)
                    print(f"[{time.strftime('%H:%M:%S')}] SESSION ENDED ({(session['end_dev_s']-session['start_dev_s'])/60:.1f} min)")
                wifi = tail_row(files["wifi"], 10)
                net = tail_row(files["net"], 30)
                vrapi = vrapi_tail(files["logcat"])
                pr, pt = ping_counts(files["ping"])
                print(f"[{time.strftime('%H:%M:%S')}] t={(time.time()-(start_dev-off))/60:6.1f}min "
                      f"rssi={wifi.get('rssi','-')} link={wifi.get('tx_link_mbps','-')} "
                      f"tx={wifi.get('tx_success','-')} retry={wifi.get('tx_retries','-')} lost={wifi.get('tx_lost','-')} "
                      f"retrans={net.get('tcp_retrans_segs','-')} | vr {vrapi} | ping {pr}/{pr+pt} "
                      f"| p2p0={float(net.get('p2p0_tx_bytes') or 0)/1e6:.0f}MB err={net.get('p2p0_tx_errs','-')}/{net.get('p2p0_rx_errs','-')} "
                      f"| sf={sf_layer or 'none'} frames={samples['sf_frames']:.0f}", flush=True)
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("monitor interrupted")
    finally:
        for p in (logcat, ping, sampler, pc):
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        clock["cell_start_dev_s"] = round(start_dev, 3)
        clock["cell_end_dev_s"] = round(time.time() + off, 3)
        if session and "end_dev_s" not in session:
            session["end_dev_s"] = clock["cell_end_dev_s"]
            json.dump(session, open(files["session"], "w"), indent=1)
        json.dump(clock, open(os.path.join(run_dir, "clock.json"), "w"), indent=1)
        print("monitor stopped; artifacts in " + run_dir, flush=True)


def tail_row(path, n=1):
    """Last complete TSV row as a dict (the sampler may be mid-append)."""
    if not os.path.exists(path):
        return {}
    lines = _read_text(path).splitlines()
    if len(lines) < 2:
        return {}
    hdr = lines[0].split("\t")
    for line in reversed(lines[-n:]):
        f = line.split("\t")
        if len(f) == len(hdr):
            return dict(zip(hdr, f))
    return {}


def vrapi_tail(path, n=4000):
    """Compact 'fps/stale' from the newest VrApi line in the streamed logcat file."""
    if not os.path.exists(path):
        return "fps=-,stale=-"
    txt = _read_text(path)
    if not txt:
        return "fps=-,stale=-"
    lines = txt[-(n * 150):].splitlines()
    for line in reversed(lines):
        if " FPS=" in line:
            fps = re.search(r"FPS=(\d+)/", line)
            stale = re.search(r"(?:^|,)Stale=(\d+)", line)
            return f"fps={fps.group(1) if fps else '-'},stale={stale.group(1) if stale else '-'}"
    return "fps=-,stale=-"


def ping_counts(path):
    if not os.path.exists(path):
        return 0, 0
    raw = _read_text(path)
    return len(re.findall(r"time[=<]", raw)), len(re.findall(r"(timed out|unreachable)", raw, re.I))


def _run(args, timeout=None):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout).stdout


def _adb(*args, timeout=None):
    return _run([ADB, *args], timeout=timeout)


def _read_text(path, tries=4, delay=0.2):
    """Read a file another process may be appending to: on Windows an open-for-write by the PowerShell
    sampler surfaces as a sharing violation (PermissionError), so retry briefly and then give up quietly."""
    for _ in range(tries):
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                return f.read()
        except OSError:
            time.sleep(delay)
    return ""


def read_tsv(path):
    txt = _read_text(path)
    return list(csv.DictReader(txt.splitlines(), delimiter="\t")) if txt.strip() else []


def adb_serial(refresh=False):
    """Resolve the wireless-adb endpoint. The 5555 tcpip port dies with every headset reboot;
    the Android 11+ Wireless Debugging endpoint only advertises over mDNS."""
    global _serial_cache
    if _serial_cache and not refresh:
        return _serial_cache

    def attached(prefix=None):
        for line in _adb("devices").splitlines():
            if re.search(r"\sdevice\s*$", line):
                s = line.split("\t")[0].strip()
                if not prefix or s.startswith(prefix):
                    return s
        return None

    ser = attached(QUEST_IP)
    if not ser:
        _adb("connect", f"{QUEST_IP}:5555")
        ser = attached(QUEST_IP)
    if not ser:
        svc = ""
        for line in _adb("mdns", "services").splitlines():
            if "_adb-tls-connect" in line:
                svc = line.split()[-1]
        if svc:
            _adb("connect", svc)
            ser = attached(QUEST_IP)
    if not ser:
        raise RuntimeError(f"no Quest reachable: tried {QUEST_IP}:5555 and mDNS _adb-tls-connect")
    _serial_cache = ser
    return ser


def elev(cmd):
    if os.path.exists(RES):
        os.remove(RES)
    with open(CMD, "w") as f:
        f.write(cmd)
    r = subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        return "NO-TASK: " + (r.stderr or r.stdout).strip().replace("\n", " ")[:200]
    for _ in range(40):
        if os.path.exists(RES):
            return open(RES, encoding="utf-8-sig").read().strip()
        time.sleep(1)
    return "TIMEOUT"


# ---------------------------------------------------------------- clock / layers
def clock_sample(ser):
    """headset wall clock vs PC wall clock (the headset is ~0.5 s behind; needed to align
    device timelines with the pcap) plus the headset uptime domain used by SF/gfxinfo."""
    t0 = time.time()
    dev = _adb("-s", ser, "shell", "date +%s.%N").strip()
    t1 = time.time()
    uptime = _adb("-s", ser, "shell", "cat /proc/uptime").strip().split()[0] if dev else ""
    try:
        return {"dev_epoch_s": float(dev), "pc_epoch_s": round((t0 + t1) / 2, 3),
                "offset_s": round(float(dev) - (t0 + t1) / 2, 3), "rtt_s": round(t1 - t0, 3),
                "dev_uptime_s": float(uptime) if uptime else None}
    except ValueError:
        return {}


def sf_list(ser):
    return [l.strip() for l in _adb("-s", ser, "shell", "dumpsys SurfaceFlinger --list").splitlines() if l.strip()]


def sf_max_actual(ser, layer):
    """Newest actualPresentTime (ns) for a layer; 0 when the layer has no frames."""
    out = _adb("-s", ser, "shell", f"dumpsys SurfaceFlinger --latency '{layer}'")
    best = 0
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 3 and f[1].isdigit():
            best = max(best, int(f[1]))
    return best


def find_sf_layer(ser, settle_s=2.0):
    """Pick the panel layer that is actually advancing (the compositor's vr_compositor_* layers are
    HWC layers, invisible to SurfaceFlinger, so only 2D panel/app layers are observable here)."""
    cand = re.compile(r"panel_app|vrshell|VirtualDesktop|xrstreaming|AndroidPanelLayer|SurfaceView")
    layers = [l for l in sf_list(ser) if cand.search(l)]
    first = {l: sf_max_actual(ser, l) for l in layers}
    time.sleep(settle_s)
    second = {l: sf_max_actual(ser, l) for l in layers}
    advancing = [l for l in layers if second[l] > first[l]]
    if not advancing:
        return None
    return max(advancing, key=lambda l: second[l])


# ---------------------------------------------------------------- capture
def capture(run_id, duration=150):
    run_dir = os.path.join(BASE, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    ser = adb_serial()

    clock_before = clock_sample(ser)
    json.dump(clock_before, open(os.path.join(run_dir, "clock.json"), "w"), indent=1)
    if clock_before:
        print(f"clock offset (headset - PC): {clock_before['offset_s']} s, rtt {clock_before['rtt_s']} s")

    sf_layer = find_sf_layer(ser)
    print("sf layer:", sf_layer or "(none advancing -> SurfaceFlinger sampling skipped)")

    print("pktmon-start:", elev(f"pktmon-start {run_dir}"))

    sampler = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SAMPLER,
         "-Adb", ADB, "-Serial", ser,
         "-OutFile", os.path.join(run_dir, "quest_wifi_samples.tsv"),
         "-NetFile", os.path.join(run_dir, "quest_net_samples.tsv"),
         "-EnvFile", os.path.join(run_dir, "quest_env_samples.tsv"),
         "-SfFile", os.path.join(run_dir, "sf_latency_samples.tsv") if sf_layer else "",
         "-SfLayer", sf_layer or "",
         "-Seconds", str(duration + 20)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ping = subprocess.Popen(
        ["ping", "-n", str(duration), "-w", "1000", QUEST_IP],
        stdout=open(os.path.join(run_dir, "ping_samples.txt"), "w"), stderr=subprocess.DEVNULL)

    logcat = subprocess.Popen(
        [ADB, "-s", ser, "logcat", "-v", "time", "-s", "VrApi"],
        stdout=open(os.path.join(run_dir, "vr_api_logcat.txt"), "w", encoding="utf-8"),
        stderr=subprocess.DEVNULL)

    # logcat -s VrApi dumps the buffer backlog first (~5 min) - remember the device-clock window so the
    # reduction keeps only in-cell frames.
    off = clock_before.get("offset_s") or 0.0
    cell_start_dev = time.time() + off

    print(f"capturing {duration}s...")
    time.sleep(duration)

    print("pktmon-stop:", elev(f"pktmon-stop {run_dir}"))
    for p in (logcat, ping):
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sampler.wait()

    clock_after = clock_sample(ser)
    clock_after["offset_before_s"] = clock_before.get("offset_s")
    clock_after["cell_start_dev_s"] = round(cell_start_dev, 3)
    clock_after["cell_end_dev_s"] = round(time.time() + (clock_after.get("offset_s") or off), 3)
    json.dump(clock_after, open(os.path.join(run_dir, "clock.json"), "w"), indent=1)
    print("done")


def quest_counters(ser):
    """Cumulative device counters, used to bound a session that was NOT instrumented live."""
    wifi = _adb("-s", ser, "shell", "cmd wifi status")
    dev = _adb("-s", ser, "shell", "cat /proc/net/dev")
    snmp = _adb("-s", ser, "shell", "cat /proc/net/snmp")
    out = {}
    for key, pat in (("tx_success", r"successfulTxPackets:\s*(\d+)"), ("tx_retries", r"retriedTxPackets:\s*(\d+)"),
                     ("tx_lost", r"lostTxPackets:\s*(\d+)"), ("rx_success", r"successfulRxPackets:\s*(\d+)"),
                     ("rssi", r"RSSI:\s*(-?\d+)"), ("tx_link_mbps", r"Tx Link speed:\s*(\d+)Mbps")):
        m = re.search(pat, wifi)
        out[key] = int(m.group(1)) if m else None
    m = re.search(r"(?m)^\s*wlan0:\s*(\d+)(?:\s+\d+){7}\s+(\d+)", dev)
    if m:
        out["wlan0_rx_bytes"], out["wlan0_tx_bytes"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(?m)^Tcp:\s*((?:[\d\-]+\s*){5,})$", snmp)
    if m:
        f = m.group(1).split()
        if len(f) >= 14:
            out["tcp_retrans_segs"], out["tcp_in_errs"] = int(f[11]), int(f[12])
    return out


def passive(run_id, phase, stack="vd"):
    """Zero-perturbation session measurement: `start` snapshots cumulative counters, the session runs with
    NO adb traffic at all, `end` deltas the counters and pulls the OVR CSV the headset writes by itself.
    This is the control arm for 'does live instrumentation change what we measure'."""
    run_dir = os.path.join(BASE, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    ser = adb_serial()
    clock = clock_sample(ser)
    snap = {"phase": phase, "wall": time.strftime("%Y-%m-%d %H:%M:%S"),
            "dev_epoch_s": clock.get("dev_epoch_s"), "pc_epoch_s": clock.get("pc_epoch_s"),
            "offset_s": clock.get("offset_s"), "dev_uptime_s": clock.get("dev_uptime_s"),
            "counters": quest_counters(ser)}
    json.dump(snap, open(os.path.join(run_dir, f"passive_{phase}.json"), "w"), indent=1)
    if phase != "end":
        print(f"baseline captured for {run_id} at {snap['wall']}. Play with NO monitoring running.")
        print(f"when finished:  python tools/cell.py passive {run_id} end")
        return

    start = json.load(open(os.path.join(run_dir, "passive_start.json")))
    a, b = start.get("counters", {}), snap["counters"]
    dur = (snap.get("dev_epoch_s") or 0) - (start.get("dev_epoch_s") or 0)
    res = {"transport": "passive (no adb polling during play)"}
    if dur and dur > 0 and a.get("wlan0_rx_bytes") is not None and b.get("wlan0_rx_bytes") is not None:
        res["quest_session_min"] = round(dur / 60, 1)
        res["passive_avg_down_mbps"] = round((b["wlan0_rx_bytes"] - a["wlan0_rx_bytes"]) * 8 / dur / 1e6, 2)
        res["passive_avg_up_mbps"] = round((b["wlan0_tx_bytes"] - a["wlan0_tx_bytes"]) * 8 / dur / 1e6, 3)
        tx = b["tx_success"] - a["tx_success"]
        tr = b["tx_retries"] - a["tx_retries"]
        tl = b["tx_lost"] - a["tx_lost"]
        res.update({"quest_tx_packets_delta": tx, "quest_retried_tx_delta": tr, "quest_lost_tx_delta": tl,
                    "quest_rx_packets_delta": b["rx_success"] - a["rx_success"],
                    "retry_rate_pct": round(100 * tr / tx, 3) if tx else None,
                    "lost_rate_pct": round(100 * tl / tx, 4) if tx else None,
                    "tcp_retrans_segs": (b.get("tcp_retrans_segs") or 0) - (a.get("tcp_retrans_segs") or 0),
                    "tcp_in_errs": (b.get("tcp_in_errs") or 0) - (a.get("tcp_in_errs") or 0),
                    "quest_clock_offset_s": snap.get("offset_s"), "quest_uptime_s": snap.get("dev_uptime_s"),
                    "quest_cell_start_dev_s": start.get("dev_epoch_s"),
                    "quest_session_start_dev_s": start.get("dev_epoch_s")})

    newest = _adb("-s", ser, "shell", f"ls -t {OVR_DIR} | head -1").strip()
    ovr_csv = os.path.join(run_dir, "ovr_metrics.csv")
    res["ovr_source_file"] = newest or None
    if newest:
        _run([ADB, "-s", ser, "pull", f"{OVR_DIR}/{newest}", ovr_csv])
        mt = _adb("-s", ser, "shell", f"stat -c '%Y' '{OVR_DIR}/{newest}'").strip()
        try:
            mt = float(mt)
            res["ovr_file_mtime_dev_s"] = mt
            res["ovr_stale"] = not (start.get("dev_epoch_s") and mt >= float(start["dev_epoch_s"]) - 300)
        except ValueError:
            res["ovr_stale"] = None
        if os.path.exists(ovr_csv) and not res.get("ovr_stale"):
            res.update(ovr_window(ovr_csv, tail_s=None))

    if not os.path.exists(os.path.join(run_dir, "settings.json")):
        json.dump({"run_id": run_id, "stack": stack, "codec": "passive", "bitrate_mbps": None,
                   "content": "motion", "band": "6g"},
                  open(os.path.join(run_dir, "settings.json"), "w"), indent=1)
    json.dump(res, open(os.path.join(run_dir, "results.json"), "w"), indent=1)
    s = json.load(open(os.path.join(run_dir, "settings.json")))
    csv_upsert(os.path.join(BASE, "results.csv"), ID_COLS, MEAS_COLS,
               {**{c: s.get(c) for c in ID_COLS}, **res})
    print("passive session:", res.get("quest_session_min"), "min | avg down", res.get("passive_avg_down_mbps"),
          "Mbps | retry", res.get("retry_rate_pct"), "% | lost", res.get("quest_lost_tx_delta"),
          "| ovr fps", res.get("ovr_avg_fps"), "stale", res.get("ovr_stale_frames_window"),
          "| ovr_stale", res.get("ovr_stale"))


# ---------------------------------------------------------------- reductions
def ovr_window(csv_path, tail_s=170):
    """Summarise an OVR-metrics CSV (1 Hz rows written by the headset itself).
    tail_s=None summarises the whole file. NOTE: stale_frame_count is a PER-SECOND bucket."""
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    if not rows:
        return {}
    t_last = float(rows[-1]["Time Stamp"])
    win = rows if tail_s is None else [r for r in rows if (t_last - float(r["Time Stamp"])) <= tail_s * 1000]

    def vals(col):
        out = []
        for r in win:
            v = r.get(col)
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                pass
        return out
    out = {"ovr_rows": len(win), "ovr_window_s": round((t_last - float(win[0]["Time Stamp"])) / 1000, 1)}
    for col, key, how in (("average_frame_rate", "ovr_avg_fps", "mean"),
                          ("average_frame_rate", "ovr_fps_min", "min"),
                          ("battery_temperature_celcius", "ovr_battery_temp_c", "mean"),
                          ("gpu_utilization_percentage", "ovr_gpu_util_pct", "mean"),
                          ("cpu_utilization_percentage", "ovr_cpu_util_pct", "mean"),
                          ("stale_frame_count", "ovr_stale_seconds", "count_nonzero"),
                          ("stale_frame_count", "ovr_stale_frames_window", "sum"),
                          ("stale_frames_consecutive", "ovr_max_consecutive_stale", "max"),
                          ("max_repeated_frames", "ovr_max_repeated_frames", "max"),
                          ("skipped_frames", "ovr_skipped_frames", "max"),
                          ("app_frame_throttle", "ovr_throttle_seconds", "sum")):
        v = vals(col)
        if not v:
            continue
        out[key] = {"mean": round(sum(v) / len(v), 2), "min": min(v), "max": max(v),
                    "sum": round(sum(v), 2), "count_nonzero": sum(1 for x in v if x)}[how]
    secs_below = sum(1 for x in vals("average_frame_rate") if x < 85)
    out["ovr_seconds_below_85fps"] = secs_below
    return out


def codec_events(run_dir, ser, since_dev_s=None, out_tsv=None):
    """Video-codec lifecycle from `dumpsys batterystats --history` (ms resolution, retained days):
    '+video'/'-video' mark decoder attach/detach per uid. A restart during a session is the fingerprint
    of the VD encoder/decoder decay, so results carry the count and the events themselves."""
    txt = _adb("-s", ser, "shell", "dumpsys batterystats --history")
    base = re.search(r"RESET:TIME:\s*(\d{4})-(\d\d)-(\d\d)-(\d\d)-(\d\d)-(\d\d)", txt)
    if not base:
        return {}
    import datetime as _dt
    b = _dt.datetime(*[int(x) for x in base.groups()])
    ev = []
    for line in txt.splitlines():
        m = re.search(r"\+(\d+)d(\d+)h(\d+)m([\d.]+)s(\d+)ms\s*\(\d+\)\s+(\d+)\s+([+-])video", line)
        if not m:
            continue
        t = b + _dt.timedelta(days=int(m.group(1)), hours=int(m.group(2)), minutes=int(m.group(3)),
                              seconds=float(m.group(4)), milliseconds=int(m.group(5)))
        ev.append((t.timestamp(), m.group(6), m.group(7)))
    if out_tsv:
        with open(out_tsv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["dev_epoch_s", "wall", "uid", "event"])
            for t, uid, sign in ev:
                w.writerow([round(t, 3), _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                            uid, sign + "video"])
    out = {"codec_events_total": len(ev)}
    if ev and since_dev_s:
        win = [e for e in ev if e[0] >= float(since_dev_s)]
        out["codec_events_session"] = len(win)
        out["codec_starts_session"] = sum(1 for e in win if e[2] == "+")
        out["codec_restarts_session"] = max(0, out["codec_starts_session"] - 1)
        if win:
            out["codec_first_session_dev_s"] = round(win[0][0], 3)
            out["codec_last_session_dev_s"] = round(win[-1][0], 3)
    return out


def p2p_stats(path):
    """p2p0 carries the controller link (Quest Pro controllers + map share). Rate and error counters are
    the direct health signal for the controller path."""
    d = tab_deltas(path, ["p2p0_rx_bytes", "p2p0_tx_bytes", "p2p0_rx_errs", "p2p0_rx_drop",
                          "p2p0_tx_errs", "p2p0_tx_drop"])
    if not d:
        return {}
    out = {k: d.get(k) for k in ("p2p0_rx_errs", "p2p0_rx_drop", "p2p0_tx_errs", "p2p0_tx_drop")}
    el = d.get("elapsed_s")
    if el and el > 0:
        if d.get("p2p0_rx_bytes") is not None:
            out["p2p0_rx_mbps"] = round(d["p2p0_rx_bytes"] * 8 / el / 1e6, 3)
        if d.get("p2p0_tx_bytes") is not None:
            out["p2p0_tx_mbps"] = round(d["p2p0_tx_bytes"] * 8 / el / 1e6, 3)
    return {k: v for k, v in out.items() if v is not None}


def cm_reduce(path, session_start_dev_s=None, offset_s=0.0, out_tsv=None):
    """Controller/P2P events from appended `dumpsys cm_wifi` snapshots: controller link state transitions
    (CONNECTED_ACTIVE / CONNECTED_INACTIVE / CONNECTING / SEARCHING / DISABLED), P2P channel switches, RSDB
    concurrency changes and LOW_LATENCY toggles. Counts are restricted to the session window if known."""
    txt = _read_text(path)
    if not txt:
        return {}
    parts = re.split(r"(?m)^== (.+?) ==\s*$", txt)
    pairs = list(zip(parts[1::2], parts[2::2]))
    if not pairs:
        return {}
    import datetime as _dt
    ts_last, body = pairs[-1]
    try:
        snap_dev = _dt.datetime.strptime(ts_last.strip(), "%Y-%m-%d %H:%M:%S.%f").timestamp() + offset_s
    except ValueError:
        snap_dev = None

    def in_window(ago_s):
        if session_start_dev_s is None or snap_dev is None:
            return True
        return (snap_dev - float(ago_s)) >= float(session_start_dev_s)

    out = {"cm_snapshots": len(pairs)}
    events = []
    counts = {}
    for m in re.finditer(r"(\d\d:\d\d:\d\d\.\d\d\d)\s+\((\d+(?:\.\d+)?)s ago\)\s+-\s+Status:\s+deviceType:\s*(\d+)\s*\((\w+)\)[^\n]*?status:\s+(\w+)", body):
        if not in_window(m.group(2)):
            continue
        kind = f"ctrl_{m.group(5).lower()}"
        counts[kind] = counts.get(kind, 0) + 1
        events.append({"dev_epoch_s": round(snap_dev - float(m.group(2)), 3) if snap_dev else None,
                       "kind": "controller_status", "hand": m.group(4), "detail": m.group(5)})
    # P2P / WLAN events use a different row shape: "<uptime>s (<ago>s ago, <ISO-UTC>) - EVENT"
    for m in re.finditer(r"([\d.]+)s\s+\((\d+(?:\.\d+)?)s ago,\s*([\d\-]+T[\d:.]+)Z?\)\s+-\s+([^\n]+)", body):
        if not in_window(m.group(2)):
            continue
        detail = m.group(4).strip()
        try:
            ev_dev = _dt.datetime.fromisoformat(m.group(3)).replace(tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            ev_dev = None
        head = detail.split()[0].upper().rstrip(":")
        key = ("low_latency_toggle" if head.startswith("LOW_LATENCY") else
               "concurrency_change" if head.startswith("ONCONCURRENCYMODECHANGED") else
               "p2p_" + head.lower().replace("p2p_", ""))
        counts[key] = counts.get(key, 0) + 1
        events.append({"dev_epoch_s": round(ev_dev, 3) if ev_dev else None,
                       "kind": "p2p", "hand": "", "detail": detail[:120]})
    # map-share: each controller pushes its ~693 KiB map over the P2P link on a fixed cadence; a gap or a
    # missed send is a sensitive indicator of controller-link trouble (more sensitive than the state log).
    maps = []
    for m in re.finditer(r"\[\+\]\s+(\d\d:\d\d:\d\d\.\d\d\d)\s+\((\d+(?:\.\d+)?)s ago\)\s+-\s+Send Map \[(\d+)\]\s+with length (\d+) KiB", body):
        if not in_window(m.group(2)):
            continue
        maps.append({"ago_s": float(m.group(2)), "kib": int(m.group(4))})
    if maps:
        agos = sorted(x["ago_s"] for x in maps)
        gaps = [round(b - a, 1) for a, b in zip(agos, agos[1:])]
        out["cm_map_share_sends"] = len(maps)
        out["cm_map_share_kib"] = sum(x["kib"] for x in maps)
        out["cm_map_share_gap_max_s"] = max(gaps) if gaps else None
        out["cm_map_share_gap_median_s"] = round(sorted(gaps)[len(gaps) // 2], 1) if gaps else None
    # newest controller status per hand from the last snapshot (line order = newest first)
    for hand, key in (("LeftHand", "ctrl_last_left"), ("RightHand", "ctrl_last_right")):
        m = re.search(r"deviceType:\s*\d+\s*\(" + hand + r"\)[^\n]*?status:\s+(\w+)", body)
        if m:
            out[key] = m.group(1)
    out.update({f"cm_{k}": v for k, v in counts.items()})
    out["cm_events_session"] = len(events)
    if out_tsv and events:
        with open(out_tsv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["dev_epoch_s", "kind", "hand", "detail"])
            w.writeheader()
            w.writerows(sorted(events, key=lambda e: e["dev_epoch_s"] or 0))
    return out


def _rate_series(run_dir):
    """(epoch, delivered Mbps) from the 2 s wlan0 samples."""
    rows = [r for r in read_tsv(os.path.join(run_dir, "quest_net_samples.tsv")) if r.get("wlan0_rx_bytes")]
    out = []
    for a, b in zip(rows, rows[1:]):
        try:
            ta = time.mktime(time.strptime(a["timestamp"][:19], "%Y-%m-%d %H:%M:%S"))
            tb = time.mktime(time.strptime(b["timestamp"][:19], "%Y-%m-%d %H:%M:%S"))
            if tb <= ta:
                continue
            out.append((tb, (float(b["wlan0_rx_bytes"]) - float(a["wlan0_rx_bytes"])) * 8 / (tb - ta) / 1e6))
        except (TypeError, ValueError):
            continue
    return out


def pc_summary(path):
    """PC-side sampler summary: NVENC utilization (is the encoder producing?) and Windows TCP retransmit
    rate (is the transport stalling?)."""
    rows = read_tsv(path) if os.path.exists(path) else []
    if not rows:
        return {}

    def col(c):
        v = []
        for r in rows:
            try:
                v.append(float(r[c]))
            except (KeyError, TypeError, ValueError):
                pass
        return v
    out = {"pc_samples": len(rows)}
    enc = col("enc_util_pct")
    if enc:
        out.update({"pc_enc_util_mean": round(sum(enc) / len(enc), 2), "pc_enc_util_min": min(enc), "pc_enc_util_max": max(enc)})
    gpu = col("gpu_util_pct")
    if gpu:
        out["pc_gpu_util_mean"] = round(sum(gpu) / len(gpu), 2)
    retr = col("tcp_retrans_per_s")
    if retr:
        out.update({"pc_tcp_retrans_mean": round(sum(retr) / len(retr), 2), "pc_tcp_retrans_max_per_s": max(retr)})
    sent = col("tcp_sent_per_s")
    if sent:
        out["pc_tcp_sent_mean_per_s"] = round(sum(sent) / len(sent), 1)
    return out


def decay_events(run_dir, frac=0.4, min_s=20.0, gap_s=10.0, out_tsv=None):
    """The community's 'VD bitrate decay' = a sustained collapse of the delivered rate. Detect every
    episode and report the PC-side encoder/TCP state inside it (+ the minute before it) versus the whole
    session: encoder utilization falling means the encoder stopped producing; a retransmit spike means the
    TCP transport collapsed."""
    ser = _rate_series(run_dir)
    if len(ser) < 20:
        return {}
    import statistics as _stats
    med = _stats.median(r for _, r in ser)
    # ignore the stream's own ramp-up: start looking only after the rate first reached half the median
    start_idx = 0
    for i, (_, r) in enumerate(ser):
        if r >= med * 0.5:
            start_idx = i
            break
    groups, cur = [], []
    for t, r in ser[start_idx:]:
        if r < frac * med:
            if cur and (t - cur[-1][0]) > gap_s:
                groups.append(cur)
                cur = []
            cur.append((t, r))
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    # merge episodes separated by a short recovery blip (menu navigation, a desktop repaint)
    merged = []
    for g in groups:
        if merged and (g[0][0] - merged[-1][-1][0]) <= 30:
            merged[-1].extend(g)
        else:
            merged.append(g)
    eps = [g for g in merged if (g[-1][0] - g[0][0]) >= min_s]
    out = {"decay_rate_median_mbps": round(med, 1), "decay_episodes": len(eps)}
    if eps:
        out["decay_first_start_min"] = round((eps[0][0][0] - ser[0][0]) / 60, 1)
        out["decay_total_min"] = round(sum(g[-1][0] - g[0][0] for g in eps) / 60, 1)
        out["decay_min_mbps"] = round(min(r for g in eps for _, r in g), 1)
    pc = read_tsv(os.path.join(run_dir, "pc_samples.tsv"))

    def pc_stats(t0, t1):
        enc, retr, sent, gpu = [], [], [], []
        for r in pc:
            try:
                t = time.mktime(time.strptime(r["timestamp"][:19], "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                continue
            if not (t0 <= t <= t1):
                continue
            for key, bag in (("enc_util_pct", enc), ("tcp_retrans_per_s", retr),
                             ("tcp_sent_per_s", sent), ("gpu_util_pct", gpu)):
                try:
                    bag.append(float(r[key]))
                except (KeyError, TypeError, ValueError):
                    pass
        mean = lambda v: round(sum(v) / len(v), 2) if v else None
        return {"enc_util_mean": mean(enc), "enc_util_min": min(enc) if enc else None,
                "gpu_util_mean": mean(gpu),
                "tcp_retrans_mean": mean(retr), "tcp_retrans_max": max(retr) if retr else None,
                "tcp_sent_mean": mean(sent)}

    rows_out = []
    if eps and pc:
        base = pc_stats(ser[0][0], ser[-1][0])
        out["decay_baseline_enc_util_mean"] = base["enc_util_mean"]
        out["decay_baseline_retrans_mean"] = base["tcp_retrans_mean"]
        for g in eps[:10]:
            s = pc_stats(g[0][0] - 60, g[-1][0])
            rows_out.append({"start": time.strftime("%H:%M:%S", time.localtime(g[0][0])),
                             "end": time.strftime("%H:%M:%S", time.localtime(g[-1][0])),
                             "dur_s": round(g[-1][0] - g[0][0], 1),
                             "min_mbps": round(min(r for _, r in g), 1), **s})
        if rows_out:
            out["decay_episode_enc_util_mean"] = rows_out[0]["enc_util_mean"]
            out["decay_episode_retrans_max"] = max(r["tcp_retrans_max"] or 0 for r in rows_out)
    if out_tsv and rows_out:
        with open(out_tsv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
            w.writeheader()
            w.writerows(rows_out)
    return out


def ping_stats(path):
    if not os.path.exists(path):
        return {}
    raw = _read_text(path)
    rtts = [float(m) for m in re.findall(r"time[=<]\s*(\d+)\s*ms", raw)]
    sent = re.search(r"Sent\s*=\s*(\d+)", raw)
    lost = re.search(r"Lost\s*=\s*(\d+)", raw)
    out = {}
    if rtts:
        s = sorted(rtts)

        def pct(p):
            return s[min(len(s) - 1, int(len(s) * p))]
        out = {"ping_n": len(s), "ping_rtt_p50_ms": pct(0.50), "ping_rtt_p90_ms": pct(0.90),
               "ping_rtt_p95_ms": pct(0.95), "ping_rtt_p99_ms": pct(0.99), "ping_rtt_max_ms": s[-1],
               "ping_rtt_min_ms": s[0]}
    if sent and lost:
        n, l = int(sent.group(1)), int(lost.group(1))
        out["ping_sent"] = n
        out["ping_loss_pct"] = round(l / n * 100, 3) if n else None
    return out


VR_KEYS = ["FPS", "Prd", "Tear", "Early", "Stale", "VSnc", "Lat", "TW", "App", "GD", "CPU&GPU",
           "GPU%", "CPU%", "CFL", "ICFLp95", "LD", "SF", "Temp", "PoseAgeP95", "Preempt"]


def _row_dev_epoch(stamp, ref_epoch):
    """'09-16 00:57:19.048' -> epoch seconds, using the year nearest to ref_epoch (logcat has no year)."""
    import datetime as _dt
    mm, dd = int(stamp[0:2]), int(stamp[3:5])
    hh, mi, ss = int(stamp[6:8]), int(stamp[9:11]), float(stamp[12:])
    ref_y = _dt.datetime.fromtimestamp(ref_epoch).year
    best = None
    for y in (ref_y - 1, ref_y, ref_y + 1):
        try:
            t = _dt.datetime(y, mm, dd, hh, mi, int(ss), int((ss % 1) * 1e6)).timestamp()
        except ValueError:
            continue
        if best is None or abs(t - ref_epoch) < abs(best - ref_epoch):
            best = t
    return best


def vr_api_reduce(path, out_csv=None, since_dev_s=None, ref_epoch=None):
    """logcat -s VrApi emits one line per second from the pid owning the VR session:
       FPS=90/90,...,Stale=0,Stale2/5/10/max=0/0/0/0,...,TW=1.77ms,App=1.46ms,...,CFL=12.54/16.83,
       ICFLp95=15.81,...,Temp=38.0C/0.0C,...,GPU%=0.31,PoseAgeP95=0.00
       plus a parallel ASW= line. This is the only frame telemetry that also covers Air Link.
       since_dev_s trims the pre-cell logcat backlog (the main buffer holds ~5 min)."""
    if not os.path.exists(path):
        return {}
    ts_re = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d\d\d)\s+\w/VrApi\s*\(\s*(\d+)\)")
    rows, asw, skipped = [], 0, 0
    t0 = None
    for line in _read_text(path).splitlines():
        if " ASW=" in line and "/VrApi" in line:
            asw += 1
            continue
        m = ts_re.match(line)
        if not m or " FPS=" not in line:
            continue
        if since_dev_s and ref_epoch:
            t = _row_dev_epoch(m.group(1), ref_epoch)
            if t is not None and t < since_dev_s:
                skipped += 1
                continue
        body = line.split("):", 1)[-1].strip()
        kv = {}
        for k in VR_KEYS:
            mm = re.search(r"(?:^|,)\s*" + re.escape(k) + r"=([^,]*)", body)
            if mm:
                kv[k] = mm.group(1)
        st = re.search(r"Stale2/5/10/max=([\d/]+)", body)
        hh, mm_, ss = map(float, m.group(1).split()[1].split(":"))
        tsec = hh * 3600 + mm_ * 60 + ss
        rows.append({
            "pid": int(m.group(2)),
            "t_s": round(tsec, 3),
            "clock": m.group(1),
            "fps": float(kv.get("FPS", "0/0").split("/")[0] or 0),
            "stale": float(kv.get("Stale", 0) or 0),
            "stale_max": float(st.group(1).split("/")[3]) if st else 0.0,
            "tw_ms": float(kv.get("TW", "0ms").replace("ms", "") or 0),
            "app_ms": float(kv.get("App", "0ms").replace("ms", "") or 0),
            "cfl_ms": float(kv.get("CFL", "0/0").split("/")[0] or 0),
            "icfl_p95_ms": float(kv.get("ICFLp95", 0) or 0),
            "temp_c": float(kv.get("Temp", "0C/0C").split("C")[0] or 0),
            "gpu_pct": float(kv.get("GPU%", 0) or 0),
            "pose_age_p95": float(kv.get("PoseAgeP95", 0) or 0),
        })
    if not rows:
        return {"vr_api_lines": 0}
    # Two processes can emit VrApi lines (the VR session owner and vrshell): keep the dominant emitter so
    # the series is not a mix of two frame pipelines.
    from collections import Counter
    pid = Counter(r["pid"] for r in rows).most_common(1)[0][0]
    other = len(rows) - sum(1 for r in rows if r["pid"] == pid)
    rows = [r for r in rows if r["pid"] == pid]
    t0 = rows[0]["t_s"]
    for r in rows:
        r["t_s"] = round(r["t_s"] - t0, 3)
    if out_csv:
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    fps = [r["fps"] for r in rows]
    return {
        "vr_api_lines": len(rows), "vr_api_asw_lines": asw, "vr_api_lines_skipped": skipped,
        "vr_api_pid": pid, "vr_api_lines_ignored_other_pid": other,
        "vr_api_fps_mean": round(sum(fps) / len(fps), 2), "vr_api_fps_min": min(fps),
        "vr_api_seconds_below_85fps": sum(1 for x in fps if x < 85),
        "vr_api_stale_total": int(sum(r["stale"] for r in rows)),
        "vr_api_stale_seconds": sum(1 for r in rows if r["stale"] > 0),
        "vr_api_stale_max_consecutive": max(r["stale_max"] for r in rows),
        "vr_api_tw_ms_mean": round(sum(r["tw_ms"] for r in rows) / len(rows), 3),
        "vr_api_app_ms_mean": round(sum(r["app_ms"] for r in rows) / len(rows), 3),
        "vr_api_cfl_ms_mean": round(sum(r["cfl_ms"] for r in rows) / len(rows), 3),
        "vr_api_icfl_p95_ms_mean": round(sum(r["icfl_p95_ms"] for r in rows) / len(rows), 3),
        "vr_api_icfl_p95_ms_max": max(r["icfl_p95_ms"] for r in rows),
        "vr_api_pose_age_p95_max": max(r["pose_age_p95"] for r in rows),
        "vr_api_temp_c_max": max(r["temp_c"] for r in rows),
        "vr_api_gpu_pct_mean": round(sum(r["gpu_pct"] for r in rows) / len(rows), 2),
    }


def tab_deltas(path, cols):
    """first/last delta of the named numeric columns of a sampler TSV."""
    if not os.path.exists(path):
        return {}
    rows = read_tsv(path)
    if len(rows) < 2:
        return {"n": len(rows)}

    def num(r, c):
        try:
            return float(r[c])
        except (KeyError, TypeError, ValueError):
            return None
    out = {"n": len(rows)}
    try:
        t0 = time.mktime(time.strptime(rows[0]["timestamp"][:19], "%Y-%m-%d %H:%M:%S"))
        t1 = time.mktime(time.strptime(rows[-1]["timestamp"][:19], "%Y-%m-%d %H:%M:%S"))
        out["elapsed_s"] = round(t1 - t0, 1)
    except Exception:
        out["elapsed_s"] = None
    for c in cols:
        a, b = num(rows[0], c), num(rows[-1], c)
        out[c] = None if (a is None or b is None) else b - a
    return out


def sf_stats(path):
    if not os.path.exists(path):
        return {}
    rows = read_tsv(path)
    if len(rows) < 3:
        return {"sf_samples": len(rows)}
    frames = [float(r["frames_since_prev"]) for r in rows if r.get("frames_since_prev")]
    out = {"sf_samples": len(rows), "sf_frames_total": round(sum(frames), 1)}
    if frames:
        out["sf_frames_per_sample_mean"] = round(sum(frames) / len(frames), 2)
        out["sf_frames_per_sample_min"] = min(frames)
    return out


def summarize_env(path):
    if not os.path.exists(path):
        return {}
    rows = read_tsv(path)
    if not rows:
        return {}

    def nums(c):
        v = []
        for r in rows:
            try:
                v.append(float(r[c]))
            except (KeyError, TypeError, ValueError):
                pass
        return v
    out = {"env_samples": len(rows)}
    for c, label in [("soc_usr_c", "env_soc_max_c"), ("gpuss_max_c", "env_gpu_max_c"),
                     ("cpuss_max_c", "env_cpu_max_c"), ("batt_virt_c", "env_batt_virt_max_c"),
                     ("gpu_busy_pct", "env_gpu_busy_mean_pct"), ("sta_tx_power_dbm", "env_sta_tx_power_dbm")]:
        v = nums(c)
        if v:
            out[label] = round(max(v) if "busy" not in label and "power" not in label else sum(v) / len(v), 2)
    rssi = nums("sta_rssi")
    if rssi:
        out["env_sta_rssi_min"] = min(rssi)
        out["env_sta_rssi_max"] = max(rssi)
    return out


def csv_upsert(path, id_cols, meas_cols, row):
    """Append a run's row, replacing any earlier row with the same id (re-running `results` must update,
    not duplicate)."""
    cols = id_cols + meas_cols
    key = id_cols[0] if id_cols else None
    existing, hdr = [], None
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            hdr = rd.fieldnames
            existing = list(rd)
    want = list(hdr) if hdr else []
    for c in cols:
        if c not in want:
            want.append(c)
    if key:
        existing = [r for r in existing if r.get(key) != row.get(key)]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=want)
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k, "") for k in want})
        w.writerow({k: row.get(k, "") for k in want})


# ---------------------------------------------------------------- results
def watch(run_id, interval=30, drop_frac=0.6, heartbeat_min=5):
    """Unattended watcher for a long soak: prints only when state changes (delivered rate collapsing, TCP
    retransmits appearing) plus a periodic heartbeat, so an episode gets timestamped even when nobody is
    watching the monitor's status lines."""
    import statistics as _stats
    run_dir = os.path.join(BASE, "runs", run_id)
    print(f"watch started: {run_id} interval={interval}s drop_frac={drop_frac}", flush=True)
    last_hb, low_streak, last_retrans = 0.0, 0, None
    while True:
        ser = _rate_series(run_dir)
        pc = read_tsv(os.path.join(run_dir, "pc_samples.tsv"))
        now = time.strftime("%H:%M:%S")
        if len(ser) >= 10:
            med = _stats.median(r for _, r in ser)
            recent = [r for _, r in ser[-4:]]
            cur = sum(recent) / len(recent)
            enc = pc[-1].get("enc_util_pct") if pc else "?"
            ws = pc[-1].get("streamer_ws_mb") if pc else "?"
            try:
                retr = float(pc[-1].get("tcp_retrans_per_s") or 0)
            except (IndexError, TypeError, ValueError):
                retr = 0.0
            low_streak = low_streak + 1 if cur < drop_frac * med else 0
            if low_streak == 2:
                print(f"[{now}] DECAY SUSPECT: rate {cur:.0f} Mbps vs median {med:.0f} ({cur / med * 100:.0f}%)"
                      f" | encoder {enc}% | retrans {retr:.0f}/s | ws {ws} MB", flush=True)
            if last_retrans is not None and retr > max(50.0, last_retrans * 10):
                print(f"[{now}] TCP RETRANSMIT SPIKE: {retr:.0f}/s (prev {last_retrans:.0f}) | rate {cur:.0f} Mbps"
                      f" | encoder {enc}%", flush=True)
            last_retrans = retr
            if time.time() - last_hb >= heartbeat_min * 60:
                last_hb = time.time()
                print(f"[{now}] hb rate {cur:.0f} Mbps (median {med:.0f}) encoder {enc}% gpu "
                      f"{pc[-1].get('gpu_util_pct') if pc else '?'}% retrans {retr:.0f}/s ws {ws} MB buckets {len(ser)}", flush=True)
        time.sleep(interval)


def results(run_id, overlay_path=None):
    run_dir = os.path.join(BASE, "runs", run_id)
    ser = adb_serial()
    overlay = {}
    if overlay_path and os.path.exists(overlay_path):
        overlay = json.load(open(overlay_path))

    w = json.loads(_run([sys.executable, os.path.join(qsite.TOOLS_DIR, "analyze.py"),
                         "cell", os.path.join(BASE, "runs"), run_id,
                         "--quest-ip", QUEST_IP, "--window", "30,150"]))

    # wifi counter deltas (MAC layer, headset TX direction)
    rows = read_tsv(os.path.join(run_dir, "quest_wifi_samples.tsv"))
    if len(rows) < 2:
        raise RuntimeError("no wifi samples yet in " + run_dir)
    f, l = rows[0], rows[-1]

    def d(k):
        return int(l[k]) - int(f[k])
    tx = d("tx_success"); tr = d("tx_retries"); tl = d("tx_lost"); rx = d("rx_success")
    retry = round(tr / tx * 100, 3) if tx else None
    lost = round(tl / tx * 100, 3) if tx else None

    # ovr metrics (last 170 s of newest CSV) - VD only; stale_frame_count is a PER-SECOND bucket.
    # The OVR service writes one CSV per session, so a cell with OVR logging disabled would otherwise
    # silently inherit the previous session's numbers: gate on the file's device mtime vs the cell window.
    clock_file = os.path.join(run_dir, "clock.json")
    clock = json.load(open(clock_file)) if os.path.exists(clock_file) else {}
    start_dev = clock.get("cell_start_dev_s")
    sess_file = os.path.join(run_dir, "session.json")
    sess = json.load(open(sess_file)) if os.path.exists(sess_file) else {}
    vr_start = sess.get("start_dev_s") or start_dev
    newest = _adb("-s", ser, "shell", f"ls -t {OVR_DIR} | head -1").strip()
    ovr_csv = os.path.join(run_dir, "ovr_metrics.csv")
    if newest:
        _run([ADB, "-s", ser, "pull", f"{OVR_DIR}/{newest}", ovr_csv])
    ovr = {"ovr_source_file": newest or None}
    fresh = True
    if newest and start_dev:
        mt = _adb("-s", ser, "shell", f"stat -c '%Y' '{OVR_DIR}/{newest}'").strip()
        try:
            mt = float(mt)
            ovr["ovr_file_mtime_dev_s"] = mt
            fresh = mt >= float(start_dev) - 5
            ovr["ovr_stale"] = not fresh
        except ValueError:
            ovr["ovr_stale"] = None
    if newest and fresh and os.path.exists(ovr_csv):
        ovr.update(ovr_window(ovr_csv, tail_s=170))

    res = {
        "wire_tx_mbps": w.get("wire_tx_mbps"), "wire_rx_mbps": w.get("wire_rx_mbps"),
        "wire_pkts_per_s": w.get("wire_pkts_per_s"), "transport": "tcp",
        "quest_tx_packets_delta": tx, "quest_retried_tx_delta": tr,
        "quest_lost_tx_delta": tl, "quest_rx_packets_delta": rx,
        "retry_rate_pct": retry, "lost_rate_pct": lost,
        "overlay_fps": overlay.get("fps"), "overlay_latency_total_ms": overlay.get("lat_total"),
        "overlay_latency_game_ms": overlay.get("lat_game"), "overlay_latency_encode_ms": overlay.get("lat_encode"),
        "overlay_latency_network_ms": overlay.get("lat_network"), "overlay_latency_decode_ms": overlay.get("lat_decode"),
        "overlay_bitrate_mbps": overlay.get("bitrate_mbps"), "overlay_wifi_mbps": overlay.get("wifi_mbps"),
    }
    res.update(ovr)
    res.update(ping_stats(os.path.join(run_dir, "ping_samples.txt")))
    res.update(vr_api_reduce(os.path.join(run_dir, "vr_api_logcat.txt"),
                             os.path.join(run_dir, "vr_api_samples.csv"),
                             since_dev_s=vr_start, ref_epoch=vr_start))
    res.update({k: v for k, v in tab_deltas(
        os.path.join(run_dir, "quest_net_samples.tsv"),
        ["tcp_retrans_segs", "tcp_in_errs", "wlan0_rx_errs", "wlan0_rx_drop",
         "wlan0_tx_errs", "wlan0_tx_drop", "tcp_in_segs", "tcp_out_segs"]).items()})
    res.update(summarize_env(os.path.join(run_dir, "quest_env_samples.tsv")))
    res.update(sf_stats(os.path.join(run_dir, "sf_latency_samples.tsv")))
    res.update(p2p_stats(os.path.join(run_dir, "quest_net_samples.tsv")))
    res.update(pc_summary(os.path.join(run_dir, "pc_samples.tsv")))
    res.update(decay_events(run_dir, out_tsv=os.path.join(run_dir, "decay_episodes.tsv")))
    res.update(cm_reduce(os.path.join(run_dir, "cm_wifi_snapshots.txt"),
                         session_start_dev_s=start_dev,
                         offset_s=(clock.get("offset_s") or 0.0),
                         out_tsv=os.path.join(run_dir, "controller_events.tsv")))
    res.update(codec_events(run_dir, ser, since_dev_s=vr_start,
                            out_tsv=os.path.join(run_dir, "codec_events.tsv")))
    if clock:
        res["quest_clock_offset_s"] = clock.get("offset_s")
        res["quest_uptime_s"] = clock.get("dev_uptime_s")
        res["quest_cell_start_dev_s"] = start_dev
        res["quest_cell_end_dev_s"] = clock.get("cell_end_dev_s")
    if sess:
        res["quest_session_start_dev_s"] = sess.get("start_dev_s")
        res["quest_session_min"] = (round((sess["end_dev_s"] - sess["start_dev_s"]) / 60, 1) if sess.get("end_dev_s")
                                    else round(res.get("vr_api_lines", 0) / 60, 1) or None)

    json.dump(res, open(os.path.join(run_dir, "results.json"), "w"), indent=1)

    s = json.load(open(os.path.join(run_dir, "settings.json")))
    id_cols, meas_cols = ID_COLS, MEAS_COLS
    csv_upsert(os.path.join(BASE, "results.csv"), id_cols, meas_cols,
               {**{c: s.get(c) for c in id_cols}, **res})
    print("wrote results.json + results.csv")
    print("wire_tx_mbps:", res["wire_tx_mbps"], "retry:", res["retry_rate_pct"],
          "ping_p95:", res.get("ping_rtt_p95_ms"), "vr_api_fps:", res.get("vr_api_fps_mean"),
          "stale:", res.get("vr_api_stale_total"))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "serial":
        print(adb_serial())
    elif sys.argv[1] == "layer":
        ser = adb_serial()
        print(find_sf_layer(ser) or "")
    elif sys.argv[1] == "monitor":
        monitor(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 10800)
    elif sys.argv[1] == "watch":
        watch(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 30)
    elif sys.argv[1] == "passive":
        passive(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "start")
    elif sys.argv[1] == "capture":
        capture(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 150)
    elif sys.argv[1] == "results":
        results(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
