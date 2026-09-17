#!/usr/bin/env python3
"""wizard.py - guided, menu-driven entry point for a single measurement session.

For a person sitting at the keyboard who wants to run a session without knowing the harness's
command sequence, the codec-before-bitrate rule, or how to recover a headset that lost its
wireless-adb pairing after a reboot:

    python tools/wizard.py

This is a friendlier front door onto the exact same harness the README's manual command sequence
documents (cell.py's monitor/stop/results/fingerprint, dashboard.py) -- not a separate code path.
Scripted/unattended use should keep going straight through cell.py; this is interactive only.

What it does, in order: connect to the headset (bootstrapping wireless adb over USB if needed),
check the streaming stack is running, ask for the session's stack/codec/bitrate/content/band
(pausing for you to actually set codec-then-bitrate in the headset, since nothing here can do that
for you), start `monitor` + the live dashboard, wait for you to press Enter when you're done
playing, then run `results` and `fingerprint` and print a plain-language verdict.
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time

import qsite
import cell

ADB = qsite.path("adb")
QUEST_IP = qsite.get("quest_ip")
BASE = qsite.base_dir()

STREAMER_PROCESS_NAMES = ("OVRServer_x64.exe", "VirtualDesktop.Streamer.exe", "VirtualDesktop.Server.exe")

# Known-good values seen across this rig's own runs (see settings.json across runs/*/) -- offered as
# a menu so a person doesn't have to remember exact capitalization/spelling for the fingerprint tag
# to line up with an existing baseline, but "other" always lets them type something new.
STACKS = ["vd", "airlink"]
CODECS = {"vd": ["H.264+", "H.264", "HEVC 10-bit", "AV1 10-bit"], "airlink": ["H.264"]}
BANDS = ["6g", "5g"]
CONTENTS = ["motion", "static"]


def _print_header(title):
    print()
    print(f"== {title} ==")


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def choose(prompt, options, default=None):
    default = default or options[0]
    print(prompt)
    for i, opt in enumerate(options, 1):
        marker = " (default)" if opt == default else ""
        print(f"  {i}. {opt}{marker}")
    print(f"  {len(options) + 1}. other (type your own)")
    raw = input(f"choice [1-{len(options) + 1}, or Enter for default]: ").strip()
    if not raw:
        return default
    if raw.isdigit():
        n = int(raw)
        if 1 <= n <= len(options):
            return options[n - 1]
        if n == len(options) + 1:
            return input("value: ").strip()
    return raw  # typed something that wasn't a menu number -- take it as a literal value


def _tasklist():
    return subprocess.run(["tasklist"], capture_output=True, text=True, encoding="utf-8", errors="ignore").stdout


def _adb_devices_raw():
    return subprocess.run([ADB, "devices"], capture_output=True, text=True).stdout


def ensure_headset_connected(usb_timeout_s=90):
    """cell.adb_serial() already tries a direct connect() to the last-known IP, then falls back to
    mDNS wireless-debugging discovery -- that covers a headset that's still on the same network with
    its pairing intact. Two escalating fallbacks below that, before asking for a cable:

    1. disconnect+connect. adb's own `connect` is a no-op against an endpoint it already considers
       attached-but-`offline` (confirmed live: the headset's wireless link idles into `offline` fairly
       often, and a bare `connect` did not clear it, but `disconnect` then `connect` did). This is by
       far the common case, so it's tried first and silently -- no need to bother a human for it.
    2. USB bootstrap. Only reached when adb has genuinely lost the pairing, e.g. right after the
       headset reboots (the tcpip:5555 listener dies with it) and mDNS discovery comes up empty too
       (seen in practice, 2026-09-17). This is the one case that actually needs a cable."""
    _print_header("Headset connection")
    try:
        ser = cell.adb_serial(refresh=True)
        print(f"already connected: {ser}")
        return ser
    except RuntimeError:
        pass

    subprocess.run([ADB, "disconnect", f"{QUEST_IP}:5555"], capture_output=True, text=True)
    try:
        ser = cell.adb_serial(refresh=True)
        print(f"reconnected: {ser}")
        return ser
    except RuntimeError:
        pass

    print("Headset not reachable over Wi-Fi.")
    print("Plug it into this PC with a USB-C cable now (put the headset on if it's asleep).")
    input("Press Enter once it's plugged in... ")
    print("Waiting for it to show up over USB (accept any 'Allow USB debugging?' prompt in the headset)...")
    deadline = time.time() + usb_timeout_s
    serial = None
    warned_unauthorized = False
    while time.time() < deadline:
        out = _adb_devices_raw()
        m = re.search(r"^(\S+)\t device$", out, re.M)
        if m:
            serial = m.group(1)
            break
        if "unauthorized" in out and not warned_unauthorized:
            print("  seen, but waiting on the on-headset authorization prompt...")
            warned_unauthorized = True
        time.sleep(2)
    if not serial:
        raise RuntimeError(
            "no USB device showed up in time. Check: headset powered on and worn/visible, cable is "
            "data-capable (not charge-only), and Settings > System > Developer > USB Connection Dialog "
            "+ Wireless Debugging are both on (see README Setup).")

    subprocess.run([ADB, "-s", serial, "tcpip", "5555"], capture_output=True, text=True)
    time.sleep(2)
    subprocess.run([ADB, "connect", f"{QUEST_IP}:5555"], capture_output=True, text=True)
    print("Wireless adb re-paired. You can unplug the USB cable now.")
    return cell.adb_serial(refresh=True)


def ensure_streamer_running():
    _print_header("Streaming stack")
    if any(name in _tasklist() for name in STREAMER_PROCESS_NAMES):
        print("Virtual Desktop / OVR server is running.")
        return
    print("Virtual Desktop / OVR server doesn't look like it's running.")
    switcher = qsite.get("vd_switcher_exe")
    streamer = qsite.get("vd_streamer_exe")
    launch_exe = switcher if switcher and os.path.exists(switcher) else \
        (streamer if streamer and os.path.exists(streamer) else None)
    if launch_exe:
        ans = input(f"Launch it now ({launch_exe})? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            subprocess.Popen([launch_exe])
            print("Launched. Give it a few seconds to come up.")
    input("Once your streaming stack is running and ready, press Enter to continue... ")


def _free_port(preferred):
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"no free port found near {preferred}")


def configure_session():
    _print_header("Session configuration")
    stack = choose("Streaming stack:", STACKS, default="vd")
    codec = choose("Codec (must match what you're about to set in the headset):",
                    CODECS.get(stack, CODECS["vd"]), default=CODECS.get(stack, CODECS["vd"])[0])
    bitrate = ask("Bitrate (Mbps)", default="500")
    band = choose("Wi-Fi band:", BANDS, default="6g")
    content = choose("Content type:", CONTENTS, default="motion")
    max_minutes = ask("Safety cap on session length, in minutes (you can stop earlier)", default="60")

    print()
    print(f"IMPORTANT: in the headset / streamer app, set codec to '{codec}' FIRST, then set bitrate")
    print(f"to {bitrate} Mbps. Setting bitrate before codec causes VD to silently re-map it (see")
    print("findings.md) -- this order matters, not just the end values.")
    input("Press Enter once codec and bitrate are set in the headset... ")

    ts = time.strftime("%Y%m%d-%H%M%S")
    codec_slug = re.sub(r"[^A-Za-z0-9.+-]", "", codec)
    run_id = f"{stack}_{band}_{codec_slug}_{bitrate}_{content}_{ts}"

    settings = {"run_id": run_id, "stack": stack, "codec": codec, "bitrate_mbps": int(bitrate),
                "content": content, "band": band}
    run_dir = os.path.join(BASE, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    import json
    json.dump(settings, open(os.path.join(run_dir, "settings.json"), "w"), indent=1)
    return run_id, int(max_minutes) * 60


def run_session(run_id, max_seconds):
    _print_header("Live session")
    monitor_thread = threading.Thread(target=cell.monitor, args=(run_id, max_seconds), daemon=True)
    monitor_thread.start()
    time.sleep(1.5)  # let monitor's startup (clock sample, sampler spawn) happen before dashboard reads files

    port = _free_port(int(qsite.get("dashboard_port", 8765)))
    dash_env = dict(os.environ)
    dashboard_proc = subprocess.Popen(
        [sys.executable, os.path.join(qsite.TOOLS_DIR, "dashboard.py"), run_id, "--port", str(port)],
        cwd=qsite.TOOLS_DIR, env=dash_env)
    print(f"Live dashboard: http://127.0.0.1:{port}/")
    print(f"Play now. Session will stop automatically after {max_seconds // 60} min if you don't stop it first.")
    try:
        input("Press Enter when you're done playing to stop and reduce the session... ")
    except KeyboardInterrupt:
        print("\n(Ctrl+C) stopping...")
    cell.stop_monitor(run_id)
    monitor_thread.join(timeout=30)
    if monitor_thread.is_alive():
        print("warning: monitor thread didn't exit within 30s of the stop request; artifacts may be incomplete")

    dashboard_proc.terminate()
    try:
        dashboard_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/PID", str(dashboard_proc.pid), "/T", "/F"],
                        capture_output=True)


def summarize(run_id):
    _print_header("Results")
    cell.results(run_id)
    diff = cell.fingerprint(run_id)
    if diff.get("baseline"):
        print(f"No prior baseline for this configuration -- this run is now the baseline for future "
              f"sessions with the same stack/codec/bitrate/band.")
        return
    flags = diff.get("flags", [])
    if not flags:
        print("Verdict: consistent with the saved baseline -- no metric drifted beyond its threshold.")
    else:
        print(f"Verdict: {len(flags)} metric(s) drifted beyond the baseline:")
        for f in flags:
            print(f"  - {f['metric']}: {f['baseline']} -> {f['current']} (delta {f['delta']:+})")
    print(f"Full results: runs/{run_id}/results.json")


def main():
    print("Quest 3 PCVR diagnostics -- guided session")
    try:
        ensure_headset_connected()
        ensure_streamer_running()
        run_id, max_seconds = configure_session()
        run_session(run_id, max_seconds)
        summarize(run_id)
    except KeyboardInterrupt:
        print("\ncancelled")
        sys.exit(1)
    except RuntimeError as e:
        print(f"\nerror: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
