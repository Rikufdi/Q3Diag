#!/usr/bin/env python3
"""cell.py - one-cell capture + results orchestration.

Usage:
  python cell.py serial                          # print the resolved wireless-adb endpoint
  python cell.py capture <run_id> [duration_s]   # pktmon + device sampler + ping + VrApi logcat
  python cell.py monitor <run_id> [max_seconds]  # open-ended live-session sampling (default 10800s / 3h)
  python cell.py stop <run_id>                   # ask a running `monitor <run_id>` to shut down cleanly
                                                  # (same effect as Ctrl+C; use when it isn't attached to
                                                  # your own console -- see monitor()'s docstring)
  python cell.py results <run_id> [overlay.json] # analyze + deltas + ovr + write results
  python cell.py fingerprint <run_id> [--save-baseline] [--tag=NAME]
                                                  # diff results.json against a saved per-config baseline
  python cell.py linktest [--no-beep] [--headset-beep]
                                                  # live RSSI/retry watch for AP/headset placement testing,
                                                  # no monitor/stream needed; PC beeps by default (confirmed
                                                  # working). --headset-beep is a silent notification-history
                                                  # marker only, NOT an alert -- see _alert_headset
  python cell.py watch <run_id> [interval] [--beep] [--headset-beep]
                                                  # add during `monitor` for the same alerting

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
import analyze

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
    "ovr_throttle_pct_mean", "ovr_battery_temp_c", "ovr_gpu_util_pct", "ovr_cpu_util_pct",
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
    "quest_session_start_dev_s", "quest_session_min", "quest_session_segments",
    "pc_game_process", "pc_game_frame_count", "pc_game_fps_mean", "pc_game_fps_min", "pc_game_fps_1pct_low",
]

# ---------------------------------------------------------------- fingerprinting
# A "fingerprint" is a small, curated subset of results.json meant to answer "did anything change since
# last time", not to replace the full results row. Each metric has a drift rule: (direction, ratio_thresh,
# abs_thresh). A metric trips if it moves past EITHER threshold -- ratio catches proportionally large
# moves even on a small baseline value, abs catches small-ratio moves that still matter in absolute terms
# (e.g. +4 degC). ratio_thresh=999 disables the ratio check for counters that scale with session length.
FINGERPRINT_KEYS = [
    "retry_rate_pct", "lost_rate_pct", "ping_rtt_p50_ms", "ping_rtt_p95_ms", "ping_loss_pct",
    "vr_api_fps_mean", "vr_api_seconds_below_85fps", "vr_api_stale_per_min",
    "pc_enc_util_mean", "pc_tcp_retrans_mean", "decay_episodes", "decay_total_min",
    "decay_rate_median_mbps", "env_soc_max_c", "env_gpu_max_c", "env_sta_tx_power_dbm",
    "wlan0_rx_errs", "wlan0_rx_drop", "tcp_retrans_segs", "pc_game_fps_mean", "pc_game_fps_1pct_low",
]
FINGERPRINT_RULES = {
    "retry_rate_pct":             ("worse_high", 1.5,   0.5),
    "lost_rate_pct":               ("worse_high", 2.0,   0.05),
    "ping_rtt_p50_ms":             ("worse_high", 1.5,   3.0),
    "ping_rtt_p95_ms":             ("worse_high", 1.5,   8.0),
    "ping_loss_pct":                ("worse_high", 2.0,   0.5),
    "vr_api_fps_mean":              ("worse_low",  0.9,   2.0),
    "vr_api_seconds_below_85fps":  ("worse_high", 2.0,   10.0),
    "vr_api_stale_per_min":        ("worse_high", 2.0,   3.0),
    "pc_enc_util_mean":             ("worse_low",  0.7,   3.0),
    "pc_tcp_retrans_mean":          ("worse_high", 3.0,   2.0),
    "decay_episodes":               ("worse_high", 999.0, 0.5),
    "decay_total_min":              ("worse_high", 1.5,   1.0),
    "decay_rate_median_mbps":       ("worse_low",  0.85,  20.0),
    "env_soc_max_c":                ("worse_high", 1.05,  4.0),
    "env_gpu_max_c":                ("worse_high", 1.05,  4.0),
    "env_sta_tx_power_dbm":         ("worse_high", 1.3,   3.0),
    "wlan0_rx_errs":                ("worse_high", 999.0, 5.0),
    "wlan0_rx_drop":                ("worse_high", 999.0, 5.0),
    "tcp_retrans_segs":             ("worse_high", 999.0, 20.0),
    "pc_game_fps_mean":             ("worse_low",  0.9,   3.0),
    "pc_game_fps_1pct_low":         ("worse_low",  0.8,   5.0),
}


def _fp_derive(res):
    out = {k: res.get(k) for k in FINGERPRINT_KEYS if res.get(k) is not None}
    lines, stale = res.get("vr_api_lines"), res.get("vr_api_stale_total")
    if lines and stale is not None:
        out["vr_api_stale_per_min"] = round(stale / (lines / 60), 2)
    return out


def fingerprint(run_id, save_baseline=False, tag=None):
    """Compare a run's results.json against a saved per-configuration baseline (same stack/codec/bitrate/
    band by default) and report which metrics drifted beyond their threshold. With no prior baseline for
    the tag, or with save_baseline=True, this run becomes the new baseline instead of being diffed.
    Point of this: run it after every session so a regression (AP moved, driver update, cable routing)
    shows up as 'these N metrics moved' instead of a vague 'something feels off'."""
    run_dir = os.path.join(BASE, "runs", run_id)
    res_path = os.path.join(run_dir, "results.json")
    if not os.path.exists(res_path):
        raise RuntimeError(f"no results.json in {run_dir} -- run `cell.py results {run_id}` first")
    res = json.load(open(res_path))
    settings = json.load(open(os.path.join(run_dir, "settings.json"))) if \
        os.path.exists(os.path.join(run_dir, "settings.json")) else {}
    tag = tag or f"{settings.get('stack', 'na')}_{settings.get('codec', 'na')}_{settings.get('bitrate_mbps', 'na')}_{settings.get('band', 'na')}"
    tag = re.sub(r"[^A-Za-z0-9_.+-]", "_", str(tag))
    metrics = _fp_derive(res)

    fp_dir = os.path.join(BASE, "baseline", "fingerprints")
    os.makedirs(fp_dir, exist_ok=True)
    baseline_path = os.path.join(fp_dir, f"{tag}.json")
    diff_path = os.path.join(run_dir, "fingerprint_diff.json")
    fp = {"tag": tag, "run_id": run_id, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
          "settings": {k: settings.get(k) for k in ("stack", "codec", "bitrate_mbps", "content", "band")},
          "metrics": metrics}

    if save_baseline or not os.path.exists(baseline_path):
        json.dump(fp, open(baseline_path, "w"), indent=1)
        why = "forced" if save_baseline else "no prior baseline for this configuration"
        print(f"fingerprint: saved '{tag}' from {run_id} as the baseline ({why})")
        json.dump({"tag": tag, "baseline": True, "flags": []}, open(diff_path, "w"), indent=1)
        return {"baseline": True, "tag": tag, "flags": []}

    base = json.load(open(baseline_path))
    base_metrics = base.get("metrics", {})
    flags, rows = [], []
    for key, val in metrics.items():
        rule = FINGERPRINT_RULES.get(key)
        bval = base_metrics.get(key)
        if rule is None or bval is None:
            continue
        direction, ratio_thresh, abs_thresh = rule
        delta = val - bval
        if direction == "worse_high":
            trip = (delta >= abs_thresh) or (bval > 0 and val / bval >= ratio_thresh)
        else:
            trip = ((-delta) >= abs_thresh) or (bval > 0 and val / bval <= ratio_thresh)
        row = {"metric": key, "baseline": bval, "current": val, "delta": round(delta, 3)}
        rows.append(row)
        if trip:
            flags.append(row)

    print(f"fingerprint: {run_id} vs baseline '{tag}' (from {base.get('run_id')} @ {base.get('saved_at')})")
    if flags:
        print(f"  {len(flags)} metric(s) drifted beyond threshold:")
        for r in flags:
            print(f"    - {r['metric']}: {r['baseline']} -> {r['current']} (delta {r['delta']:+})")
    else:
        print("  no metric exceeded its drift threshold -- consistent with the baseline")
    out = {"tag": tag, "baseline": False, "baseline_run_id": base.get("run_id"),
           "baseline_saved_at": base.get("saved_at"), "flags": flags, "all": rows}
    json.dump(out, open(diff_path, "w"), indent=1)
    print(f"  wrote {diff_path}")
    return out


# ---------------------------------------------------------------- audio alerting
def _tone_wav(tones, sample_rate=44100, volume=0.5):
    """Build an in-memory 16-bit mono PCM WAV of the given [(freq_hz, ms), ...] tones played back to
    back, each with a short fade in/out to avoid clicks. Used instead of winsound.Beep(), which drives
    Windows' legacy tone-generator API -- historically the physical PC speaker, and on modern systems
    only loosely routed through the audio mixer. That API goes silent on non-standard audio setups
    (virtual audio devices, multi-endpoint routing, ASIO/spatial-audio processing chains, etc.) because
    it doesn't share a path with normal application audio. A real WAV played via winsound.PlaySound uses
    the standard multimedia (waveOut) path instead -- the same one system/notification sounds use, so if
    the machine can hear anything at all, it should hear this."""
    import struct, math
    pcm = bytearray()
    for freq, ms in tones:
        n = max(1, int(sample_rate * ms / 1000))
        fade = max(1, int(sample_rate * 0.01))
        for i in range(n):
            env = min(1.0, i / fade, (n - i) / fade)
            val = int(32767 * volume * env * math.sin(2 * math.pi * freq * i / sample_rate))
            pcm += struct.pack("<h", val)
    data = bytes(pcm)
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    return b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + fmt + b"data" + struct.pack("<I", len(data)) + data


def _alert(degrade):
    """A short two-tone chime -- descending for 'link got worse', ascending for 'link recovered'. Meant to
    be run alongside `watch --beep` or `linktest` while physically moving the headset/AP/cables, so a
    degradation is audible without staring at the terminal. Plays via winsound.PlaySound(SND_MEMORY) (see
    _tone_wav for why, not winsound.Beep); falls back to MessageBeep, then the terminal bell, if that
    fails or winsound is unavailable (non-Windows)."""
    tones = [(520, 160), (360, 220)] if degrade else [(700, 110), (980, 140)]
    try:
        import winsound
        winsound.PlaySound(_tone_wav(tones), winsound.SND_MEMORY)
        return
    except Exception:
        pass
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION if degrade else winsound.MB_ICONASTERISK)
        return
    except Exception:
        pass
    print("\a", end="", flush=True)


def _alert_headset(ser, degrade):
    """Posts a notification on the headset via `cmd notification post` -- NOT an audible or visible
    alert, verified dead end as of 2026-09-16 (see findings.md "Dead end: cmd notification post..."):
    this Horizon OS build's shell notification tool has no flag for sound/vibration/priority at all
    (`-h` lists only -t/-i/-I/-S/-c), and the posted notification carries `sound=null vibrate=null` --
    confirmed with the headset worn and Do Not Disturb off: it lands in notification history only, no
    heads-up card, no sound. Kept only as a silent timestamped marker for retroactively correlating a
    degradation event with the headset's own notification log, NOT as a live alert -- do not present
    --headset-beep to a user as making noise or popping up a card, it does neither on this build.
    Still gated behind explicit opt-in and never fired without asking first: it is still an on-device
    mutation (creates a real, visible-in-history notification), same category QUEST-AGENT-PLAYBOOK.md
    gates behind asking, even though its effect turned out to be inert in the moment."""
    title = "Q3Diag link degraded" if degrade else "Q3Diag link recovered"
    body = "RSSI/retry threshold crossed" if degrade else "back to baseline"
    cmd = f"cmd notification post -S bigtext -t '{title}' q3diag-linktest '{body}'"
    _adb("-s", ser, "shell", cmd)


def _parse_wifi_status(text):
    out = {}
    for key, pat in (("tx_success", r"successfulTxPackets:\s*(\d+)"), ("tx_retries", r"retriedTxPackets:\s*(\d+)"),
                     ("tx_lost", r"lostTxPackets:\s*(\d+)"), ("rx_success", r"successfulRxPackets:\s*(\d+)"),
                     ("rssi", r"RSSI:\s*(-?\d+)"), ("tx_link_mbps", r"Tx Link speed:\s*(\d+)Mbps")):
        m = re.search(pat, text)
        out[key] = int(m.group(1)) if m else None
    return out


def linktest(interval=1.0, rssi_drop_db=8, retry_pct_thresh=5.0, beep=True, headset_beep=False, window=5,
             retry_window_s=10):
    """Standalone live link-quality watch -- no monitor/streaming session needed. Polls `cmd wifi status`
    at `interval`s, prints RSSI/link/retry/lost, and (if beep) sounds an alert when RSSI drops
    rssi_drop_db below its rolling baseline or the retry rate exceeds retry_pct_thresh. Built for
    physically testing AP placement, headset position, or cable routing in real time: move things around
    and listen for the boop instead of reading numbers.
    retry/lost % are deltas over the trailing retry_window_s seconds of polls, not just the previous
    poll -- a single-poll delta is too few packets for a stable ratio at interval=1s and was visibly
    jumpy (and could false-trigger the alarm on noise); 10s balances that against staying responsive to
    a real change caused by moving hardware around, same tradeoff as the dashboard's retry tile.
    headset_beep additionally posts a marker to the headset's notification history (see _alert_headset)
    -- NOT an audible/visible alert, a verified dead end on this Horizon OS build (silent, no heads-up
    card). Only useful for retroactively correlating a degradation event against the headset's own
    notification log timestamps. Off by default; still an on-device mutation."""
    ser = adb_serial()
    print(f"linktest: interval={interval}s rssi_drop_db={rssi_drop_db} retry_pct_thresh={retry_pct_thresh}% "
          f"(avg over {retry_window_s}s) beep={beep} headset_beep={headset_beep}")
    print("move the headset / AP / cables now -- Ctrl+C to stop")
    hist, baseline_rssi, alarm = [], None, False
    counters = []  # [(poll_time, wifi_status_dict), ...] trimmed to the trailing retry_window_s
    try:
        while True:
            t_now = time.time()
            c = _parse_wifi_status(_adb("-s", ser, "shell", "cmd wifi status"))
            rssi = c.get("rssi")
            if rssi is not None:
                hist.append(rssi)
                hist = hist[-window:]
                if baseline_rssi is None and len(hist) >= window:
                    baseline_rssi = sum(hist) / len(hist)
            counters.append((t_now, c))
            counters = [x for x in counters if t_now - x[0] <= retry_window_s]
            retry_pct = lost_pct = None
            if len(counters) >= 2:
                a, b = counters[0][1], counters[-1][1]
                dtx = (b.get("tx_success") or 0) - (a.get("tx_success") or 0)
                dtr = (b.get("tx_retries") or 0) - (a.get("tx_retries") or 0)
                dtl = (b.get("tx_lost") or 0) - (a.get("tx_lost") or 0)
                if dtx > 0:
                    retry_pct, lost_pct = round(dtr / dtx * 100, 2), round(dtl / dtx * 100, 3)
            bad, reasons = False, []
            if baseline_rssi is not None and rssi is not None and (baseline_rssi - rssi) >= rssi_drop_db:
                bad, _ = True, reasons.append(f"RSSI {rssi} vs baseline {baseline_rssi:.0f}")
            if retry_pct is not None and retry_pct >= retry_pct_thresh:
                bad, _ = True, reasons.append(f"retry {retry_pct}%")
            print(f"[{time.strftime('%H:%M:%S')}] rssi={rssi if rssi is not None else '-'} "
                  f"link={c.get('tx_link_mbps', '-')}Mbps "
                  f"retry={retry_pct if retry_pct is not None else '-'}% "
                  f"lost={lost_pct if lost_pct is not None else '-'}%"
                  + (f"  *** DEGRADED: {'; '.join(reasons)}" if bad else ""), flush=True)
            if bad and not alarm:
                alarm = True
                if beep:
                    _alert(degrade=True)
                if headset_beep:
                    _alert_headset(ser, degrade=True)
            elif not bad and alarm:
                alarm = False
                if beep:
                    _alert(degrade=False)
                if headset_beep:
                    _alert_headset(ser, degrade=False)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("linktest stopped")

_serial_cache = None


def write_session(path, stack, segments, off=0.0):
    """Write session.json from the full list of {proc, start_dev_s, [end_dev_s]} segments observed so
    far in this monitor run. Stays backward compatible with the old single-session shape at the top
    level (start_dev_s = first segment's start, end_dev_s = last segment's end if it has closed, proc =
    most recent) so `results()` and any old tooling reading session.json need no changes; `segments` and
    `active_min` (sum of each segment's duration, i.e. excluding any gap where the app wasn't running --
    such as while the headset was asleep) are additive fields for anything that wants the full picture.
    `off` is the headset<->PC clock offset (seconds, headset ahead is positive): start_dev_s/end_dev_s
    are always in device-epoch terms, so an open segment's still-running duration must be estimated with
    `time.time() + off`, not raw `time.time()` -- using the wrong clock domain understates (or, when off
    is large enough, makes negative) the duration of whichever segment is still open when this is called."""
    active_min = round(sum((s.get("end_dev_s", time.time() + off) - s["start_dev_s"]) for s in segments) / 60, 2)
    doc = {"stack": stack, "proc": segments[-1]["proc"], "start_dev_s": segments[0]["start_dev_s"],
           "segments": segments, "active_min": active_min}
    if "end_dev_s" in segments[-1]:
        doc["end_dev_s"] = segments[-1]["end_dev_s"]
    json.dump(doc, open(path, "w"), indent=1)


def stop_monitor(run_id):
    """Ask a running `monitor <run_id>` to shut down cleanly (see the stop-sentinel comment in monitor()):
    creates runs/<run_id>/.stop, which the monitor loop notices within 0.25s and exits on, running its
    `finally` block (samplers terminated, session.json/clock.json closed out correctly) exactly like a
    Ctrl+C would -- unlike a hard kill (Ctrl+Break, `taskkill /F`, hub stop), which skips `finally`
    entirely and can leave an open session segment's duration wrong (see write_session's `off` docstring).
    Prefer Ctrl+C when monitor is running attached to your own interactive console; use this instead
    whenever it isn't (started detached/backgrounded, or from a different shell/process than the one
    watching it) since a hard kill is otherwise the only externally available option in that case."""
    run_dir = os.path.join(BASE, "runs", run_id)
    if not os.path.isdir(run_dir):
        raise RuntimeError(f"no such run: {run_dir}")
    open(os.path.join(run_dir, ".stop"), "w").close()
    print(f"stop requested for {run_id} -- monitor will exit within ~1s and finish writing its artifacts")


def _pc_find_pid(exe_name):
    """PID of a running PC process by exact image name, via `tasklist` -- used to confirm PresentMon's
    --process_name target is actually alive (see monitor()'s presentmon_target handling / the dashboard's
    "Game exe found" tile). Returns None if not currently running."""
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, encoding="utf-8", errors="ignore").stdout
    if not out or "No tasks" in out:
        return None
    row = next(csv.reader(out.splitlines()), None)
    if row and len(row) >= 2:
        try:
            return int(row[1])
        except ValueError:
            return None
    return None


_PRESENTMON_HINT_IGNORE = {
    "System", "System Idle Process", "svchost.exe", "explorer.exe", "dwm.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "conhost.exe", "RuntimeBroker.exe",
    "SearchHost.exe", "Taskmgr.exe", "cmd.exe", "powershell.exe", "python.exe", "pythonw.exe",
    "WindowsTerminal.exe", "VirtualDesktop.Streamer.exe", "OVRServer_x64.exe",
}


def _fuzzy_match_process(hint):
    """Resolve a free-text game-name hint (what a human actually types, e.g. "half life alyx") against
    whatever's running on the PC right now, for monitor()'s presentmon_hint. A plain substring check
    against the exe name (minus ".exe", spaces ignored) runs first since it's the common case and never
    produces a surprising match; difflib's fuzzy ratio is the fallback for a looser guess (e.g. "hlvr"
    for "hlvr.exe" already hits the substring path, but "halflifealyx" needs the ratio path against
    "hlvr" to have any chance). Returns the exact image name (with ".exe") or None."""
    import difflib
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         encoding="utf-8", errors="ignore").stdout
    names = set()
    for row in csv.reader(out.splitlines()):
        if row and row[0].lower().endswith(".exe") and row[0] not in _PRESENTMON_HINT_IGNORE:
            names.add(row[0])
    if not names:
        return None
    hint_norm = hint.lower().replace(" ", "")
    substr = [n for n in sorted(names) if hint_norm in n[:-4].lower().replace(" ", "")]
    if substr:
        return substr[0]
    stripped = {n[:-4]: n for n in names}
    best = difflib.get_close_matches(hint, list(stripped.keys()), n=1, cutoff=0.45)
    return stripped[best[0]] if best else None


PRESENTMON_HINT_WINDOW_S = 300  # how long after VD/Air Link connects to keep trying to resolve a hint


def monitor(run_id, max_seconds=10800, status_every=30, stack="vd", presentmon_target=None,
            presentmon_hint=None):
    """Open-ended monitoring of a live session (no pktmon: the elevated task is intentionally absent).
    Samples until killed, stopped (`cell.py stop <run_id>` or Ctrl+C), or max_seconds elapses; prints one
    status line per status_every, and detects the streaming client's start/stop so the reduction can
    window on the session instead of the monitor.

    Two ways to get PresentMon an exact game process name instead of it guessing (see
    presentmon_reduce()'s docstring for why guessing is unreliable):
    - presentmon_target: an exact PC image name (e.g. "hlvr.exe") already known up front. Passed
      straight to Sample-GameFPS.ps1's --process_name, so PresentMon only ever captures that process --
      the more efficient path, for anyone who already knows the exe name (a manual/advanced cell.py
      caller, typically).
    - presentmon_hint: a free-text guess at the game's name (e.g. "half life alyx"), for the normal case
      where the actual game isn't running yet when this is asked -- a VR title is almost always launched
      *after* VD/Air Link connects, sometimes minutes later (the wizard asks for this hint before the
      session even starts, precisely because it doesn't need the game running to answer). PresentMon
      isn't even started yet in this case: once VD/Air Link is actually detected connected (see session
      detection below), the hint is fuzzy-matched against `tasklist` every status_every tick for up to
      PRESENTMON_HINT_WINDOW_S seconds *from that point*, not from monitor start -- catching the game
      whenever the player actually launches it, without guessing indefinitely against whatever else gets
      opened later in a multi-hour session. PresentMon is only launched once a match is confirmed
      running, targeted at that exact process from the start via --process_name, for whatever's left of
      max_seconds -- no system-wide capture to filter down after the fact, and if the hint never
      resolves, PresentMon simply never runs at all this session (correctly: there would be nothing
      right to target anyway).
    Either way, the resolved name (or the fact that nothing ever matched) is written to
    presentmon_target.json -- what the dashboard's "Game exe found" tile reads, and what
    results()/presentmon_reduce() use to report a real number or a graceful "never showed up" note
    instead of a guess."""
    run_dir = os.path.join(BASE, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    if not os.path.exists(os.path.join(run_dir, "settings.json")):
        json.dump({"run_id": run_id, "stack": stack, "codec": "live", "bitrate_mbps": None,
                   "content": "motion", "band": "6g"},
                  open(os.path.join(run_dir, "settings.json"), "w"), indent=1)

    # Graceful-stop sentinel: `cell.py stop <run_id>` (or just touching this file) asks the loop below to
    # exit on its next 0.25s tick, so the `finally` block below still runs -- closing samplers cleanly and
    # writing a correct session.json/clock.json, unlike a hard kill (Ctrl+Break, `taskkill /F`, hub stop),
    # which skips `finally` entirely. A stale sentinel from a previous run under this same run_id would
    # otherwise stop the new one instantly, so clear it before the loop starts.
    stop_path = os.path.join(run_dir, ".stop")
    if os.path.exists(stop_path):
        os.remove(stop_path)

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
              "ping": "ping_samples.txt", "session": "session.json",
              "presentmon": "presentmon.csv", "presentmon_target": "presentmon_target.json"}.items()}

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

    def _start_presentmon(target_name, remaining_seconds):
        p = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", qsite.script("game_fps_sampler"),
             "-PresentMonExe", presentmon_exe, "-OutFile", files["presentmon"],
             "-Seconds", str(remaining_seconds + 60), "-ExtraArgs", qsite.get("presentmon_args", ""),
             "-TargetProcess", target_name or ""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if target_name:
            print(f"PresentMon game-fps capture started, targeting '{target_name}' -> {files['presentmon']}")
        else:
            print(f"PresentMon game-fps capture started (system-wide, no target given) -> {files['presentmon']}")
        return p

    presentmon = None
    presentmon_target_found = False
    presentmon_hint_deadline = None
    presentmon_exe = qsite.presentmon_exe()
    if presentmon_exe and os.path.exists(presentmon_exe):
        if presentmon_hint and not presentmon_target:
            # Defer starting PresentMon at all until the hint resolves to an exact process (see the
            # status-tick loop below) -- no point capturing system-wide (extra data, back to the
            # ambiguous "most frames" guess if it's ever used) for the stretch before the game even
            # exists to be found. Worst case, the hint never resolves and PresentMon never runs at all
            # this session -- exactly the right outcome, since there would be nothing correct to target
            # anyway.
            print(f"PresentMon: waiting to see '{presentmon_hint}' launch before starting capture "
                  f"(up to {PRESENTMON_HINT_WINDOW_S // 60} min after VD/Air Link connects)...")
        else:
            presentmon = _start_presentmon(presentmon_target, max_seconds)
    else:
        print("PresentMon missing, skipping PC-side fps collection. Download the console-app build "
              "from https://github.com/GameTechDev/PresentMon/releases/latest, save it as "
              f"PresentMon.exe in {qsite.TOOLS_DIR} (or set presentmon_exe in site.json) to also get "
              "PC-side fps data next run.")

    start_dev = time.time() + off
    sf_layer, sf_prev, sf_next, sf_found_at = None, 0, 0.0, 0.0
    # segments: every VD/Air Link app start..stop the process-presence check observes during this monitor
    # run. The headset can drop into standby (Wi-Fi off, streaming app killed by the OS) without anyone
    # touching it -- observed 2026-09-16 -- and wake up later with the app relaunched; without tracking
    # multiple segments the old single-`session` dict latched permanently "closed" after the first end and
    # never re-armed, so a second play window in the same monitor run went undetected. `current` is the
    # in-progress segment (None between sessions); `segments` is the full history for this run. Seeded
    # from any session.json already on disk so restarting `monitor` on the same run_id (e.g. to pick up
    # a code change, or after a hard-kill/crash) doesn't silently discard already-completed segments --
    # only the in-memory list would otherwise reset, even though the sample TSVs themselves just keep
    # appending regardless of a monitor restart.
    current, segments = None, []
    if os.path.exists(files["session"]):
        try:
            prev = json.load(open(files["session"]))
            loaded = prev.get("segments")
            if loaded is None and prev.get("start_dev_s"):
                loaded = [{"proc": prev.get("proc"), "start_dev_s": prev["start_dev_s"],
                          **({"end_dev_s": prev["end_dev_s"]} if "end_dev_s" in prev else {})}]
            if loaded:
                if "end_dev_s" not in loaded[-1]:
                    # was still open when this file was last written; the writing process is gone now
                    # (we're starting fresh), so close it at the file's own mtime as the best available
                    # estimate of when that monitor process stopped updating it.
                    loaded[-1]["end_dev_s"] = os.path.getmtime(files["session"])
                segments = loaded
                print(f"resumed {len(segments)} prior session segment(s) from {files['session']}")
        except (OSError, ValueError, KeyError):
            pass
    samples = {"wifi_prev": None, "ping_replies": 0, "ping_timeouts": 0, "sf_frames": 0.0}
    last_status = 0.0
    stopped_gracefully = False
    try:
        while time.time() - (start_dev - off) < max_seconds:
            if os.path.exists(stop_path):
                stopped_gracefully = True
                print(f"[{time.strftime('%H:%M:%S')}] stop requested ({stop_path}) -- shutting down cleanly")
                break
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
                if procs and current is None:
                    current = {"proc": procs, "start_dev_s": round(time.time() + off, 3)}
                    segments.append(current)
                    write_session(files["session"], stack, segments, off)
                    print(f"[{time.strftime('%H:%M:%S')}] SESSION DETECTED (#{len(segments)}): {procs}")
                    if presentmon_hint_deadline is None and presentmon_hint and not presentmon_target_found:
                        # The clock on resolving the hint starts from VD/Air Link actually connecting,
                        # not from monitor start -- a VR title is almost always launched after that,
                        # sometimes minutes later, so starting the window any earlier would burn through
                        # it before the game even exists to be found.
                        presentmon_hint_deadline = now + PRESENTMON_HINT_WINDOW_S
                        # "searching": True lets the dashboard tell "still looking" apart from "nothing
                        # requested" (file simply absent) or "gave up" (searching: False, process: None).
                        json.dump({"process": None, "pid": None, "hint": presentmon_hint, "found_at": None,
                                  "searching": True}, open(files["presentmon_target"], "w"), indent=1)
                elif current is not None and not procs:
                    current["end_dev_s"] = round(time.time() + off, 3)
                    write_session(files["session"], stack, segments, off)
                    print(f"[{time.strftime('%H:%M:%S')}] SESSION ENDED (#{len(segments)}, "
                          f"{(current['end_dev_s'] - current['start_dev_s']) / 60:.1f} min) "
                          f"-- will re-arm if the app restarts (e.g. after headset sleep/wake)")
                    current = None
                if presentmon_target and not presentmon_target_found:
                    pid = _pc_find_pid(presentmon_target)
                    if pid:
                        presentmon_target_found = True
                        json.dump({"process": presentmon_target, "pid": pid, "searching": False,
                                  "found_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                  open(files["presentmon_target"], "w"), indent=1)
                        print(f"[{time.strftime('%H:%M:%S')}] PRESENTMON TARGET FOUND: "
                              f"{presentmon_target} (PID {pid})")
                elif presentmon_hint and not presentmon_target_found and presentmon_hint_deadline is not None:
                    match = _fuzzy_match_process(presentmon_hint)
                    pid = _pc_find_pid(match) if match else None
                    if pid:
                        presentmon_target_found = True
                        remaining = max(60, int(max_seconds - (now - (start_dev - off))))
                        presentmon = _start_presentmon(match, remaining)
                        json.dump({"process": match, "pid": pid, "hint": presentmon_hint, "searching": False,
                                  "found_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                  open(files["presentmon_target"], "w"), indent=1)
                        print(f"[{time.strftime('%H:%M:%S')}] PRESENTMON TARGET FOUND: {match} "
                              f"(PID {pid}, matched from hint '{presentmon_hint}') -- capture starting now")
                    elif now > presentmon_hint_deadline:
                        presentmon_target_found = True  # stop trying; one final write, then leave it alone
                        json.dump({"process": None, "pid": None, "hint": presentmon_hint, "found_at": None,
                                  "searching": False}, open(files["presentmon_target"], "w"), indent=1)
                        print(f"[{time.strftime('%H:%M:%S')}] presentmon hint '{presentmon_hint}' never "
                              f"matched a running process within {PRESENTMON_HINT_WINDOW_S // 60} min of "
                              "the session starting -- giving up")
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
        for p in (logcat, ping, sampler, pc, presentmon):
            if p is None:
                continue
            # taskkill /T, not p.terminate(): the PowerShell-wrapped samplers (sampler, pc, presentmon)
            # each launch their own child (presentmon in particular spawns PresentMon-*.exe underneath
            # it), and Windows TerminateProcess -- what Popen.terminate() calls -- kills only the one PID
            # handed to it, not that PID's children. Without /T, a stop (graceful or hard) leaves those
            # grandchildren running and still holding their output files open, e.g. an orphaned
            # PresentMon-*.exe blocking a later `rm` of the run directory -- confirmed by hand while
            # testing this cleanup path. /F is not "less graceful" here: Windows has no signal-based
            # terminate the way Unix does, so Popen.terminate() was already an immediate kill of whatever
            # it reached; this just makes that kill reach the whole subtree instead of stopping short.
            try:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        clock["cell_start_dev_s"] = round(start_dev, 3)
        clock["cell_end_dev_s"] = round(time.time() + off, 3)
        if current is not None and "end_dev_s" not in current:
            current["end_dev_s"] = clock["cell_end_dev_s"]
        if segments:
            write_session(files["session"], stack, segments, off)
        if not presentmon_target_found and (presentmon_target or presentmon_hint):
            # Written even on failure so results()/the wizard can tell "never found" apart from "no
            # target was ever requested" (file absent) -- the former is a graceful-failure note, the
            # latter is the ordinary system-wide-guess path. Reached here (rather than the loop's own
            # deadline check) when the session ended before an exact target ever showed up, or before a
            # hint's resolution window elapsed.
            json.dump({"process": None, "pid": None, "hint": presentmon_hint, "searching": False,
                      "found_at": None, **({"target": presentmon_target} if presentmon_target else {})},
                      open(files["presentmon_target"], "w"), indent=1)
        json.dump(clock, open(os.path.join(run_dir, "clock.json"), "w"), indent=1)
        if stopped_gracefully and os.path.exists(stop_path):
            os.remove(stop_path)
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
    out = _parse_wifi_status(wifi)
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
    if is_private_run(run_id):
        print(f"private run ({PRIVATE_RUN_PREFIX}* prefix) -- skipping results.csv, results.json only")
    else:
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
    # app_frame_throttle is a per-row headroom PERCENTAGE (100 = fully unthrottled), not a duration --
    # confirmed live, 2026-09-18: it read a constant 100.0 across an entire session with no throttling.
    # summing it used to be labeled "_seconds" and produced a large, meaningless number; mean is the
    # only aggregation that means anything for a percentage.
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
                          ("app_frame_throttle", "ovr_throttle_pct_mean", "mean")):
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


def _presentmon_fps_stats(rows, app_col, ms_col, process_name):
    fps = []
    for r in rows:
        if r.get(app_col) != process_name:
            continue
        try:
            ms = float(r[ms_col])
            if ms > 0:
                fps.append(1000.0 / ms)
        except (KeyError, TypeError, ValueError):
            continue
    if not fps:
        return None
    s = sorted(fps)
    p1low = s[max(0, int(len(s) * 0.01) - 1)]
    return {"pc_game_process": process_name, "pc_game_frame_count": len(fps),
            "pc_game_fps_mean": round(sum(fps) / len(fps), 2), "pc_game_fps_min": round(min(fps), 2),
            "pc_game_fps_1pct_low": round(p1low, 2)}


def presentmon_reduce(path, exclude_procs=("VirtualDesktop.Streamer", "svchost", "dwm", "explorer",
                                            "oculus", "OVRServer"), session=None, off=0.0,
                      target_process=None):
    """Reduce a PresentMon capture into the PC game's own present-rate stats -- the one layer
    findings.md calls out as invisible to every other sampler here (the headset/OVR telemetry only ever
    sees the HEADSET compositor's frame rate). Column names vary across PresentMon versions, so this
    reads whichever known variant is present rather than assuming one schema.

    `target_process` (an exact image name, e.g. "hlvr.exe") comes from a run whose PresentMon capture
    was launched with --process_name -- see monitor()'s presentmon_target/presentmon_hint handling and
    wizard.py's ask_presentmon_hint(). When given, this is unambiguous: just report that process's
    frames, or a clear note if it never presented any (didn't launch, crashed, or wasn't actually the
    one rendering). No target given falls back to
    guessing, which has two known failure modes handled explicitly rather than silently producing a
    misleading number:
    1. Without windowing, frames presented before the game started or after it closed (menu, loading,
       the desktop) get mixed into "the game"'s stats. `session`/`off` (session.json's segments + the
       clock offset, same convention as decay_events()) restrict the analysis to the streaming app's own
       active window(s), when the capture has absolute per-row timestamps (Sample-GameFPS.ps1 passes
       --date_time for exactly this reason). Without --date_time in the capture, or without a session,
       this falls back to reducing the whole file unwindowed, same as before.
    2. Even windowed, "pick whichever process has the most frames" is confirmed wrong (2026-09-18)
       whenever something else on the desktop presents at a higher, steadier rate than a stalling game
       -- a browser/terminal at a solid 60fps outscores a game stuttering at 20fps, picking exactly the
       wrong process at exactly the moment the stall is worth seeing. This guess is a convenience for
       Quick Test's zero-setup path, not something to trust for a real investigation -- use
       target_process whenever the reading matters. PresentMon also needs elevated privilege to name
       short-lived or other-account processes at all; without it they're lumped under the literal string
       "<unknown>", excluded from guessing like the streamer/OS names since it could be several
       unrelated processes, not one."""
    if not os.path.exists(path):
        return {}
    try:
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    except OSError:
        return {}
    if not rows:
        return {}
    hdr = set(rows[0].keys())

    def pick(*names):
        return next((n for n in names if n in hdr), None)
    app_col = pick("Application", "process_name", "ProcessName")
    ms_col = pick("MsBetweenPresents", "msBetweenPresents", "ms_between_presents", "MsBetweenDisplayChange")
    if not app_col or not ms_col:
        return {"pc_game_fps_note": f"unrecognized PresentMon CSV schema ({sorted(hdr)[:6]}...)"}

    ts_col = pick("CPUStartDateTime", "TimeInDateTime")
    segs = (session or {}).get("segments")
    windows = None
    if ts_col and segs:
        windows = [(s["start_dev_s"] - off, s.get("end_dev_s", time.time() + off) - off) for s in segs]

    def row_epoch(raw):
        # "2026-9-18 8:57:43.386354900" -- PresentMon's --date_time format: no leading zeros, up to
        # nanosecond precision. datetime.strptime chokes on both, so this is parsed by hand.
        import datetime as _dt
        try:
            date_part, time_part = raw.split(" ", 1)
            y, mo, d = (int(x) for x in date_part.split("-"))
            h, mi, sec = time_part.split(":")
            whole, _sep, frac = sec.partition(".")
            micros = int((frac + "000000")[:6]) if frac else 0
            return _dt.datetime(y, mo, d, int(h), int(mi), int(whole), micros).timestamp()
        except (ValueError, IndexError):
            return None

    if windows:
        kept = [r for r in rows if r.get(ts_col) and
                (lambda t: t is not None and any(w0 <= t <= w1 for w0, w1 in windows))(row_epoch(r[ts_col]))]
        if kept:
            rows = kept

    if target_process:
        stats = _presentmon_fps_stats(rows, app_col, ms_col, target_process)
        if stats:
            return stats
        return {"pc_game_fps_note": f"no frames captured from '{target_process}' -- it never presented "
                "a frame in this session (didn't launch, crashed, or wasn't the one actually rendering)"}

    def excluded(name):
        if not name or name == "<unknown>":
            return True
        low = name.lower()
        return any(x.lower() in low for x in exclude_procs)

    from collections import Counter
    counts = Counter(r[app_col] for r in rows if not excluded(r.get(app_col)))
    if not counts:
        unknown_frames = sum(1 for r in rows if r.get(app_col) == "<unknown>")
        if unknown_frames:
            return {"pc_game_fps_note": f"{unknown_frames} frame(s) captured but PresentMon couldn't "
                    "name the process (it needs admin privilege for short-lived/other-account "
                    "processes) -- re-run elevated for a real reading"}
        return {}
    game = counts.most_common(1)[0][0]
    return _presentmon_fps_stats(rows, app_col, ms_col, game) or {}


def decay_events(run_dir, frac=0.4, min_s=20.0, gap_s=10.0, out_tsv=None, session=None, off=0.0):
    """The community's 'VD bitrate decay' = a sustained collapse of the delivered rate. Detect every
    episode and report the PC-side encoder/TCP state inside it (+ the minute before it) versus the whole
    session: encoder utilization falling means the encoder stopped producing; a retransmit spike means the
    TCP transport collapsed.

    `session`/`off`, when given, restrict the analysis to the streaming app's own active segment(s) --
    session.json's start_dev_s/end_dev_s, converted from device-epoch to the PC-epoch _rate_series() uses
    via `t - off`. Without this, a monitor that keeps sampling for a while after the app closes (or before
    it starts) sees delivered rate fall to ~0 in that dead time and misreports it as a decay episode --
    exactly the false pattern this whole detector exists to rule out. Confirmed live 2026-09-17: a 96s
    session inside a 117s monitor run flagged a 26s "decay episode" that lined up almost exactly with the
    tail after the app closed (encoder utilization and TCP send rate both collapsed to ~0 in that window,
    with zero retransmits -- a stopped source, not a degraded link)."""
    ser = _rate_series(run_dir)
    segs = (session or {}).get("segments")
    if segs:
        windows = [(s["start_dev_s"] - off, s.get("end_dev_s", time.time() + off) - off) for s in segs]
        ser = [(t, r) for t, r in ser if any(w0 <= t <= w1 for w0, w1 in windows)]
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


PRIVATE_RUN_PREFIX = "priv_"


def is_private_run(run_id):
    """A run_id starting with `priv_` (e.g. priv_ram3600_..., matched via `runs/priv_*/` in .gitignore)
    is this rig's own scratch/sanity-check data, not part of the published dataset -- results()/passive()
    skip appending its row to the shared, git-tracked results.csv so it can never end up committed by
    forgetting a manual step. results.json still gets written inside the (gitignored) run directory, so
    fingerprint/dashboard/local inspection all still work; only the row in the tracked CSV is skipped."""
    return run_id.startswith(PRIVATE_RUN_PREFIX)


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
def watch(run_id, interval=30, drop_frac=0.6, heartbeat_min=5, beep=False, headset_beep=False):
    """Unattended watcher for a long soak: prints only when state changes (delivered rate collapsing, TCP
    retransmits appearing) plus a periodic heartbeat, so an episode gets timestamped even when nobody is
    watching the monitor's status lines. beep=True sounds an alert chime on DECAY SUSPECT / retransmit
    spike (and a recovery chime once the rate is back to normal) -- useful for the same live AP/headset
    placement testing `linktest` targets, but while an actual stream is running.
    headset_beep additionally posts a silent notification-history marker on the headset (see
    _alert_headset -- NOT an audible/visible alert, a verified dead end on this build); off by default,
    still an on-device mutation."""
    import statistics as _stats
    run_dir = os.path.join(BASE, "runs", run_id)
    adb_ser = adb_serial() if headset_beep else None
    print(f"watch started: {run_id} interval={interval}s drop_frac={drop_frac} beep={beep} "
          f"headset_beep={headset_beep}", flush=True)
    last_hb, low_streak, last_retrans, alarm = 0.0, 0, None, False
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
            spike = last_retrans is not None and retr > max(50.0, last_retrans * 10)
            if low_streak == 2:
                print(f"[{now}] DECAY SUSPECT: rate {cur:.0f} Mbps vs median {med:.0f} ({cur / med * 100:.0f}%)"
                      f" | encoder {enc}% | retrans {retr:.0f}/s | ws {ws} MB", flush=True)
            if spike:
                print(f"[{now}] TCP RETRANSMIT SPIKE: {retr:.0f}/s (prev {last_retrans:.0f}) | rate {cur:.0f} Mbps"
                      f" | encoder {enc}%", flush=True)
            if beep or headset_beep:
                bad = low_streak >= 2 or spike
                if bad and not alarm:
                    alarm = True
                    if beep:
                        _alert(degrade=True)
                    if headset_beep:
                        _alert_headset(adb_ser, degrade=True)
                elif not bad and alarm and low_streak == 0:
                    alarm = False
                    if beep:
                        _alert(degrade=False)
                    if headset_beep:
                        _alert_headset(adb_ser, degrade=False)
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

    # Direct call, not a sys.executable subprocess: the latter breaks under a frozen/bundled build the
    # same way dashboard.py's old subprocess launch did (see its make_server() docstring) -- there is
    # no plain python.exe to hand a sibling .py file to when sys.executable is the bundled exe itself.
    w = analyze.reduce_cell(os.path.join(BASE, "runs"), run_id, quest_ip=QUEST_IP, window="30,150")

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

    # ovr metrics (newest CSV, windowed to the actual cell duration) - VD only; stale_frame_count is
    # a PER-SECOND bucket. The OVR service writes one CSV per session, so a cell with OVR logging
    # disabled would otherwise silently inherit the previous session's numbers: gate on the file's
    # device mtime vs the cell window.
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
        # 170s used to be a hardcoded constant here, sized for the old fixed-duration `capture()`
        # cells (150s + margin). `monitor()` sessions run open-ended and are routinely much longer --
        # a 5-minute play session silently had its first ~2 minutes excluded from every OVR-derived
        # stat (avg fps, stale-frame counts, GPU/CPU util, ...) with no indication that had happened.
        # Confirmed live, 2026-09-18: a real stutter (12 consecutive stale frames) at the 2:43 mark of
        # a ~5 min session was completely absent from results.json because it fell outside the fixed
        # window, while a smaller one at 7:09 was the only one visible -- looking like an isolated
        # blip instead of the second of two. Size the window to the actual cell duration instead, with
        # 170s as a floor (not a ceiling) so short cells keep their old behavior unchanged.
        cell_end = clock.get("cell_end_dev_s")
        tail_s = max(170, (cell_end - start_dev) + 15) if (cell_end and start_dev) else 170
        ovr.update(ovr_window(ovr_csv, tail_s=tail_s))

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
    res.update(decay_events(run_dir, out_tsv=os.path.join(run_dir, "decay_episodes.tsv"),
                            session=sess, off=(clock.get("offset_s") or 0.0)))
    res.update(cm_reduce(os.path.join(run_dir, "cm_wifi_snapshots.txt"),
                         session_start_dev_s=start_dev,
                         offset_s=(clock.get("offset_s") or 0.0),
                         out_tsv=os.path.join(run_dir, "controller_events.tsv")))
    res.update(codec_events(run_dir, ser, since_dev_s=vr_start,
                            out_tsv=os.path.join(run_dir, "codec_events.tsv")))
    target_path = os.path.join(run_dir, "presentmon_target.json")
    presentmon_target, presentmon_gave_up = None, None
    if os.path.exists(target_path):
        try:
            info = json.load(open(target_path))
            presentmon_target = info.get("process")
            if presentmon_target is None:
                presentmon_gave_up = info.get("target") or info.get("hint") or "the requested game"
        except (OSError, ValueError):
            pass
    if presentmon_gave_up:
        # A target/hint was explicitly requested and never resolved -- report that plainly instead of
        # silently falling back to presentmon_reduce()'s ambiguous "most frames" guess, which is exactly
        # the unreliable path an explicit target/hint exists to avoid.
        res["pc_game_fps_note"] = (f"never found a process matching '{presentmon_gave_up}' during this "
                                    "session -- no PC game-fps data captured")
    else:
        res.update(presentmon_reduce(os.path.join(run_dir, "presentmon.csv"),
                                     session=sess, off=(clock.get("offset_s") or 0.0),
                                     target_process=presentmon_target))
    if clock:
        res["quest_clock_offset_s"] = clock.get("offset_s")
        res["quest_uptime_s"] = clock.get("dev_uptime_s")
        res["quest_cell_start_dev_s"] = start_dev
        res["quest_cell_end_dev_s"] = clock.get("cell_end_dev_s")
    if sess:
        res["quest_session_start_dev_s"] = sess.get("start_dev_s")
        segs = sess.get("segments")
        if segs is not None:
            # active_min excludes any gap where the streaming app wasn't running (e.g. headset asleep
            # between segments) -- see write_session(). quest_session_segments > 1 flags that a
            # sleep/wake or app-restart happened mid-monitor, worth knowing when reading the numbers.
            res["quest_session_min"] = sess.get("active_min")
            res["quest_session_segments"] = len(segs)
        else:
            # old-format session.json (single segment, no "segments" key) from a run predating this fix.
            res["quest_session_min"] = (round((sess["end_dev_s"] - sess["start_dev_s"]) / 60, 1) if sess.get("end_dev_s")
                                        else round(res.get("vr_api_lines", 0) / 60, 1) or None)

    json.dump(res, open(os.path.join(run_dir, "results.json"), "w"), indent=1)

    s = json.load(open(os.path.join(run_dir, "settings.json")))
    id_cols, meas_cols = ID_COLS, MEAS_COLS
    if is_private_run(run_id):
        print(f"private run ({PRIVATE_RUN_PREFIX}* prefix) -- skipping results.csv, results.json only")
    else:
        csv_upsert(os.path.join(BASE, "results.csv"), id_cols, meas_cols,
                   {**{c: s.get(c) for c in id_cols}, **res})
    print("wrote results.json" + ("" if is_private_run(run_id) else " + results.csv"))
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
    elif sys.argv[1] == "stop":
        stop_monitor(sys.argv[2])
    elif sys.argv[1] == "watch":
        flags = sys.argv[2:]
        rest = [a for a in flags if a not in ("--beep", "--headset-beep")]
        interval = int(rest[1]) if len(rest) > 1 else 30
        watch(rest[0], interval, beep=("--beep" in flags), headset_beep=("--headset-beep" in flags))
    elif sys.argv[1] == "passive":
        passive(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "start")
    elif sys.argv[1] == "capture":
        capture(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 150)
    elif sys.argv[1] == "results":
        results(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif sys.argv[1] == "fingerprint":
        rest = sys.argv[2:]
        tag = next((a.split("=", 1)[1] for a in rest if a.startswith("--tag=")), None)
        fingerprint(rest[0], save_baseline=("--save-baseline" in rest), tag=tag)
    elif sys.argv[1] == "linktest":
        flags = sys.argv[2:]
        linktest(beep=("--no-beep" not in flags), headset_beep=("--headset-beep" in flags))
