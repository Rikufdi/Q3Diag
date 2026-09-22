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
  headset_logcat.txt      the headset's own logcat (see HEADSET_LOG_TAGS): VrApi/QC2Comp give
                          per-second FPS/Stale/Tear/Early/Prd/TW/App/CFL/ICFL/PoseAge/quality-scaling
                          lines (any stack) plus the codec process's decoder stats (output fps, Mbps,
                          queue lag), and the streaming-client tags are captured opportunistically --
                          on the reference rig VD's own tag emits only SELinux audit lines, so treat
                          them as a bonus for other builds, not as the source of bitrate/connection data
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

# The headset logcat stream, and the tags it is filtered to. One stream for two jobs (a session costs
# one adb reader, not two -- adb traffic shares the very link being measured):
#   - the VR runtime: VrApi (compositor frame telemetry, the only frame source that also covers Air
#     Link) and QC2Comp (the hardware decoder's own output rate/bitrate/queue lag, and the codec
#     identity in its instance name).
#   - the streaming client's own tags, captured opportunistically. The premise that the client logs its
#     bitrate adaptation/codec/connection decisions does NOT hold on this rig (verified live
#     2026-09-22: VirtualDesktop.Android emits only `avc: denied` audit lines, VR_Engine/OVRMediaCodec/
#     ALVR are silent, VD's PC logs are not written during a stream at all -- see
#     QUEST-AGENT-PLAYBOOK.md). They cost nothing to keep in the filter (a tag that never emits costs
#     one match attempt and 0 bytes) and would pick up a build that does log there.
# `headset_log_tags` in site.json (or QUEST3_HEADSET_LOG_TAGS) replaces the list wholesale, e.g. to add
# `tag:W`-style priority filters, or to drop the client tags on a rig with a chatty build.
HEADSET_LOGCAT_NAME = "headset_logcat.txt"
HEADSET_LOG_TAGS = (qsite.get("headset_log_tags") or
                    "VrApi QC2Comp VirtualDesktop.Android OVRMediaCodec VR_Engine ALVR").split()

# ---------------------------------------------------------------- data footprint
# Measured write rates for the artifacts a run produces, in MB per minute. The wizard and the live
# dashboard use these to tell the operator where the data lands and how big it gets before an
# hour-long session quietly fills a disk. Every number is observed, with its source, so it can be
# re-derived rather than trusted:
#   samplers   -- every always-on file (Wi-Fi/net/env/sf TSVs, VrApi logcat, OVR metrics, pc_samples,
#                 ping). vd_nvenc_soak_20260916-0420 wrote ~7.2 MB of non-trace artifacts in 49.4 min.
#   presentmon -- the optional game-fps CSV, ~253 B per present, so it scales with the game's own frame
#                 rate: measured 2.4-2.7 MB/min at ~160 fps (priv_ram3600, vd_live_20260916-1831/0555).
#   trace      -- the optional WPR trace (CPU profile, 2 ms interval). trace-state.json etl_mb over
#                 capture_seconds: 7811/332, 3879/172, 4196/188 MB/s -> 22.3-23.5 MB/s, so ~1.3 GB/min.
DATA_RATES_MB_PER_MIN = {
    "samplers": 0.15,
    "presentmon": 2.5,
    "trace": 1300.0,
}


def estimate_run_mb(minutes, presentmon=False, trace=False):
    """Estimated artifact size in MB for a session of `minutes`, broken down by component plus a
    "total" key. Only the components actually enabled are included, so a Quick Test with no trace and
    no PresentMon stays tiny."""
    mb = {"samplers": DATA_RATES_MB_PER_MIN["samplers"] * minutes}
    if presentmon:
        mb["presentmon"] = DATA_RATES_MB_PER_MIN["presentmon"] * minutes
    if trace:
        mb["trace"] = DATA_RATES_MB_PER_MIN["trace"] * minutes
    mb["total"] = sum(mb.values())
    return mb


def format_mb(mb):
    """Human-readable size: GB once it would be awkward as MB, so a traced hour reads as GB not MB.
    Sub-10 MB keeps a decimal so the tiny always-on sampler files for a short Quick Test don't all
    round to the same whole number."""
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    if mb < 10:
        return f"{mb:.1f} MB"
    return f"{mb:.0f} MB"


# results.csv schema: identity columns + everything the reductions can produce.
ID_COLS = ["run_id", "stack", "codec", "bitrate_mbps", "content", "band"]
MEAS_COLS = [
    "wire_tx_mbps", "wire_rx_mbps", "wire_pkts_per_s", "retry_rate_pct", "lost_rate_pct",
    "retry_rate_p90_pct", "retry_rate_max_pct", "retry_bursts", "retry_bursts_pct",
    "retry_lost_packets", "retry_burst_lost_packets", "retry_burst_rssi_dbm_min",
    "retry_burst_link_mbps_min", "retry_counter_resets",
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
    "vr_api_tear_total", "vr_api_tear_seconds", "vr_api_early_total", "vr_api_early_seconds",
    "vr_api_prd_ms_mean", "vr_api_prd_ms_max", "vr_api_dpu_scale_min",
    "vr_api_dpu_scale_below_native", "vr_api_dropped_frames_total", "vr_api_late_motion_total",
    "vr_api_preempt_total", "vr_api_free_mb_min", "vr_api_lat_ms_max",
    "codec_stream_instance", "codec_stream_samples", "codec_stream_fps_mean", "codec_stream_fps_min",
    "codec_stream_seconds_below_60fps",
    "codec_stream_mbps_mean", "codec_stream_mbps_min", "codec_stream_lag_max",
    "codec_stream_workrate_min", "codec_stream_other_instances",
    "codec_stream_decoder", "codec_stream_codec", "codec_stream_low_latency",
    "codec_stream_bit_depth", "codec_stream_configured_codec", "codec_stream_codec_mismatch",
    "codec_stream_instance_count", "codec_stream_instances_late",
    "tcp_retrans_segs", "tcp_in_errs", "wlan0_rx_errs", "wlan0_rx_drop", "wlan0_tx_errs", "wlan0_tx_drop",
    "p2p0_rx_mbps", "p2p0_tx_mbps", "p2p0_rx_errs", "p2p0_rx_drop", "p2p0_tx_errs", "p2p0_tx_drop",
    "cm_snapshots", "cm_events_session", "cm_ctrl_last_left", "cm_ctrl_last_right",
    "cm_ctrl_connected_active", "cm_ctrl_connected_inactive", "cm_ctrl_connecting", "cm_ctrl_searching",
    "cm_ctrl_disabled", "cm_p2p_gc_connect", "cm_p2p_channel_switch", "cm_p2p_go_create_success",
    "cm_low_latency_toggle", "cm_concurrency_change",
    "cm_map_share_sends", "cm_map_share_kib", "cm_map_share_gap_median_s", "cm_map_share_gap_max_s",
    "pc_samples", "pc_enc_util_mean", "pc_enc_util_min", "pc_enc_util_max", "pc_gpu_util_mean",
    "pc_tcp_retrans_mean", "pc_tcp_retrans_max_per_s", "pc_tcp_sent_mean_per_s",
    "pc_dpc_pct_mean", "pc_dpc_pct_max", "pc_isr_pct_mean", "pc_isr_pct_max",
    "pc_mem_avail_min_mb", "pc_mem_pages_max_per_s", "pc_page_faults_max_per_s",
    "pc_game_prio", "pc_game_cpu_pct_mean", "pc_game_cpu_pct_min", "pc_game_ws_mb_mean",
    "pc_game_pf_max_per_s", "pc_game_threads_mean", "pc_game_thr_wait_at_min_cpu",
    "pc_top_procs_at_min_game_cpu",
    "pc_streamer_cpu_pct_mean", "pc_streamer_cpu_pct_max", "pc_streamer_cpu_pct_min",
    "pc_vr_cpu_pct_mean", "pc_vr_cpu_pct_max", "pc_vr_cpu_pct_min", "pc_vr_ws_mb_mean",
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
    "retry_rate_pct", "retry_rate_p90_pct", "retry_rate_max_pct", "retry_bursts",
    "retry_bursts_pct", "retry_lost_packets", "retry_burst_lost_packets",
    "retry_burst_rssi_dbm_min", "retry_burst_link_mbps_min", "lost_rate_pct", "ping_rtt_p50_ms", "ping_rtt_p95_ms", "ping_loss_pct",
    "vr_api_fps_mean", "vr_api_seconds_below_85fps", "vr_api_stale_per_min",
    "pc_enc_util_mean", "pc_tcp_retrans_mean", "decay_episodes", "decay_total_min",
    "pc_dpc_pct_max", "pc_isr_pct_max",
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
    # Worst single-core DPC/ISR% seen in the run. Trips on a clear rise (≥2 points AND ≥2x baseline):
    # a driver that starts monopolising a core for ~100 ms stretches is exactly what produces
    # PC-side frame stalls with an idle GPU.
    "pc_dpc_pct_max":               ("worse_high", 2.0,   2.0),
    # Worst per-interval Wi-Fi retry rate. Deliberately the MAX, not the run mean: the mean sits at
    # ~2 % in runs whose samples peak at 76 %, and that averaging is what hid the real fault.
    "retry_rate_max_pct":           ("worse_high", 2.0,   5.0),
    "pc_isr_pct_max":               ("worse_high", 2.0,   2.0),
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
    if not tag:
        # Tag = the identity columns that define a configuration, taken in ID_COLS order (minus the
        # run_id), so it cannot silently drift out of step with them again. `content` belongs in it: a
        # motion run and a static run at the same stack/codec/bitrate/band are different workloads, and
        # leaving it out made the fingerprint compare apples to oranges -- confirmed 2026-09-19, a
        # motion run was diffed against a *static* baseline. Consequence of the change: old-format
        # baseline files are no longer matched by name, so the first run on each configuration reports
        # "no prior baseline" once and establishes a fresh one.
        tag = "_".join(str(settings.get(k) if settings.get(k) is not None else "na")
                       for k in ("stack", "codec", "bitrate_mbps", "content", "band", "bt"))
    tag = re.sub(r"[^A-Za-z0-9_.+-]", "_", str(tag))
    metrics = _fp_derive(res)

    fp_dir = os.path.join(BASE, "baseline", "fingerprints")
    os.makedirs(fp_dir, exist_ok=True)
    baseline_path = os.path.join(fp_dir, f"{tag}.json")
    diff_path = os.path.join(run_dir, "fingerprint_diff.json")
    fp = {"tag": tag, "run_id": run_id, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
          "settings": {k: settings.get(k) for k in ("stack", "codec", "bitrate_mbps", "content",
                                                    "band", "bt")},
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
    alert, verified dead end as of 2026-09-16: this Horizon OS build's shell notification tool has no
    flag for sound/vibration/priority at all (`-h` lists only -t/-i/-I/-S/-c), and the posted
    notification carries `sound=null vibrate=null` --
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


# Network identifiers that must never reach a published artifact. The Quest's controller link always
# sits on 192.168.49.0/24 -- identical on every unit -- so it is excluded here as well as in the
# sampler, rather than turning a device constant into noise.
_SENSITIVE_MAC_RE = re.compile(rb"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b")
_SENSITIVE_IP_RE = re.compile(rb"\b(?!192\.168\.49\.)(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3})\b")
_SENSITIVE_SSID_RE = re.compile(rb'(SSID:\s*")([^"]+)(")')


def redact_artifacts(run_dir, names=("ping_samples.txt", "cm_wifi_snapshots.txt")):
    """Strip network identifiers from the raw-text artifacts of a run, in place. Returns what changed.

    The headset sampler redacts as it writes (Protect-Identifiers in Sample-Quest.ps1), but two paths
    bypass it: ping_samples.txt comes from a plain `ping` redirect issued by this process, so it carries
    the headset's LAN address on every line, and a hard-killed session (taskkill, hub stop) never runs
    the sampler's own exit path. This therefore runs at the end of every session *and* at the start of
    results(), so whichever happens first, an artifact that reaches a repository or a bug report has
    already been through it.

    Ordinals (mac1, ip1) rather than a hash, matching the sampler: a MAC carries only 48 bits, so a hash
    would be trivially reversible, while ordinals keep the relational structure later analysis needs
    ("the same AP as the previous sample")."""
    macs, ips, ssids = {}, {}, {}

    def sub_mac(m):
        macs.setdefault(m.group(0).lower(), b"mac%d" % (len(macs) + 1))
        return macs[m.group(0).lower()]

    def sub_ip(m):
        ips.setdefault(m.group(0), b"ip%d" % (len(ips) + 1))
        return ips[m.group(0)]

    def sub_ssid(m):
        ssids.setdefault(m.group(2), b"ssid%d" % (len(ssids) + 1))
        return m.group(1) + ssids[m.group(2)] + m.group(3)

    changed = []
    for name in names:
        path = os.path.join(run_dir, name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        out = _SENSITIVE_IP_RE.sub(sub_ip, _SENSITIVE_MAC_RE.sub(sub_mac, _SENSITIVE_SSID_RE.sub(sub_ssid, data)))
        if out != data:
            with open(path, "wb") as f:
                f.write(out)
            changed.append(name)
    return changed


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


# Guard rails for _fuzzy_match_process()'s difflib fallback. Deliberately strict: failing to resolve
# a hint is safe (the search keeps running, and a miss is reported by name at the end), whereas
# resolving it to the WRONG process silently costs the run all of its PC frame data -- which is
# exactly what happened on 2026-09-19 (see that function's docstring).
PRESENTMON_HINT_MIN_LEN = 8      # shorter hints must match by substring, or not at all
PRESENTMON_HINT_RATIO_CUTOFF = 0.75


def _fuzzy_match_process(hint):
    """Resolve a free-text game-name hint (what a human actually types, e.g. "half life alyx") against
    whatever's running on the PC right now, for monitor()'s presentmon_hint. Returns the exact image
    name (with ".exe") or None.

    Both sides are flattened to letters+digits before any comparison, because the hint and the exe
    name rarely share punctuation: a human types "angry birds", the file is
    "angry-birds-vr-isle-of-pigs.exe".

    A substring test on that flattened name runs first -- the common case, and it never surprises.

    The difflib fallback is a last resort and is deliberately hard to trip. It used to be cutoff 0.45
    over the *raw* names, which matched "angry" to **MacTray.exe**: a 5-character hint scores 0.50
    against that unrelated 7-character name while scoring only 0.32 against the real, much longer
    "angry-birds-vr-isle-of-pigs" -- the ratio penalises length, so it fails worst exactly where a
    human's short hint is most likely. Confirmed live 2026-09-19: presentmon_target.json recorded
    MacTray.exe for hint 'angry', PresentMon captured that instead of the game, and the run ended with
    no PC frame data at all. A short hint now simply keeps waiting, and the substring path catches the
    game when it launches -- strictly better than locking onto the wrong process early."""
    import difflib
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         encoding="utf-8", errors="ignore").stdout
    names = set()
    for row in csv.reader(out.splitlines()):
        if row and row[0].lower().endswith(".exe") and row[0] not in _PRESENTMON_HINT_IGNORE:
            names.add(row[0])
    if not names:
        return None

    def flat(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())

    hint_norm = flat(hint)
    if not hint_norm:
        return None
    substr = [n for n in sorted(names) if hint_norm in flat(n[:-4])]
    if substr:
        return substr[0]

    if len(hint_norm) < PRESENTMON_HINT_MIN_LEN:
        return None
    by_flat = {}
    for n in sorted(names):
        by_flat.setdefault(flat(n[:-4]), n)
    best = difflib.get_close_matches(hint_norm, list(by_flat), n=1, cutoff=PRESENTMON_HINT_RATIO_CUTOFF)
    return by_flat[best[0]] if best else None


PRESENTMON_HINT_WINDOW_S = 300  # how long after VD/Air Link connects to keep trying to resolve a hint


def monitor(run_id, max_seconds=10800, status_every=30, stack="vd", presentmon_target=None,
            presentmon_hint=None, presentmon_capture=True):
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
              "layers": "sf_layers.log", "logcat": HEADSET_LOGCAT_NAME,
              "cm": "cm_wifi_snapshots.txt", "pc": "pc_samples.tsv",
              "ping": "ping_samples.txt", "session": "session.json",
              "presentmon": "presentmon.csv", "presentmon_target": "presentmon_target.json"}.items()}

    sampler = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SAMPLER,
         "-Adb", ADB, "-Serial", ser, "-OutFile", files["wifi"], "-NetFile", files["net"],
         "-EnvFile", files["env"], "-CmFile", files["cm"],
         "-Seconds", str(max_seconds + 60)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logcat = subprocess.Popen([ADB, "-s", ser, "logcat", "-v", "time", "-s", *HEADSET_LOG_TAGS],
                              stdout=open(files["logcat"], "a", encoding="utf-8"), stderr=subprocess.DEVNULL)
    print(f"headset logcat -> {files['logcat']} (tags: {' '.join(HEADSET_LOG_TAGS)})")
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
        rate = DATA_RATES_MB_PER_MIN["presentmon"]
        if target_name:
            print(f"PresentMon game-fps capture started, targeting '{target_name}' -> {files['presentmon']} "
                  f"(writes ~{rate:.1f} MB/min, scaling with the game's frame rate)")
        else:
            print(f"PresentMon game-fps capture started (system-wide, no target given) -> {files['presentmon']} "
                  f"(writes ~{rate:.1f} MB/min, scaling with the frame rate of whatever presents)")
        return p

    presentmon = None
    presentmon_target_found = False
    presentmon_hint_deadline = None
    presentmon_exe = qsite.presentmon_exe()
    if not presentmon_capture:
        # PresentMon attaches its own ETW session to the game's present path, which is the one part of
        # this harness that touches the thing under measurement -- and the wizard's prompt has always
        # PROMISED that blank meant "skip", while the code turned blank into None and this branch then
        # started a SYSTEM-WIDE capture instead. That cost us the only clean A/B available (does the
        # stutter survive with no PC-side capture at all?), so the promise is now kept: blank really
        # does skip, and system-wide capture is asked for explicitly with "all".
        print("PresentMon: skipped for this run (no PC-side frame data will be collected).")
    elif presentmon_exe and os.path.exists(presentmon_exe):
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
        print("PresentMon: none found -- skipping PC-side fps collection "
              "(drop PresentMon.exe in vendor/ to enable it).")

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
        # Samplers are dead, so nothing is appending any more: scrub the raw-text artifacts here as
        # well as at write time in the sampler. This is the path that also covers ping_samples.txt
        # (written by our own redirect) and any run the sampler did not exit cleanly from.
        redact_artifacts(run_dir)
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
        [ADB, "-s", ser, "logcat", "-v", "time", "-s", *HEADSET_LOG_TAGS],
        stdout=open(os.path.join(run_dir, HEADSET_LOGCAT_NAME), "w", encoding="utf-8"),
        stderr=subprocess.DEVNULL)

    # logcat -s dumps the buffer backlog first (~5 min) - remember the device-clock window so the
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


# Wi-Fi band classification, in one place so every source of "which band" agrees. Edges are the
# allocation boundaries: 2.4 GHz 2400-2483.5 (ch 1-14), 5 GHz 5150-5895 (U-NII-1..3), 6 GHz
# 5925-7125 (U-NII-5..8).
BAND_RANGES = (("2g4", 2400, 2483), ("5g", 5150, 5895), ("6g", 5925, 7125))


def band_from_freq(mhz):
    """Channel centre frequency in MHz -> band label, or None if missing/unrecognised."""
    try:
        f = float(str(mhz).strip())
    except (TypeError, ValueError):
        return None
    for name, lo, hi in BAND_RANGES:
        if lo <= f <= hi:
            return name
    return None


def detect_band(run_dir, serial=None):
    """Which Wi-Fi band the headset was on for this run, from the best evidence available.

    Band is an identity field (settings.json and the fingerprint tag), so "unknown" here is not
    neutral: it splits one configuration into two, minting a separate baseline. Confirmed
    2026-09-19 -- a run that was streaming on 6 GHz, whose headset sampler happened to write only
    blank rows, reported band "unknown" and produced the tag `vd_auto_500_unknown` with its own
    baseline file.

    Evidence order:
      1. `quest_wifi_samples.tsv` freq_mhz, classified per sample and reduced to the *majority* band.
         This is the headset's own STA frequency during the session, so it wins whenever any sample
         is usable. Deliberately not `rows[-1]` (what this replaced): a run that loses the device
         part-way through, or that only ever wrote blank rows, would otherwise come back "unknown"
         despite hundreds of valid samples -- and a session that roamed mid-run would report whichever
         band happened to be last rather than the one it mostly ran on.
      2. a live `adb shell cmd wifi status` on the headset. Present-tense, so weaker evidence -- it is
         the band *now*, not during the run -- but the headset is in practice still associated to the
         same SSID the session ran on, and that beats reporting "unknown". Only reached when (1)
         produced nothing at all.
    """
    counts = {}
    for row in read_tsv(os.path.join(run_dir, "quest_wifi_samples.tsv")):
        b = band_from_freq(row.get("freq_mhz"))
        if b:
            counts[b] = counts.get(b, 0) + 1
    if counts:
        return max(counts, key=counts.get)

    if not os.path.isdir(run_dir):
        return None  # not a run at all -- never answer this from the live device
    try:
        out = _adb("-s", serial or adb_serial(), "shell", "cmd wifi status", timeout=15)
    except Exception:
        return None
    m = re.search(r"Frequency:\s*(\d+)\s*MHz", out or "")
    return band_from_freq(m.group(1)) if m else None


def detect_bluetooth(serial=None):
    """Headset Bluetooth state as "on"/"off", or None if it can't be determined.

    Recorded as part of a run's identity because it demonstrably changes the measurement. Confirmed
    2026-09-19: runs split cleanly by it. Bluetooth on -- uplink retries peaked at 62% across 19 burst
    intervals, 1533 packets lost, the encoder fell to 0% in ~11s stalls and the VR decoder starved at
    10fps; Bluetooth off, same scenario -- 1.9% mean retries, 2 bursts, 78 packets lost, encoder never
    below 6%, decoder never below 90fps. Nothing in the artifacts said which state a run was in, so
    that split read as randomness for most of a session.

    Bluetooth is 2.4GHz and the stream is 6GHz, so this is coexistence on a shared radio front-end
    rather than band overlap -- and it only bites under the stream's load, which is why an idle
    headset measured 0.00-0.15% retries with Bluetooth on."""
    ser = serial or adb_serial()
    if not ser:
        return None
    try:
        out = _adb("-s", ser, "shell", "settings get global bluetooth_on", timeout=15)
    except Exception:
        return None
    v = (out or "").strip()
    return {"1": "on", "0": "off"}.get(v)


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
    """PC-side sampler summary: NVENC utilization (is the encoder producing?), Windows TCP retransmit
    rate (is the transport stalling?), and DPC/ISR time (is a driver -- audio APOs and vendor audio
    services are the usual offenders -- stealing the CPU for long enough to stall frames?)."""
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
    # `*_mean` columns are the across-core average in each sample, so their mean is the typical DPC/ISR
    # load; `*_max` columns are the worst single core in each sample, so their max is the worst excursion
    # any one CPU saw -- the number that matters when a driver storm is pinned to one core.
    for src, dst in (("dpc_pct_mean", "pc_dpc_pct_mean"), ("isr_pct_mean", "pc_isr_pct_mean")):
        v = col(src)
        if v:
            out[dst] = round(sum(v) / len(v), 2)
    for src, dst in (("dpc_pct_max", "pc_dpc_pct_max"), ("isr_pct_max", "pc_isr_pct_max")):
        v = col(src)
        if v:
            out[dst] = round(max(v), 2)
    # Memory and the game's own scheduling state. These separate two failures that look identical in
    # every other column -- the game pinned at ~10 fps with an idle GPU, which is either memory
    # starvation (hard faults) or the process being throttled/descheduled (EcoQoS puts it at Idle).
    avail = col("mem_avail_mb")
    if avail:
        out["pc_mem_avail_min_mb"] = min(avail)
    for src, dst in (("mem_pages_per_s", "pc_mem_pages_max_per_s"),
                     ("mem_page_faults_per_s", "pc_page_faults_max_per_s"),
                     ("game_pf_per_s", "pc_game_pf_max_per_s")):
        v = col(src)
        if v:
            out[dst] = round(max(v), 1)
    gws = col("game_ws_mb")
    if gws:
        out["pc_game_ws_mb_mean"] = round(sum(gws) / len(gws), 1)
    gcpu = col("game_cpu_pct")
    if gcpu:
        out["pc_game_cpu_pct_mean"] = round(sum(gcpu) / len(gcpu), 2)
        out["pc_game_cpu_pct_min"] = min(gcpu)
    prios = []
    for r in rows:
        v = (r.get("game_prio") or "").strip()
        if v and v not in prios:
            prios.append(v)
    if prios:
        # Report the *most throttled* class seen, not the first or the most common.
        order = ["Idle", "BelowNormal", "Normal", "AboveNormal", "High", "RealTime"]
        out["pc_game_prio"] = min(prios, key=lambda p: order.index(p) if p in order else len(order))
    # The other side of "the game is waiting on something": the VR runtime and the Virtual Desktop
    # stack, which a game blocked in frame submission would be handing its work to. min matters as much
    # as max here -- a runtime that STOPS consuming CPU during the stall is as telling as one that
    # spikes, and either points somewhere different from a stalled game.
    for src, key in (("streamer_cpu_pct", "streamer"), ("vr_cpu_pct", "vr")):
        v = col(src)
        if v:
            out[f"pc_{key}_cpu_pct_mean"] = round(sum(v) / len(v), 2)
            out[f"pc_{key}_cpu_pct_max"] = round(max(v), 2)
            out[f"pc_{key}_cpu_pct_min"] = round(min(v), 2)
    vw = col("vr_ws_mb")
    if vw:
        out["pc_vr_ws_mb_mean"] = round(sum(vw) / len(vw), 1)
    # Thread states: the per-sample histogram of what the game's threads are waiting on. Reported from
    # the sample where the game had the LEAST CPU -- i.e. the stall -- because a run-level average would
    # just be dominated by the idle reason every process shows in bulk, and the histogram is only
    # interesting at the moment the game stops being scheduled.
    pairs = []
    for r in rows:
        try:
            cpu_v = float(r.get("game_cpu_pct", ""))
        except (TypeError, ValueError):
            continue
        hist = (r.get("game_thr_wait") or "").strip()
        if hist:
            pairs.append((cpu_v, hist, (r.get("top_procs") or "").strip()))
    if pairs:
        worst = min(pairs, key=lambda p: p[0])
        out["pc_game_thr_wait_at_min_cpu"] = worst[1]
        # Same moment: who else was consuming CPU while the game had the least. This is the check on
        # "something else stole the machine" that the counter columns can otherwise only answer for
        # the game and the VR stack.
        if worst[2]:
            out["pc_top_procs_at_min_game_cpu"] = worst[2]
    gth = col("game_threads")
    if gth:
        out["pc_game_threads_mean"] = round(sum(gth) / len(gth), 1)
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
    """Reduce a PresentMon capture into the PC game's own present-rate stats -- the one layer every
    other sampler here is blind to (the headset/OVR telemetry only ever sees the HEADSET compositor's
    frame rate). Column names vary across PresentMon versions, so this
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
           "GPU%", "CPU%", "CFL", "ICFLp95", "LD", "SF", "Temp", "PoseAgeP95", "Preempt",
           # Fields the reducer previously discarded. DpuScale/DVFS are the runtime's own quality
           # scaling state (a resolution/clock scale the runtime applies before it asks the host for a
           # different bitrate, so it is the earliest per-second sign the stream is being turned down);
           # Mem/Free are the memory controller's clock and the device's free memory; PLS/LP/CABC are
           # further compositor flags. Every one of them lands in vr_api_samples.csv as a parsed number,
           # and a summary key is emitted only for the fields with an unambiguous reading (see
           # vr_api_reduce) -- the rest stay in the per-second series for correlation.
           "DpuScale", "DVFS", "Mem", "Free", "PLS", "LP", "CABC", "Fov", "LCnt"]
# LCnt is the one field whose value contains a comma ("LCnt=2(DR109,LM2)"), so the generic
# "(?:^|,)\s*KEY=([^,]*)" scan above truncates it at the comma -- it is parsed by its own regex.
LCNT_RE = re.compile(r"LCnt=\d+\(DR(\d+),LM(\d+)\)")


def _num(text):
    """First number in a VrApi field value, with its unit stripped: '36ms' -> 36.0, '-1' -> -1.0,
    '1.00' -> 1.0. None when there is no number at all."""
    if text is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(text))
    return float(m.group(0)) if m else None


def _cum_delta(vals):
    """Change in a cumulative counter across a window: last minus first when the series never goes
    backwards, otherwise the sum of its positive steps (a counter that resets mid-window -- the
    decoder or the panel object was recreated -- must not report a negative or an absurd total)."""
    vs = [v for v in vals if v is not None]
    if len(vs) < 2:
        return None
    if all(b >= a for a, b in zip(vs, vs[1:])):
        return vs[-1] - vs[0]
    return sum(b - a for a, b in zip(vs, vs[1:]) if b > a)


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


RETRY_BURST_PCT = 10.0      # per-interval retry rate that counts as a burst


def wifi_retry_bursts(path, out_tsv=None):
    """Per-interval Wi-Fi retry behaviour, instead of one run-long average.

    results already carries `retry_rate_pct`, but that is the whole run averaged, and in the
    2026-09-19 runs it came out at ~2 % -- which is why "retries are only ~2 %, so the link isn't the
    bottleneck" survived as a conclusion for so long. Sample by sample the same runs peak at 76 % for
    about 15 % of their duration, and it is precisely those bursts that stall the entire pipeline
    (NVENC to 0 %, TCP send halved, the headset's decoders starving at 0.2 frames/s) while RSSI and
    link rate stay perfect. An average cannot express that, so this reports the distribution and the
    bursts explicitly, and writes the per-interval series next to the run for correlating against
    stalls.

    rssi/link are pinned to the burst intervals deliberately: they are the evidence that what is
    happening is airtime/hand-off behaviour, not a signal problem."""
    rows = read_tsv(path) if os.path.exists(path) else []
    if len(rows) < 2:
        return {}

    def tsec(ts):
        hh, mm, ss = ts[11:19].split(":")
        return int(hh) * 3600 + int(mm) * 60 + int(ss)
    ser = []
    resets = 0
    for a, b in zip(rows, rows[1:]):
        try:
            dt_s = tsec(b["timestamp"]) - tsec(a["timestamp"])
            dr = int(b["tx_retries"]) - int(a["tx_retries"])
            ds = int(b["tx_success"]) - int(a["tx_success"])
            dl = int(b["tx_lost"]) - int(a["tx_lost"])
        except (KeyError, TypeError, ValueError):
            continue
        if dt_s <= 0:
            continue
        if dr < 0 or ds < 0 or dl < 0:
            # Counters went backwards, so the interface restarted between the two samples
            # (re-association, Wi-Fi re-up, driver reload). The deltas across that pair are
            # meaningless and the rate built from them is a huge bogus burst -- observed for real
            # while sampling a headset whose Bluetooth was being toggled. Drop the interval and say
            # it happened rather than let it dominate p90/max.
            resets += 1
            continue
        ser.append({"clock": b["timestamp"][11:19],
                    "rate_pct": round(100.0 * dr / max(dr + ds, 1), 2),
                    "retries_per_s": round(dr / float(dt_s), 1), "lost": dl,
                    "rssi": int(b.get("rssi") or 0), "link_mbps": int(b.get("link_mbps") or 0)})
    if not ser:
        return {}
    if out_tsv:
        with open(out_tsv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["clock", "rate_pct", "retries_per_s", "lost", "rssi", "link_mbps"])
            for r in ser:
                w.writerow([r["clock"], r["rate_pct"], r["retries_per_s"], r["lost"],
                            r["rssi"], r["link_mbps"]])
    rates = sorted(r["rate_pct"] for r in ser)
    bursts = [r for r in ser if r["rate_pct"] > RETRY_BURST_PCT]
    out = {
        "retry_rate_p90_pct": rates[min(int(len(rates) * 0.9), len(rates) - 1)],
        "retry_rate_max_pct": max(rates),
        "retry_bursts": len(bursts),
        "retry_bursts_pct": round(100.0 * len(bursts) / len(ser), 1),
        "retry_lost_packets": sum(r["lost"] for r in ser),
    }
    if bursts:
        out["retry_burst_lost_packets"] = sum(r["lost"] for r in bursts)
        out["retry_burst_rssi_dbm_min"] = min(r["rssi"] for r in bursts)
        out["retry_burst_link_mbps_min"] = min(r["link_mbps"] for r in bursts)
    if resets:
        # Surfaced deliberately: a reset also corrupts the run-mean retry rate and the
        # fingerprint's baseline for this run, so a reader needs to know it happened.
        out["retry_counter_resets"] = resets
    return out


# Decoder instance names carry the stream's identity in their prefix: `avcDLowLat_38` (observed live
# 2026-09-22 on a VD H.264 stream) is an H.264 low-latency decoder, instance 38. The family table maps
# the names MediaCodec itself uses, so a HEVC/AV1/VP9 instance is recognised the same way; a prefix that
# matches nothing stays reported raw rather than being forced into a family it may not be. A "10" marker
# surrounded by non-digits sets the bit depth to 10 -- absent, the depth is reported as unknown instead
# of assumed to be 8, since only the H.264 case is inferable from VD's own codec enum.
_DECODER_FAMILIES = (("av1", "AV1"), ("avc", "H.264"), ("h264", "H.264"), ("hevc", "HEVC"),
                     ("h265", "HEVC"), ("vp9", "VP9"), ("vp8", "VP8"))
# Configured codec names (VD's PreferredCodec display strings, see tools/vd-codec-enum.md), normalised
# to letters/digits, mapped to the family the decoder should then be reporting. Anything absent from
# this table -- "Automatic", the wizard's "live" placeholder, a future codec -- means "no expectation",
# so no mismatch is reported.
_CONFIGURED_FAMILIES = {"h264": "H.264", "h264plus": "H.264", "hevc": "HEVC", "hevc10bit": "HEVC",
                        "av1": "AV1", "av110bit": "AV1", "vp8": "VP8", "vp9": "VP9"}


def _clock_s(stamp):
    hh, mm_, ss = stamp.split(":")
    return int(hh) * 3600 + int(mm_) * 60 + float(ss)


def codec_stream_reduce(path, out_tsv=None, since_dev_s=None, until_dev_s=None, ref_epoch=None,
                        configured_codec=None):
    """Decode-side stream stats, straight from the headset's hardware codec process.

    `logcat -s <HEADSET_LOG_TAGS>` (see monitor()'s logcat launch) carries one statistics line roughly
    every 5 seconds per decoder instance out of `mediacodec` / media.hwcodec (the compositor's VrApi
    line, by contrast, is once a second), e.g.

      I QC2Comp : [avcDLowLat_57] Stats: Pending(0) i/p-done(0) Works: Q: 25235/Done 25236|
                  Work-Rate: Q(60.0/s ...) Done(59.994/s ...)| Stream: 60.11fps 7.4Mbps

    Why this is worth having: every other headset-side number we collect (VrApi's Stale/FPS/TW/App/CFL)
    comes from the COMPOSITOR, and the compositor keeps reporting a healthy 90 fps with zero stale
    frames even while the operator is describing serious stutter -- it is fed by Virtual Desktop, not by
    the wire, so it cannot see a frame that never arrived. The decoder's own output rate, and the gap
    between what it has been handed and what it has finished, are the closest thing to "did the video
    actually arrive", measured on the headset with no ETW involved -- so it stays honest precisely in
    runs where PresentMon or a trace would be suspected of causing the problem they measure.

    since_dev_s/until_dev_s trim to the VR session. The logcat main buffer holds ~5 minutes, so the
    capture starts with the *previous* session's backlog: without a window this reducer read seven
    decoder instances for a single 214s run, spanning three different VD sessions, and then reported
    whichever had the highest mean bitrate -- i.e. numbers for a stream that had already ended. The
    instance ids are the tell: they only ever increase, so a run whose decoders step 60 -> 61 -> 62 ->
    66 is being read across session boundaries.

    The instance name also carries the stream's identity, which is the closest thing to "which codec was
    really selected" that exists on the client: `[avcDLowLat_38]` is an H.264 low-latency decoder,
    instance 38 (observed live 2026-09-22 on a VD H.264 stream at 120 fps / 261.7 Mbps). That is
    compared against what the run was configured for (`settings.json`'s codec, i.e. VD's PreferredCodec
    display name -- see tools/vd-codec-enum.md), which is how a silent codec fallback becomes visible:
    a stream configured HEVC that decodes H.264 shows up as codec_stream_codec_mismatch with a note,
    instead of only in the operator noticing the picture looks soft. A decoder instance whose first
    sample lands well after the stream was already running is a decoder created mid-session, i.e. the
    stream was re-established (a reconnect/re-negotiation, or the desktop view opening on top of the VR
    stream) -- reported as codec_stream_instances_late rather than folded into the averages."""
    if not os.path.exists(path):
        return {}
    row_re = re.compile(
        r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d\d\d).*?QC2Comp[^:]*:\s*\[([^\]]+)\].*?"
        r"Q:\s*(\d+)/Done\s*(\d+).*?Done\(([\d.]+)/s.*?Stream:\s*([\d.]+)fps\s+([\d.]+)Mbps")
    rows = []
    for line in _read_text(path).splitlines():
        m = row_re.search(line)      # search, not match: the line has a pid/tid prefix before the tag
        if not m:
            continue
        if (since_dev_s or until_dev_s) and ref_epoch:
            t = _row_dev_epoch(m.group(1), ref_epoch)
            if t is not None and ((since_dev_s and t < since_dev_s) or
                                  (until_dev_s and t > until_dev_s)):
                continue
        q, done = int(m.group(3)), int(m.group(4))
        rows.append({"clock": m.group(1), "instance": m.group(2), "q": q, "done": done,
                     "lag": q - done, "done_per_s": float(m.group(5)),
                     "stream_fps": float(m.group(6)), "stream_mbps": float(m.group(7))})
    if not rows:
        return {}
    if out_tsv:
        with open(out_tsv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["clock", "instance", "q", "done", "lag", "done_per_s",
                        "stream_fps", "stream_mbps"])
            for r in rows:
                w.writerow([r["clock"], r["instance"], r["q"], r["done"], r["lag"],
                            r["done_per_s"], r["stream_fps"], r["stream_mbps"]])
    # Group by decoder instance. The headset runs two at once -- the ~60fps desktop stream and the
    # 90fps VR stream -- and pooling them made the whole summary meaningless: the 60fps/8Mbps desktop
    # instance dragged the VR stream's mean to 91.53fps/127.5Mbps and got reported as *the* instance,
    # so "decode-side starvation" could not be read off it at all. The VR stream carries what the
    # operator actually experiences, so the scalar keys describe it. Pick by sample count first, with
    # mean bitrate only as a tie-break: this run also had a 9-sample instance at 259Mbps sitting next
    # to the real 32-sample stream at 212Mbps, and ranking by bitrate alone headlined the transient
    # one. Few-sample instances are exactly the short-lived ones (session start, re-negotiation,
    # teardown) that do not represent the run.
    groups = {}
    for r in rows:
        groups.setdefault(r["instance"], []).append(r)
    means = {i: sum(x["stream_mbps"] for x in g) / len(g) for i, g in groups.items()}
    vr = max(groups, key=lambda i: (len(groups[i]), means[i]))
    sel = groups[vr]
    fps = [r["stream_fps"] for r in sel]
    mbps = [r["stream_mbps"] for r in sel]

    # Weight each low sample by the interval it covers. The decoder reports every ~5s, so counting
    # samples understates starvation in wall-clock terms: a run whose decoder reported 10fps on two
    # samples was starved for ~10s, not 2 (confirmed against a PC-side stall of the same length). The
    # sample describes the period since the previous report, so that interval is what it accounts for.
    starved_s, prev_t = 0.0, None
    for r in sel:
        t = _clock_s(r["clock"][6:])          # "MM-DD HH:MM:SS.mmm" -> "HH:MM:SS.mmm"
        if prev_t is not None and r["stream_fps"] < 60:
            starved_s += t - prev_t
        prev_t = t

    # Decoder identity from the instance name (see the docstring), compared against what the run was
    # configured for. `name` keeps the raw prefix so an unrecognised build is still readable, rather
    # than being force-fitted into a codec family it may not be.
    token = vr.split("_")[0]
    family = next((fam for tok, fam in _DECODER_FAMILIES if token.lower().startswith(tok)), None)
    low_latency = "lowlat" in token.lower()
    depth = 10 if re.search(r"(?:^|[^0-9])10(?:bit)?", token.lower()) else None
    configured = (configured_codec or "").strip()
    want = _CONFIGURED_FAMILIES.get(re.sub(r"[^a-z0-9]", "", configured.lower())) if configured else None
    got = family or f"unknown({token})"
    mismatch = (want != family) if (want and family) else None
    note = None
    if mismatch:
        note = (f"configured '{configured}' but the decoder is running {family} -- the stream fell back "
                f"to a different codec than the one asked for")
    elif configured and family is None:
        note = (f"decoder instance '{token}' does not name a codec this reducer recognises, so it could "
                f"not be checked against the configured '{configured}'")
    window_start = min(_clock_s(r["clock"][6:]) for r in rows)
    late = [i for i, g in groups.items() if _clock_s(g[0]["clock"][6:]) - window_start > 30]

    out = {
        "codec_stream_instance": vr,
        "codec_stream_samples": len(sel),
        "codec_stream_fps_mean": round(sum(fps) / len(fps), 2),
        "codec_stream_fps_min": min(fps),
        # Seconds the decoder ran below 60fps, interval-weighted (see above). Kept as a duration rather
        # than a sample count because that is the number comparable to the PC-side stall it explains.
        "codec_stream_seconds_below_60fps": round(starved_s, 1),
        "codec_stream_mbps_mean": round(sum(mbps) / len(mbps), 2),
        "codec_stream_mbps_min": min(mbps),
        # A growing handed-in-minus-finished gap means the decoder is being fed faster than it can
        # finish -- the stream outran the headset rather than the reverse.
        "codec_stream_lag_max": max(r["lag"] for r in sel),
        "codec_stream_workrate_min": min(r["done_per_s"] for r in sel),
        # The remaining decoders in the same run, so their behaviour stays visible rather than silent.
        "codec_stream_other_instances": ",".join(
            f"{i}:{round(means[i], 1)}Mbps/{round(sum(x['stream_fps'] for x in g) / len(g), 1)}fps/"
            f"{len(g)}s"
            for i, g in sorted(groups.items()) if i != vr),
        "codec_stream_decoder": token,
        "codec_stream_codec": got,
        "codec_stream_low_latency": low_latency,
        "codec_stream_bit_depth": depth,
        "codec_stream_configured_codec": configured or None,
        "codec_stream_codec_mismatch": mismatch if (want or (configured and family is None)) else None,
        "codec_stream_instance_count": len(groups),
        "codec_stream_instances_late": ",".join(late),
    }
    if note:
        out["codec_stream_codec_note"] = note
    return out


def vr_api_reduce(path, out_csv=None, since_dev_s=None, ref_epoch=None):
    """logcat -s VrApi emits one line per second from the pid owning the VR session:
       FPS=90/90,...,Stale=0,Stale2/5/10/max=0/0/0/0,...,TW=1.77ms,App=1.46ms,...,CFL=12.54/16.83,
       ICFLp95=15.81,...,Temp=38.0C/0.0C,...,GPU%=0.31,PoseAgeP95=0.00
       plus a parallel ASW= line. This is the only frame telemetry that also covers Air Link.
       since_dev_s trims the pre-cell logcat backlog (the main buffer holds ~5 min).

    The line carries ~35 fields and this reducer used to summarise 12 of them. The rest are now parsed
    into vr_api_samples.csv (panel timing Tear/Early/Prd/VSnc, the runtime's quality scaling DpuScale
    and DVFS, memory clock/free memory Mem/Free, the compositor flags PLS/LP/CABC, GPU/CPU frame
    durations GD and CPU&GPU, and the cumulative counters Preempt and LCnt's DR/LM), and the subset
    with an unambiguous reading also gets a summary key: panel tearing/early frames, predicted display
    period, minimum DpuScale (1.00 = native scale, below it the runtime has already shrunk what it
    renders), dropped/late-motion frames, preemptions, minimum free memory, and the highest available
    latency. Observed live 2026-09-22 on a VD H.264 stream at 120 fps: 90 Hz-era builds and 120 Hz ones
    differ in which fields appear, so every new key is None rather than 0 when its field is absent --
    except the counters, which are 0 only when the samples say so."""
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
        lc = LCNT_RE.search(body)
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
            "prd_ms": _num(kv.get("Prd")),
            "tear": _num(kv.get("Tear")),
            "early": _num(kv.get("Early")),
            "vsnc": _num(kv.get("VSnc")),
            "lat_ms": _num(kv.get("Lat")),
            "dpu_scale": _num(kv.get("DpuScale")),
            "dvfs": _num(kv.get("DVFS")),
            "mem_mhz": _num(kv.get("Mem")),
            "free_mb": _num(kv.get("Free")),
            "pls": _num(kv.get("PLS")),
            "lp": _num(kv.get("LP")),
            "cabc": _num(kv.get("CABC")),
            "gd_ms": _num(kv.get("GD")),
            "cpu_gpu_ms": _num(kv.get("CPU&GPU")),
            "sf": _num(kv.get("SF")),
            "preempt": _num(kv.get("Preempt")),
            "dropped": float(lc.group(1)) if lc else None,
            "late_motion": float(lc.group(2)) if lc else None,
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

    def _series(key):
        return [r[key] for r in rows]

    def _sum(key):
        vs = [v for v in _series(key) if v is not None]
        return int(sum(vs)) if vs else None

    def _secs(key):
        return sum(1 for v in _series(key) if v)

    prd = [v for v in _series("prd_ms") if v is not None]
    dpu = [v for v in _series("dpu_scale") if v is not None]
    free = [v for v in _series("free_mb") if v is not None]
    # Lat is -1 when the runtime has no latency figure to report, so negative samples are excluded
    # rather than averaged in as if they were a real 1 ms below zero.
    lats = [v for v in _series("lat_ms") if v is not None and v >= 0]
    out = {
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
        # Panel timing. Tear/Early are per-second counts of frames the panel presented torn, or early
        # relative to its predicted period; they are 0 in a healthy run and neither is visible in the
        # Stale counters, which only count frames the compositor never replaced.
        "vr_api_tear_total": _sum("tear"), "vr_api_tear_seconds": _secs("tear"),
        "vr_api_early_total": _sum("early"), "vr_api_early_seconds": _secs("early"),
        "vr_api_prd_ms_mean": round(sum(prd) / len(prd), 2) if prd else None,
        "vr_api_prd_ms_max": max(prd) if prd else None,
        # Quality scaling: 1.00 = the runtime is presenting at native scale; below it, the runtime has
        # already started shrinking what it renders, before any bitrate change is visible anywhere else.
        "vr_api_dpu_scale_min": round(min(dpu), 3) if dpu else None,
        "vr_api_dpu_scale_below_native": sum(1 for v in dpu if v < 0.999) if dpu else None,
        # Cumulative counters across the window (see _cum_delta): DR/LM are the runtime's own dropped
        # and late-motion frame counters, Preempt its preemption count.
        "vr_api_dropped_frames_total": _cum_delta(_series("dropped")),
        "vr_api_late_motion_total": _cum_delta(_series("late_motion")),
        "vr_api_preempt_total": _cum_delta(_series("preempt")),
        "vr_api_free_mb_min": min(free) if free else None,
        "vr_api_lat_ms_max": round(max(lats), 2) if lats else None,
    }
    return out


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
    skip appending its row to the shared results.csv so it can never end up committed by forgetting a
    manual step. results.json still gets written inside the (gitignored) run directory, so
    fingerprint/dashboard/local inspection all still work; only the row in the shared CSV is skipped."""
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
    # Before anything else, and before adb: a run whose session was killed hard still gets its raw
    # artifacts scrubbed here, even when the headset is unreachable.
    redact_artifacts(run_dir)
    ser = adb_serial()
    overlay = {}
    if overlay_path and os.path.exists(overlay_path):
        overlay = json.load(open(overlay_path))

    # Direct call, not a sys.executable subprocess: the latter breaks under a frozen/bundled build the
    # same way dashboard.py's old subprocess launch did (see its make_server() docstring) -- there is
    # no plain python.exe to hand a sibling .py file to when sys.executable is the bundled exe itself.
    # Only reduce a capture when one actually exists. reduce_cell() is the phase-1 pcap path; a
    # `monitor` session (every wizard run) never writes one, and calling it unconditionally made the
    # end of every such run print `{"error": "no capture found in ..."}` -- which reads as "the run
    # failed", immediately before the real results line.
    has_capture = any(os.path.exists(os.path.join(run_dir, f)) for f in ("cap.pcapng", "cap.etl"))
    w = analyze.reduce_cell(os.path.join(BASE, "runs"), run_id, quest_ip=QUEST_IP, window="30,150") \
        if has_capture else {}

    # wifi counter deltas (MAC layer, headset TX direction)
    rows = read_tsv(os.path.join(run_dir, "quest_wifi_samples.tsv"))
    if len(rows) < 2:
        raise RuntimeError("no wifi samples yet in " + run_dir)

    def d(k):
        """Delta of a monotonic counter across the samples that actually carry one.

        This used to read rows[0]/rows[-1] and int() them unconditionally. When the headset is
        unreachable for part of a run -- wireless adb drops mid-session -- the sampler still writes a
        row per interval, just with every field empty, and that aborted the whole reduction with
        `ValueError: invalid literal for int() with base 10: ''` -- throwing away the PC-side results
        (PresentMon, pc_samples, VrApi) that had captured perfectly. Confirmed live 2026-09-19: a
        4.3 min run whose quest_wifi_samples.tsv held 139 timestamp-only rows. Taking the first/last
        *valid* sample also keeps the deltas right for a run that loses the device partway through."""
        vals = []
        for r in rows:
            try:
                vals.append(int(str(r.get(k, "")).strip()))
            except (TypeError, ValueError):
                continue
        return (vals[-1] - vals[0]) if len(vals) >= 2 else None

    tx = d("tx_success"); tr = d("tx_retries"); tl = d("tx_lost"); rx = d("rx_success")
    retry = round(tr / tx * 100, 3) if (tx and tr is not None) else None
    lost = round(tl / tx * 100, 3) if (tx and tl is not None) else None
    headset_data_missing = tx is None

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
    if headset_data_missing:
        # Every headset-side reduction below will come out null; say so in the artifact rather than
        # leaving a reader to wonder whether null means zero, not-measured, or a bug.
        res["headset_data_note"] = ("headset sampler got no usable samples this run (device "
                                    "unreachable or wireless adb dropped) -- headset-side metrics "
                                    "(retry/loss, RSSI, thermals, controller link) are null")
    res.update(ovr)
    res.update(ping_stats(os.path.join(run_dir, "ping_samples.txt")))
    logcat_path = os.path.join(run_dir, HEADSET_LOGCAT_NAME)
    settings = json.load(open(os.path.join(run_dir, "settings.json")))
    res.update(vr_api_reduce(logcat_path,
                             os.path.join(run_dir, "vr_api_samples.csv"),
                             since_dev_s=vr_start, ref_epoch=vr_start))
    res.update(codec_stream_reduce(logcat_path,
                                   os.path.join(run_dir, "codec_stream_samples.tsv"),
                                   since_dev_s=vr_start, until_dev_s=sess.get("end_dev_s"),
                                   ref_epoch=vr_start, configured_codec=settings.get("codec")))
    res.update(wifi_retry_bursts(os.path.join(run_dir, "quest_wifi_samples.tsv"),
                                 os.path.join(run_dir, "retry_rate_samples.tsv")))
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

    # Optional pre-session iperf3 link check (see linkcheck.py): carry the headline numbers into the
    # run's results.json, so a session records the capacity of the link it was measured on. Deliberately
    # not a MEAS_COLS entry -- this lands in results.json only, leaving the results.csv schema alone.
    lc_path = os.path.join(run_dir, "linkcheck.json")
    if os.path.exists(lc_path):
        try:
            lc = json.load(open(lc_path, encoding="utf-8"))
            res["linkcheck_tcp_down_mbps"] = lc.get("tcp_down_mbps")
            res["linkcheck_tcp_down_retransmits"] = lc.get("tcp_down_retransmits")
            res["linkcheck_tcp_up_mbps"] = lc.get("tcp_up_mbps")
            res["linkcheck_udp_last_zero_loss_mbps"] = lc.get("udp_last_zero_loss_mbps")
        except (OSError, ValueError):
            pass

    json.dump(res, open(os.path.join(run_dir, "results.json"), "w"), indent=1)

    s = settings
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
