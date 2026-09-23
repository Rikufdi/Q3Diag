"""qsite.py - one place where every machine-specific path comes from.

(Named qsite, not site: the stdlib already owns `site`.)

The harness reads its paths/IPs from `tools/site.json` (git-ignored, one per rig).
`tools/site.example.json` is the tracked template; any key left empty falls back to
auto-discovery (PATH / known install locations) and finally to an actionable error.

Precedence, highest first:
  1. environment variable  QUEST3_<KEY>   (e.g. QUEST3_ADB, QUEST3_QUEST_IP)
  2. tools/site.json
  3. auto-discovery
Override the profile location itself with QUEST3_SITE=<path to json>.

Usage (from a script living in tools/):
    import qsite
    ADB  = qsite.path("adb")           # resolved, exits with instructions if absent
    IP   = qsite.get("quest_ip")
    BASE = qsite.base_dir()
    SAMPLER = qsite.script("quest_sampler")
"""
import json
import os
import shutil
import sys

# Frozen (PyInstaller) builds extract their bundled files -- this module, the .ps1 samplers,
# site.example.json, a vendored adb.exe -- to sys._MEIPASS, not to wherever this .py file's own
# on-disk location would suggest. __file__ inside a frozen build doesn't reliably point there, so
# detect frozen mode explicitly rather than relying on it.
FROZEN = bool(getattr(sys, "frozen", False))
TOOLS_DIR = sys._MEIPASS if FROZEN else os.path.dirname(os.path.abspath(__file__))
# The exe's own directory, not TOOLS_DIR: for a --onefile build, sys._MEIPASS is a temp extraction
# directory that gets deleted when the process exits, so anything the app needs to persist across
# runs (site.json, and runs/baseline/etc under base_dir) has to live next to the exe instead, or the
# wizard's own "discover the headset's IP once, remember it" feature would silently lose that memory
# on every single run.
PERSIST_DIR = os.path.dirname(sys.executable) if FROZEN else TOOLS_DIR
# Where the optional third-party tools the user supplies themselves go (PresentMon, iperf3). Next to
# the exe, deliberately not inside TOOLS_DIR: in a frozen build TOOLS_DIR is the bundle's own
# _internal/ folder, which is no place to ask a user to drop files. Matches the dev tree's tools/vendor/
# (git-ignored). release/build_release.py ships this folder with a readme in it.
VENDOR_DIR = os.path.join(PERSIST_DIR, "vendor")
DEFAULT_BASE_DIR = os.path.dirname(PERSIST_DIR) if not FROZEN else PERSIST_DIR
CONFIG_PATH = os.environ.get("QUEST3_SITE") or os.path.join(PERSIST_DIR, "site.json")
EXAMPLE_PATH = os.path.join(TOOLS_DIR, "site.example.json")

# Keys that name an executable or install path. `path()` resolves these.
TOOL_KEYS = ("adb", "python", "tshark", "pktmon", "ffprobe",
             "vd_streamer_exe", "vd_settings_json", "ovr_server_exe", "vd_switcher_exe")

# Discovery order per key, used when the profile leaves the key empty.
_AUTO = {
    "adb": (
        os.path.join(TOOLS_DIR, "adb.exe"),  # bundled with a packaged release -- see release/build_release.py
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe"
                     r"\platform-tools\adb.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Android\Sdk\platform-tools\adb.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), r"Android\platform-tools\adb.exe"),
    ),
    "python": (sys.executable,),
    "tshark": (
        os.path.join(os.environ.get("ProgramFiles", ""), r"Wireshark\tshark.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), r"Wireshark\tshark.exe"),
    ),
    "pktmon": (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "PktMon.exe"),),
    "ffprobe": (
        os.path.join(os.environ.get("ProgramFiles", ""), r"Virtual Desktop Streamer\ffprobe.exe"),
    ),
    "vd_streamer_exe": (
        os.path.join(os.environ.get("ProgramFiles", ""), r"Virtual Desktop Streamer\VirtualDesktop.Streamer.exe"),
    ),
    "vd_settings_json": (
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Virtual Desktop\StreamerSettings.json"),
    ),
    "ovr_server_exe": (
        os.path.join(os.environ.get("ProgramFiles", ""), r"Oculus\Support\oculus-runtime\OVRServer_x64.exe"),
    ),
}

# Script names resolved next to this file, never next to base_dir.
_SCRIPTS = {
    "quest_sampler": "Sample-Quest.ps1",
    "pc_sampler": "Sample-PC.ps1",
    "quest_probe": "Quest-Probe.ps1",
    "game_fps_sampler": "Sample-GameFPS.ps1",
    "trace_session": "Trace-Session.ps1",
}

DEFAULTS = {
    "base_dir": DEFAULT_BASE_DIR,
    "quest_ip": "",
    "pc_ip": "",
    "elev_task": "PCVR-Elev",
    "ovr_metrics_dir": "/sdcard/Android/data/com.oculus.ovrmonitormetricsservice/files/CapturedMetrics",
    "powershell": "powershell",
    # Optional: PC game frame-time capture (Sample-GameFPS.ps1). Not vendored, but auto-detected from
    # vendor/ (see presentmon_exe()) -- empty means "skip PC game-fps capture".
    "presentmon_exe": "",
    "presentmon_args": "",
    # Optional: link-capacity check (tools/linkcheck.py). iperf3_exe is the Windows client (empty =
    # vendor/ then PATH); iperf3_android is the aarch64 build pushed to the headset as the server.
    # Neither is vendored -- empty means "don't offer the check".
    "iperf3_exe": "",
    "iperf3_android": "",
    # Optional: tools/dashboard.py listen port.
    "dashboard_port": 8765,
}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise SystemExit(f"site config {path} is not valid JSON: {exc}")


_CONFIG = None


def config():
    """The merged profile (defaults <- site.json <- QUEST3_* environment)."""
    global _CONFIG
    if _CONFIG is None:
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in _read_json(EXAMPLE_PATH).items() if v})
        merged.update({k: v for k, v in _read_json(CONFIG_PATH).items() if v})
        for key in list(merged) + list(TOOL_KEYS) + list(_SCRIPTS):
            from_env = os.environ.get("QUEST3_" + key.upper())
            if from_env:
                merged[key] = from_env
        merged["base_dir"] = merged["base_dir"] or DEFAULT_BASE_DIR
        merged["base_dir"] = os.path.abspath(merged["base_dir"])
        _CONFIG = merged
    return _CONFIG


def get(key, default=None):
    return config().get(key, default)


def save(values):
    """Persist keys into the site profile (site.json next to the exe, or tools/site.json in a checkout),
    creating it from site.example.json's template when it does not exist yet. Returns the keys whose
    value actually changed, so callers can stay quiet when there was nothing to do.

    This is the one writer for the profile: the wizard's USB handoff used to carry its own copy of this
    read-modify-write, and the adb endpoint resolution in cell.py needed the same thing. Resets the
    in-process cache, so a `get()` after a `save()` in the same process sees the new value -- callers
    that cached the value in their own module global (cell.QUEST_IP, wizard.QUEST_IP) still have to
    patch that themselves."""
    global _CONFIG
    path = CONFIG_PATH
    doc = {}
    if exists(path):
        doc = _read_json(path)
    elif exists(EXAMPLE_PATH):
        doc = _read_json(EXAMPLE_PATH)
    changed = {k: v for k, v in values.items() if doc.get(k) != v}
    if not changed:
        return {}
    doc.update(changed)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    _CONFIG = None
    return changed


def base_dir():
    return config()["base_dir"]


def run_dir(run_id):
    return os.path.join(base_dir(), "runs", run_id)


def script(key):
    """Absolute path to a harness script shipped next to this file."""
    return os.path.join(TOOLS_DIR, _SCRIPTS[key])


def exists(path):
    return bool(path) and os.path.isfile(path)


def path(key, fallbacks=()):
    """Resolve an executable/install path, or exit with instructions."""
    configured = get(key)
    if configured:
        if key in TOOL_KEYS and not exists(configured) and not shutil.which(configured):
            raise SystemExit(
                f"site.json: '{key}' is set to {configured!r}, which does not exist.\n"
                f"Fix {CONFIG_PATH} or unset the key to use auto-discovery.")
        return configured
    if key in _AUTO:
        for candidate in _AUTO[key]:
            if exists(candidate):
                return candidate
    found = shutil.which(key)
    if found and os.path.getsize(found) > 0:   # 0 bytes = broken shim, not an executable
        return found
    for candidate in fallbacks:
        if exists(candidate):
            return candidate
    raise SystemExit(
        f"could not locate '{key}'. Set it in {CONFIG_PATH} "
        f"(copy tools/site.example.json to tools/site.json) or export QUEST3_{key.upper()}.")


def presentmon_exe():
    """Optional, so unlike path() this returns None instead of raising when there's nothing to find --
    cell.py's monitor() already treats a missing PresentMon as "skip this sampler", not an error, and
    prints a message telling you how to add it. Checks site.json/QUEST3_PRESENTMON_EXE first (a real
    install always wins), then a copy dropped into vendor/ as PresentMon.exe -- the folder the release
    ships with a readme, and the one cell.py's "missing" message points at -- then a copy beside this
    module, which is where it had to go before VENDOR_DIR existed. Not bundled by
    release/build_release.py (see its module docstring for why), but auto-detected if you drop one in
    yourself, so that needs no site.json edit either."""
    configured = get("presentmon_exe")
    if configured:
        return configured
    for candidate in (os.path.join(VENDOR_DIR, "PresentMon.exe"),
                      os.path.join(TOOLS_DIR, "PresentMon.exe")):
        if exists(candidate):
            return candidate
    return None


def iperf3_exe():
    """PC-side iperf3 client for the optional link check (linkcheck.py). Optional like
    presentmon_exe(): None when there is nothing to find, and the wizard simply does not offer the
    check. Checks site.json/QUEST3_IPERF3_EXE first, then vendor/iperf3.exe -- where the release's
    note file tells you to drop the Windows build -- then PATH, so a winget/scoop install works with
    no config at all.

    Deliberately does NOT accept an extensionless vendor/iperf3: that name is the *Android* build
    (see iperf3_android()), and matching it here hands the headset's ARM binary to Windows'
    CreateProcess, which fails with a bare WinError 193. Confirmed live 2026-09-22 with both files in
    vendor/."""
    configured = get("iperf3_exe")
    if configured:
        return configured
    candidate = os.path.join(VENDOR_DIR, "iperf3.exe")
    if exists(candidate):
        return candidate
    return shutil.which("iperf3")


def iperf3_android():
    """The aarch64 Android iperf3 that gets pushed to the headset: the headset has to run the *server*
    end, since that is the side whose link we want to characterise and the only arrangement that
    needs no inbound allowance on the PC. Optional; the link check needs this AND iperf3_exe().

    Uncompressed only, and deliberately not auto-decompressed: these builds ship as .gz/.zip, and
    silently unpacking a downloaded archive into a path we then execute is a worse habit than asking
    the user to unpack it. Names tried: iperf3, iperf3-aarch64, iperf3-arm64."""
    configured = get("iperf3_android")
    if configured:
        return configured
    for name in ("iperf3", "iperf3-aarch64", "iperf3-arm64"):
        candidate = os.path.join(VENDOR_DIR, name)
        if exists(candidate):
            return candidate
    return None


