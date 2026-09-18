#!/usr/bin/env python3
"""wizard.py - guided, menu-driven entry point for a single measurement session.

For a person sitting at the keyboard who wants to run a session without knowing the harness's
command sequence, the codec-before-bitrate rule, or how to recover a headset that lost its
wireless-adb pairing after a reboot:

    python tools/wizard.py

This is a friendlier front door onto the exact same harness the README's manual command sequence
documents (cell.py's monitor/stop/results/fingerprint, dashboard.py) -- not a separate code path.
Scripted/unattended use should keep going straight through cell.py; this is interactive only.

First choice is always Quick Test vs Advanced. Quick Test asks nothing at all -- just connects,
starts measuring, and reports what it finds. Advanced additionally asks for a target bitrate and
content type (motion/static), for testing a specific scenario -- everything else (stack, Wi-Fi band)
is detected automatically after the run from what was actually captured, same as Quick Test, since
asking a human something the harness already knows once the session has run is just friction; codec
stays "auto" in both modes since nothing here reads or controls the headset's actual codec setting.

Every run this creates is named with a `priv_` prefix by default -- kept off `results.csv` and out
of git entirely (see is_private_run() in cell.py / runs/priv_*/ in .gitignore). This is a general
diagnostic tool for anyone's own setup, not a data-collection pipeline; nothing here is ever
committed or published unless you explicitly decide to and do it yourself.

Either way: connect to the headset (bootstrapping wireless adb over USB if needed), check the
streaming stack is running, start `monitor` + the live dashboard, wait for you to press Enter when
you're done playing, then run `results` and `fingerprint`, print a plain-language verdict, and ask
whether to save this run as the new baseline.
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

import qsite
import cell
import dashboard

ADB = qsite.path("adb")
QUEST_IP = qsite.get("quest_ip")
BASE = qsite.base_dir()

STREAMER_PROCESS_NAMES = ("OVRServer_x64.exe", "VirtualDesktop.Streamer.exe", "VirtualDesktop.Server.exe")

# Every run this wizard creates is private by default -- see the module docstring.
PRIVATE_PREFIX = "priv_"

CONTENTS = ["motion", "static"]


def _print_header(title):
    print()
    print(f"== {title} ==")


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def ask_int(prompt, default):
    """For the two numeric prompts (bitrate, session length): plain ask() returns whatever was typed
    as a bare string, and both values get passed to int() downstream -- a mistyped non-numeric answer
    would otherwise raise an unhandled ValueError there, aborting the whole session setup after
    already going through headset connection + the streamer check. Re-prompts instead of silently
    falling back to the default the way choose() does for a bad menu pick: unlike a fixed choice,
    there's no way to guess what a mistyped number was supposed to mean, so asking again is kinder
    than guessing wrong. EOFError (genuinely no input left, e.g. non-interactive stdin) is left to
    propagate to main()'s top-level handler rather than looping forever against a closed stream."""
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"  '{raw}' isn't a number -- try again, or press Enter for the default ({default})")


def choose(prompt, options, default=None, allow_other=False):
    """Every choice this wizard actually asks is a fixed, exhaustive set (mode, content type) --
    there's no free-text value that means anything for either. allow_other=True is kept as an
    option, not removed, in case a future caller genuinely needs it, but defaults off: picking
    'other' and typing something for a choice with no meaningful custom value used to silently fall
    through to whichever branch the caller treats as the non-default case, which was a confusing
    dead end, not a real option."""
    default = default or options[0]
    print(prompt)
    for i, opt in enumerate(options, 1):
        marker = " (default)" if opt == default else ""
        print(f"  {i}. {opt}{marker}")
    n_opts = len(options) + (1 if allow_other else 0)
    if allow_other:
        print(f"  {len(options) + 1}. other (type your own)")
    raw = input(f"choice [1-{n_opts}, or Enter for default]: ").strip()
    if not raw:
        return default
    if raw.isdigit():
        n = int(raw)
        if 1 <= n <= len(options):
            return options[n - 1]
        if allow_other and n == len(options) + 1:
            return input("value: ").strip()
    if allow_other:
        return raw  # typed something that wasn't a menu number -- take it as a literal value
    print(f"  not a valid choice -- using default ({default})")
    return default


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

    ip = _discover_and_save_ip(serial) or QUEST_IP
    subprocess.run([ADB, "-s", serial, "tcpip", "5555"], capture_output=True, text=True)
    time.sleep(2)
    subprocess.run([ADB, "connect", f"{ip}:5555"], capture_output=True, text=True)
    print("Wireless adb re-paired. You can unplug the USB cable now.")
    return cell.adb_serial(refresh=True)


def _discover_and_save_ip(serial):
    """Read the headset's own current Wi-Fi IP straight off it over the USB connection we already
    have, instead of asking a human to find it in Settings and type it into site.json. This is what
    makes the one-time USB step in Setup double as the *entire* site.json bootstrap -- a first-ever
    run needs no manual config file at all, just this USB connection once. Persists to tools/site.json
    so future runs skip USB entirely (see the Wi-Fi-first path in ensure_headset_connected), and
    patches the already-imported cell/qsite modules in-process since their QUEST_IP was read once at
    import time and won't otherwise see a config file written after that."""
    out = subprocess.run([ADB, "-s", serial, "shell", "ip", "-f", "inet", "addr", "show", "wlan0"],
                         capture_output=True, text=True).stdout
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    if not m:
        print("  (couldn't read the headset's Wi-Fi IP automatically -- falling back to site.json's)")
        return None
    ip = m.group(1)

    site_path = qsite.CONFIG_PATH  # not TOOLS_DIR -- see qsite.PERSIST_DIR for why they can differ
    site = {}
    if os.path.exists(site_path):
        site = json.load(open(site_path))
    elif os.path.exists(qsite.EXAMPLE_PATH):
        site = json.load(open(qsite.EXAMPLE_PATH))
    if site.get("quest_ip") != ip:
        site["quest_ip"] = ip
        json.dump(site, open(site_path, "w"), indent=1)
        print(f"  discovered headset IP {ip} -- saved to {site_path}")

    global QUEST_IP
    QUEST_IP = ip
    cell.QUEST_IP = ip
    qsite._CONFIG = None  # force other qsite.get() calls in this process to pick up the new value too
    return ip


def _detect_stack_and_band(run_id):
    """Fill in stack/band after the fact instead of asking upfront -- used by summarize() for every
    run, Quick Test or Advanced alike. Both are already captured as a side effect of a normal run:
    session.json's segment `proc` (the same string monitor()'s own SESSION DETECTED check greps for)
    distinguishes VD from Air Link, and the last quest_wifi_samples.tsv row's freq_mhz gives the
    Wi-Fi channel actually in use -- so there's no need to ask a human something the harness already
    knows once the session has actually run."""
    run_dir = os.path.join(BASE, "runs", run_id)
    stack, band = "unknown", "unknown"

    sess_path = os.path.join(run_dir, "session.json")
    if os.path.exists(sess_path):
        segs = json.load(open(sess_path)).get("segments") or []
        proc = segs[0].get("proc", "") if segs else ""
        if "VirtualDesktop" in proc:
            stack = "vd"
        elif "xrstreamingclient" in proc:
            stack = "airlink"

    wifi_path = os.path.join(run_dir, "quest_wifi_samples.tsv")
    if os.path.exists(wifi_path):
        rows = cell.read_tsv(wifi_path)
        freq = int(float(rows[-1]["freq_mhz"])) if rows and rows[-1].get("freq_mhz") else None
        if freq:
            if 2400 <= freq <= 2483:
                band = "2g4"
            elif 5150 <= freq <= 5895:
                band = "5g"
            elif 5925 <= freq <= 7125:
                band = "6g"

    settings_path = os.path.join(run_dir, "settings.json")
    settings = json.load(open(settings_path))
    settings["stack"], settings["band"] = stack, band
    json.dump(settings, open(settings_path, "w"), indent=1)
    return stack, band


def _write_settings(run_id, settings):
    run_dir = os.path.join(BASE, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    json.dump(settings, open(os.path.join(run_dir, "settings.json"), "w"), indent=1)


def quick_session():
    """No questions about the session itself -- just measure whatever's actually happening right now
    and report it. Trades precision (a quick run's fingerprint baseline is scoped to
    'codec/bitrate: auto', separate from any baseline established via a specific Advanced
    configuration) for zero friction: the point is a fast, no-decisions health check, not a
    controlled comparison -- that's what Advanced is for."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"{PRIVATE_PREFIX}quick_{ts}"
    _write_settings(run_id, {"run_id": run_id, "stack": "auto", "codec": "auto",
                              "bitrate_mbps": "auto", "content": "auto", "band": "auto"})
    print("Quick Test: no setup questions -- just play normally, press Enter when you're done.")
    return run_id


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
    """Only bitrate and content type are asked. Stack and Wi-Fi band are auto-detected after the run
    (same as Quick Test -- see _detect_stack_and_band), and codec isn't asked at all: nothing here
    reads the headset's actual codec, so a typed answer couldn't be verified anyway and would just be
    trust-me metadata. Bitrate is the one thing worth asking, since it's the one setting a human is
    about to go set a specific number for in the headset and the fingerprint tag should reflect that
    target exactly (not a detected value that might not match if the headset's dynamic-bitrate is on)."""
    _print_header("Advanced: session configuration")
    print("Only bitrate and content type are asked -- stack and Wi-Fi band are detected automatically")
    print("from the run itself, same as Quick Test. Before starting, make sure you've already set a")
    print("FIXED codec and bitrate in the headset / streamer app (dynamic bitrate off) -- the number")
    print("you enter here should match what you set there.")
    bitrate = ask_int("Target bitrate (Mbps)", 500)
    content = choose("Content type:", CONTENTS, default="motion")

    ts = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"{PRIVATE_PREFIX}adv_{bitrate}_{content}_{ts}"
    settings = {"run_id": run_id, "stack": "auto", "codec": "auto", "bitrate_mbps": bitrate,
                "content": content, "band": "auto"}
    _write_settings(run_id, settings)
    return run_id


def ask_presentmon_hint():
    """Ask for a rough game name to feed PresentMon, if PresentMon is configured at all. Deliberately
    NOT a live match against currently-running processes: a VR title is almost always launched *after*
    VD/Air Link connects (sometimes minutes later, from inside the headset), so asking before the
    session even starts means the game usually isn't running yet to match against. This just collects
    the free-text guess; cell.monitor()'s presentmon_hint handling does the actual fuzzy-matching later,
    against a live process list, for a bounded window starting from when VD/Air Link is actually
    detected connected -- see PRESENTMON_HINT_WINDOW_S in cell.py. Blank skips PC game-fps capture for
    this run entirely, same as if PresentMon weren't installed."""
    exe = qsite.presentmon_exe()
    if not (exe and os.path.exists(exe)):
        return None
    _print_header("PC game frame-rate capture (optional)")
    print("If you'd like accurate PC-side fps for the game itself (not just the headset's own frame")
    print("rate), type its name below -- it'll be matched once you actually launch it, so it doesn't")
    print("need to be running yet.")
    return ask("Game name (blank to skip)", "") or None


def run_session(run_id, max_seconds, presentmon_hint=None):
    _print_header("Live session")
    monitor_thread = threading.Thread(target=cell.monitor, args=(run_id, max_seconds),
                                      kwargs={"presentmon_hint": presentmon_hint}, daemon=True)
    monitor_thread.start()
    time.sleep(1.5)  # let monitor's startup (clock sample, sampler spawn) happen before dashboard reads files

    port = _free_port(int(qsite.get("dashboard_port", 8765)))
    dash_srv = dashboard.make_server(run_id, port)
    dash_thread = threading.Thread(target=dash_srv.serve_forever, daemon=True)
    dash_thread.start()
    print(f"Live dashboard: http://127.0.0.1:{port}/")
    print(f"Play now. Session will stop automatically after {max_seconds // 60} min if you don't stop it first.")
    try:
        input("Press Enter when you're done playing to stop and reduce the session... ")
    except KeyboardInterrupt:
        print("\n(Ctrl+C) stopping...")
    except EOFError:
        # monitor_thread is daemon=True, so if this propagated as an uncaught exception instead
        # of being caught here, the process would die immediately and take the thread down with
        # it -- skipping cell.monitor()'s `finally` (sampler subprocesses terminated, session.json/
        # clock.json closed out) and leaving PresentMon/PowerShell samplers/ping orphaned. Confirmed
        # live: stdin closing unexpectedly here (not just Ctrl+C) is exactly that scenario.
        print("\n(stdin closed) stopping...")
    cell.stop_monitor(run_id)
    monitor_thread.join(timeout=30)
    if monitor_thread.is_alive():
        print("warning: monitor thread didn't exit within 30s of the stop request; artifacts may be incomplete")

    dash_srv.shutdown()
    dash_thread.join(timeout=10)


def summarize(run_id):
    _print_header("Results")
    stack, band = _detect_stack_and_band(run_id)
    print(f"Detected: stack={stack} band={band} (codec stays 'auto' -- not readable from the headset)")
    cell.results(run_id)
    res_path = os.path.join(BASE, "runs", run_id, "results.json")
    if os.path.exists(res_path):
        note = json.load(open(res_path)).get("pc_game_fps_note")
        if note:
            print(f"PC game-fps: {note}")
    diff = cell.fingerprint(run_id)
    if diff.get("baseline"):
        print("No prior baseline for this configuration -- this run is now the baseline for future "
              "sessions with the same stack/bitrate/band/content.")
        print(f"Full results: runs/{run_id}/results.json")
        return
    flags = diff.get("flags", [])
    if not flags:
        print("Verdict: consistent with the saved baseline -- no metric drifted beyond its threshold.")
    else:
        print(f"Verdict: {len(flags)} metric(s) drifted beyond the baseline:")
        for f in flags:
            print(f"  - {f['metric']}: {f['baseline']} -> {f['current']} (delta {f['delta']:+})")
    print(f"Full results: runs/{run_id}/results.json")

    ans = input("\nSave this run as the new baseline? [y/N]: ").strip().lower()
    if ans in ("y", "yes"):
        cell.fingerprint(run_id, save_baseline=True)
        print("Saved as the new baseline for this configuration.")


def main():
    print("Quest 3 PCVR diagnostics -- guided session")
    exit_code = 0
    try:
        mode = choose(
            "Mode:",
            ["Quick Test -- just run and measure, no setup questions",
             "Advanced -- configure a specific scenario to test"],
            default="Quick Test -- just run and measure, no setup questions")
        quick = mode.startswith("Quick")

        ensure_headset_connected()
        ensure_streamer_running()
        run_id = quick_session() if quick else configure_session()
        presentmon_hint = ask_presentmon_hint()
        max_minutes = ask_int("Max session length, in minutes (you can stop earlier)", 60)
        run_session(run_id, max_minutes * 60, presentmon_hint=presentmon_hint)
        summarize(run_id)
    except KeyboardInterrupt:
        print("\ncancelled")
        exit_code = 1
    except RuntimeError as e:
        print(f"\nerror: {e}")
        exit_code = 1
    except Exception as e:
        # Double-clicked (not launched from an already-open terminal), any unhandled exception --
        # including EOFError, e.g. from stdin not being interactive for whatever reason -- would
        # otherwise close this freshly-spawned console window the instant the process exits, with
        # nothing visible: looks exactly like a silent crash, indistinguishable from the process
        # never having started at all (confirmed live, 2026-09-18, chasing down what turned out to
        # be a different bug -- but this gap is real regardless of what actually causes a crash).
        print(f"\nunexpected error: {type(e).__name__}: {e}")
        exit_code = 1
    try:
        input("\nPress Enter to exit... ")
    except EOFError:
        pass  # no interactive input available (e.g. piped/scripted run) -- nothing to wait for
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
