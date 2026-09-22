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
import shutil
import socket
import subprocess
import sys
import threading
import time

import qsite
import cell
import linkcheck
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
    suffix = f" [{default}]" if default else ""
    print()
    val = input(f"{prompt}{suffix}: ").strip()
    print()
    return val or default


def ask_yes_no(prompt, default="n"):
    """Yes/no prompt with the default spelled out, and blank lines above and below so the question
    reads as its own step instead of crowding the text that explains it.

    EOFError is deliberately left to the caller: each site has its own idea of what closed stdin
    means (skip the optional step, or propagate to main()'s handler)."""
    print()
    ans = input(f"{prompt} [y/n, default {default}]: ").strip().lower()
    print()
    if not ans:
        return default == "y"
    return ans in ("y", "yes")


def _pause(text):
    """Enter-to-continue, spaced like ask_yes_no -- these are decision points too, and the text
    around them should not run straight into the prompt."""
    print()
    input(text)
    print()


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
        print()
        raw = input(f"{prompt} [{default}]: ").strip()
        print()
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
    print()
    print(prompt)
    for i, opt in enumerate(options, 1):
        marker = " (default)" if opt == default else ""
        print(f"  {i}. {opt}{marker}")
    n_opts = len(options) + (1 if allow_other else 0)
    if allow_other:
        print(f"  {len(options) + 1}. other (type your own)")
    raw = input(f"choice [1-{n_opts}, or Enter for default]: ").strip()
    print()
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
    _pause("Press Enter once it's plugged in... ")
    print("Waiting for it to show up over USB (accept any 'Allow USB debugging?' prompt in the headset)...")
    deadline = time.time() + usb_timeout_s
    serial = None
    warned_unauthorized = False
    while time.time() < deadline:
        out = _adb_devices_raw()
        m = re.search(r"^(\S+)[ \t]+device$", out, re.M)
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
    distinguishes VD from Air Link, and the headset's own STA frequency gives the Wi-Fi band actually
    in use (cell.detect_band, which falls back to asking the headset live when the run's own samples
    are missing) -- so there's no need to ask a human something the harness already knows once the
    session has actually run."""
    run_dir = os.path.join(BASE, "runs", run_id)
    stack = "unknown"

    sess_path = os.path.join(run_dir, "session.json")
    if os.path.exists(sess_path):
        segs = json.load(open(sess_path)).get("segments") or []
        proc = segs[0].get("proc", "") if segs else ""
        if "VirtualDesktop" in proc:
            stack = "vd"
        elif "xrstreamingclient" in proc:
            stack = "airlink"

    band = cell.detect_band(run_dir) or "unknown"

    settings_path = os.path.join(run_dir, "settings.json")
    settings = json.load(open(settings_path))
    settings["stack"], settings["band"] = stack, band
    # Bluetooth state is captured when the run is configured, but a headset that wasn't reachable yet
    # leaves it unset; fill it in from the live device rather than leaving a run unlabelled.
    settings["bt"] = settings.get("bt") or cell.detect_bluetooth() or "unknown"
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
                              "bitrate_mbps": "auto", "content": "auto", "band": "auto",
                              "bt": cell.detect_bluetooth() or "unknown"})
    _print_header("Quick Test")
    print("No setup questions -- just play normally, press Enter when you're done.")
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
        if ask_yes_no(f"Launch it now ({launch_exe})?", default="y"):
            subprocess.Popen([launch_exe])
            print("Launched. Give it a few seconds to come up.")
    _pause("Once your streaming stack is running and ready, press Enter to continue... ")


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
                "content": content, "band": "auto",
                "bt": cell.detect_bluetooth() or "unknown"}
    _write_settings(run_id, settings)
    return run_id


def ask_presentmon_hint(run_id):
    """Ask for a rough game name to feed PresentMon, if PresentMon is configured at all. Deliberately
    NOT a live match against currently-running processes: a VR title is almost always launched *after*
    VD/Air Link connects (sometimes minutes later, from inside the headset), so asking before the
    session even starts means the game usually isn't running yet to match against. This just collects
    the free-text guess; cell.monitor()'s presentmon_hint handling does the actual fuzzy-matching later,
    against a live process list, for a bounded window starting from when VD/Air Link is actually
    detected connected -- see PRESENTMON_HINT_WINDOW_S in cell.py. Blank skips PC game-fps capture for
    this run entirely, same as if PresentMon weren't installed. That last promise is the fix's whole
    point: blank used to return None, which monitor() read as "capture system-wide", so a run intended
    to have no PC-side capture still had PresentMon's ETW session attached to the game. 'all' is now
    how you ask for a system-wide capture on purpose.

    Also the point where the operator is told where PresentMon's CSV lands and how fast it grows --
    it is the one artifact here whose size tracks something (the game's frame rate) rather than the
    session clock, so it is the one worth warning about before the session starts rather than after.
    """
    exe = qsite.presentmon_exe()
    if not (exe and os.path.exists(exe)):
        return None
    _print_header("PC game frame-rate capture (optional)")
    rate = cell.DATA_RATES_MB_PER_MIN["presentmon"]
    print(f"PresentMon found: {exe}")
    print("If you'd like accurate PC-side fps for the game itself (not just the headset's own frame")
    print("rate), type its name below -- it'll be matched once you actually launch it, so it doesn't")
    print("need to be running yet.")
    print("Type 'all' to capture every presenting process, or leave it blank to skip PC-side capture")
    print("entirely (nothing then attaches to the game).")
    # Kept immediately above the prompt, like the trace's size warning: this is cost-of-the-decision
    # information (where it writes, and how fast it grows), not background.
    print(f"Capture is written to {os.path.join(BASE, 'runs', run_id, 'presentmon.csv')}, about")
    print(f"{rate:.1f} MB per minute of play -- it scales with the game's frame rate.")
    raw = ask("Game name ('all' = every process, blank = skip)", "").strip()
    if not raw:
        return None                      # None -> monitor(presentmon_capture=False)
    return "" if raw.lower() == "all" else raw   # "" -> capture, system-wide (no hint to match)


def _run_target_bitrate(run_id):
    """The run's configured bitrate, or None for a Quick Test ("auto"). Used to aim the UDP half of
    the link check at the rate this session will actually ask the link to carry."""
    try:
        bitrate = json.load(open(os.path.join(BASE, "runs", run_id, "settings.json"))).get("bitrate_mbps")
    except (OSError, ValueError):
        return None
    return bitrate if isinstance(bitrate, int) else None


def ask_and_run_linkcheck(run_id, serial):
    """Offer the optional pre-session iperf3 link check -- see linkcheck.py for what it answers and
    why it has to run before the stream does.

    Only offered when BOTH halves of iperf3 are present, because it cannot run with one: the PC client
    and the aarch64 build the headset runs as the server. When only the PC client is there that is
    worth saying out loud -- a winget/scoop install gets you that far and the missing piece is the one
    nobody expects -- while when neither is present this stays quiet: the README and the release's
    vendor/iperf3_here.txt are where "you could also measure the link" belongs, not a line printed on
    every single run.

    A failure here never blocks the session: any LinkCheckError is reported and swallowed, because a
    link check that could not run says nothing about whether the session can.
    """
    exe, headset_bin = qsite.iperf3_exe(), qsite.iperf3_android()
    if not exe and not headset_bin:
        return None
    if not (exe and headset_bin):
        missing = ("no aarch64 iperf3 in vendor/" if exe
                   else "no iperf3 client (install iperf3, or drop vendor/iperf3.exe)")
        print(f"(link check skipped: {missing})")
        return None

    _print_header("Link capacity check (optional)")
    print("Measures the raw PC <-> headset link with no video running, so a session that reads badly")
    print("can be told apart from a radio that cannot carry the bitrate. Takes about a minute, and it")
    print("loads the same link -- so run it now, before playing, not during.")
    print("Nothing should be streaming right now (no game running in the headset).")
    try:
        go = ask_yes_no("Run the link check now?")
    except EOFError:
        return None
    if not go:
        return None

    # Aim the UDP half at this session's own bitrate when there is one -- that is the question worth
    # answering -- plus one step above it for headroom; a Quick Test has no target, so use the ramp the
    # original characterisation used.
    target = _run_target_bitrate(run_id)
    rates = sorted({target, int(target * 1.5)}) if target else [200, 500, 1000]
    host = serial.split(":")[0] if serial and ":" in serial else (qsite.get("quest_ip") or "")
    try:
        res = linkcheck.run_linkcheck(run_id, host, serial=serial, pc_exe=exe, headset_bin=headset_bin,
                                      udp_mbps=rates, progress=print)
    except linkcheck.LinkCheckError as e:
        print(f"  link check failed: {e}")
        print("  (continuing without it -- the session itself is unaffected)")
        return None
    linkcheck.print_summary(res)
    print(f"  Raw results: runs/{run_id}/linkcheck.json")
    return res


def start_trace(run_id, max_seconds):
    """Offer to record a Windows Performance Recorder trace for this session, and start it.

    This exists because the ~110 ms in-frame stalls are invisible to every sampler in runs/. PresentMon
    says the game's frame took ~110 ms from CPU-start to present, with the GPU idle the whole time and
    the Present call returning in 0.03 ms -- wall time that is neither CPU work nor the present path --
    but nothing here says *who* took it. A WPR trace (CPU sampled stacks + DiskIO + Microsoft's
    Audio-glitch provider) is what names a culprit, and wpr requires Administrator, so this hands off
    to Trace-Session.ps1 through one UAC prompt.

    Returns True whenever the operator asked for a trace -- including when the handshake below never
    completed -- so the caller always arranges the stop. The first version returned False on timeout,
    which meant a helper that came up after the 60 s wait was never told to stop and recorded until
    its own ceiling; confirmed live 2026-09-19, where the trace did start and the operator reasonably
    concluded it hadn't, because an elevated window with no output looks exactly like a dead one.

    The size warning is printed *before* the prompt, not after it: that warning is the whole input to
    the decision (GB per minute, and a perturbed session), so printing it afterwards tells the
    operator something they can no longer act on."""
    gb_min = cell.DATA_RATES_MB_PER_MIN["trace"] / 1024
    print(f"A WPR trace writes about {gb_min:.2f} GB per minute (staged in %TEMP%, moved into the run")
    print("folder at the end), so both drives need room -- and it perturbs the session it measures.")
    try:
        go = ask_yes_no("Record a performance trace for this session (one admin prompt)?")
    except EOFError:
        return False
    if not go:
        return False

    run_dir = os.path.join(BASE, "runs", run_id)
    os.makedirs(run_dir, exist_ok=True)
    state_path = os.path.join(run_dir, "trace-state.json")
    stop_path = os.path.join(run_dir, "trace-stop.txt")
    for p in (state_path, stop_path):
        try:
            os.remove(p)
        except OSError:
            pass

    def q(s):
        return "'" + str(s).replace("'", "''") + "'"

    arglist = ",".join(q(a) for a in ("-NoProfile", "-ExecutionPolicy", "Bypass",
                                      "-File", qsite.script("trace_session"),
                                      "-RunDir", run_dir,
                                      "-MaxSeconds", str(int(max_seconds) + 120)))
    subprocess.Popen(["powershell", "-NoProfile", "-Command",
                      f"Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @({arglist})"])
    print("Waiting for the trace to come up -- approve the admin prompt (its window stays open).")
    for _ in range(120):
        time.sleep(1)
        if not os.path.exists(state_path):
            continue
        try:
            # utf-8-sig, not the default: the state file is written by PowerShell, whose
            # `Set-Content -Encoding UTF8` emits a BOM, and json.load() rejects that outright. With the
            # default encoding the handshake below failed on every poll for the full 120 s -- confirmed
            # live 2026-09-19, where a run's whole monitoring window was eaten by the wait.
            st = json.load(open(state_path, encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if st.get("error"):
            print(f"  trace did not start: {st['error']} -- continuing without it")
            return True
        print(f"  tracing {', '.join(st.get('profiles') or [])} -> runs/{run_id}/trace.etl")
        return True
    print("  no handshake from the trace helper yet. If you approved the prompt it IS recording -- "
          "check the new PowerShell window -- and it will be stopped when this session ends.")
    return True


def stop_trace(run_id):
    """Drop the flag Trace-Session.ps1 is watching. It finalises the ETL itself -- `wpr -stop` takes
    roughly half a minute for a multi-hundred-MB trace -- so the wizard never blocks on it."""
    run_dir = os.path.join(BASE, "runs", run_id)
    if not os.path.exists(os.path.join(run_dir, "trace-state.json")):
        return
    with open(os.path.join(run_dir, "trace-stop.txt"), "w"):
        pass
    print(f"Trace stopping -- runs/{run_id}/trace.etl is finalised in the background; "
          f"runs/{run_id}/trace-state.json reports the size once it lands.")


def _drain_stdin():
    """Throw away anything already sitting in the console input buffer; returns how many keys went.

    run_session's stop is a bare input(), so a keystroke pressed earlier -- while waiting out a slow
    handshake or the UAC prompt -- is consumed the instant the session starts and ends it immediately.
    The count is returned rather than discarded silently because the three ways that wait can end
    (a real key, stdin closing, Ctrl+C) are otherwise indistinguishable after the fact, and we have
    already lost three runs to guessing which it was."""
    try:
        import msvcrt
    except ImportError:
        return 0
    n = 0
    try:
        while msvcrt.kbhit():
            msvcrt.getch()
            n += 1
    except Exception:
        pass
    return n


def _print_data_footprint(run_id, max_minutes, presentmon, trace):
    """Print where this session writes and how big it is expected to get, before it starts.

    Everything a run produces stays inside its own folder under runs/ -- nothing is uploaded, and
    runs/ is git-ignored so a fork cannot commit it. The point of this block is the size warning:
    the always-on samplers are trivial (~0.15 MB/min), PresentMon scales with the game's frame rate
    (~2.5 MB/min), and a WPR trace is in a class of its own at ~1.3 GB/min, enough
    that an unbounded trace on a nearly-full disk is a real way to end a session badly. The free-space
    check below uses the run drive, which is where everything lands -- including the trace, which is
    staged in %TEMP% while recording and moved here on stop, so both drives need the headroom.
    """
    run_dir = os.path.join(BASE, "runs", run_id)
    rates = cell.DATA_RATES_MB_PER_MIN
    est = cell.estimate_run_mb(max_minutes, presentmon=presentmon, trace=trace)
    _print_header("Where this session writes")
    print(f"Run folder:  {run_dir}")
    print("Nothing is uploaded. runs/ is git-ignored, so forking this project won't commit session data.")
    print()
    print(f"Expected size for a {max_minutes} min session:")
    print(f"  samplers (Wi-Fi, thermals, fps, ping, logcat)   ~{rates['samplers']:.2f} MB/min   ->  "
          f"~{cell.format_mb(est['samplers'])}")
    if presentmon:
        print(f"  PresentMon game-fps  presentmon.csv             ~{rates['presentmon']:.1f} MB/min   ->  "
              f"~{cell.format_mb(est['presentmon'])}")
    if trace:
        print(f"  WPR trace            trace.etl                  ~{rates['trace'] / 1024:.2f} GB/min   ->  "
              f"~{cell.format_mb(est['trace'])}")
    total_note = "  (almost all of it the trace)" if trace else ""
    print(f"  TOTAL                                           ~{cell.format_mb(est['total'])}{total_note}")
    if trace:
        print()
        print("  !! A trace perturbs the session it measures -- keep traced runs short, and leave room")
        print("     on both this drive and the system (%TEMP%) one.")
    try:
        free_mb = shutil.disk_usage(run_dir).free / (1024 * 1024)
        print(f"\nFree space on the run drive: {cell.format_mb(free_mb)}")
        if est["total"] > free_mb * 0.9:
            print(f"  WARNING: the estimate (~{cell.format_mb(est['total'])}) could exhaust this drive. "
                  "Shorten the session or free space first.")
    except OSError:
        pass


def run_session(run_id, max_seconds, presentmon_hint=None, presentmon_capture=True):
    _print_header("Live session")
    monitor_thread = threading.Thread(target=cell.monitor, args=(run_id, max_seconds),
                                      kwargs={"presentmon_hint": presentmon_hint,
                                              "presentmon_capture": presentmon_capture},
                                      daemon=True)
    monitor_thread.start()
    time.sleep(1.5)  # let monitor's startup (clock sample, sampler spawn) happen before dashboard reads files

    port = _free_port(int(qsite.get("dashboard_port", 8765)))
    dash_srv = dashboard.make_server(run_id, port)
    dash_thread = threading.Thread(target=dash_srv.serve_forever, daemon=True)
    dash_thread.start()
    print(f"Live dashboard: http://127.0.0.1:{port}/")
    print(f"Recording to:   {os.path.join(BASE, 'runs', run_id)}")
    print(f"Play now. Session will stop automatically after {max_seconds // 60} min if you don't stop it first.")
    # Arm the stop key only after a delay. A reflexive Enter at the "Play now" moment used to end the
    # session on the spot -- and whatever the mechanism actually is (a buffered key, a console event,
    # stdin closing), three runs were lost to a ~4 s window before anyone could react. Sleeping first
    # and draining after means anything pressed in this window is discarded instead of stopping the
    # run; a deliberate Enter later still works normally.
    ARM_DELAY_S = 8
    print(f"   (stop key arms in {ARM_DELAY_S}s -- ignore anything you press before then)")
    time.sleep(ARM_DELAY_S)
    drained = _drain_stdin()
    t_session_start = time.time()
    stop_reason = "key"
    try:
        _pause("Press Enter when you're done playing to stop and reduce the session... ")
    except KeyboardInterrupt:
        stop_reason = "ctrl+c"
        print("\n(Ctrl+C) stopping...")
    except EOFError:
        # monitor_thread is daemon=True, so if this propagated as an uncaught exception instead
        # of being caught here, the process would die immediately and take the thread down with
        # it -- skipping cell.monitor()'s `finally` (sampler subprocesses terminated, session.json/
        # clock.json closed out) and leaving PresentMon/PowerShell samplers/ping orphaned. Confirmed
        # live: stdin closing unexpectedly here (not just Ctrl+C) is exactly that scenario.
        #
        # Do NOT treat it as "stop" either: stdin going away is not the operator asking to finish,
        # and the earlier prompts prove it was usable moments ago. Keep the session alive until its
        # own time limit (or a stop file dropped beside the run) instead of silently truncating it.
        stop_reason = "stdin-eof"
        print("\n(stdin closed -- the session will now run to its time limit; drop "
              "runs/<id>/session-stop.txt to end it early)")
        stop_file = os.path.join(BASE, "runs", run_id, "session-stop.txt")
        while monitor_thread.is_alive() and not os.path.exists(stop_file):
            time.sleep(1)
    try:
        with open(os.path.join(BASE, "runs", run_id, "session-stop.json"), "w") as f:
            json.dump({"reason": stop_reason,
                       "after_s": round(time.time() - t_session_start, 1),
                       "drained_keys": drained,
                       "stdin_isatty": sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else None},
                      f, indent=1)
    except OSError:
        pass
    print(f"Session ended ({stop_reason} after {time.time() - t_session_start:.0f}s"
          + (f", {drained} stray key(s) discarded)" if drained else ")"))
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
    lc_path = os.path.join(BASE, "runs", run_id, "linkcheck.json")
    if os.path.exists(lc_path):
        try:
            linkcheck.print_summary(json.load(open(lc_path, encoding="utf-8")))
        except (OSError, ValueError):
            pass
    cell.results(run_id)
    res_path = os.path.join(BASE, "runs", run_id, "results.json")
    if os.path.exists(res_path):
        res = json.load(open(res_path))
        for key, label in (("pc_game_fps_note", "PC game-fps"), ("headset_data_note", "Headset data")):
            if res.get(key):
                print(f"{label}: {res[key]}")
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

    if ask_yes_no("Save this run as the new baseline?"):
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

        serial = ensure_headset_connected()
        ensure_streamer_running()
        run_id = quick_session() if quick else configure_session()
        presentmon_hint = ask_presentmon_hint(run_id)
        max_minutes = ask_int("Max session length, in minutes (you can stop earlier)", 60)
        ask_and_run_linkcheck(run_id, serial)
        tracing = start_trace(run_id, max_minutes * 60)
        _print_data_footprint(run_id, max_minutes,
                              presentmon=(presentmon_hint is not None), trace=tracing)
        run_session(run_id, max_minutes * 60, presentmon_hint=presentmon_hint,
                    presentmon_capture=(presentmon_hint is not None))
        if tracing:
            stop_trace(run_id)
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
