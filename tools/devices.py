"""devices.py - the one place a headset family is described.

The harness separates what belongs to the PC from what belongs to the headset, and this module is the
headset half's whole vocabulary: which shell commands reach the headset's radio/thermal/compositor
state, what its streaming client's process is called, which files its samplers write, and which of
those the reducers can expect to exist. Everything PC-side (Sample-PC.ps1, PresentMon, the fingerprint
rules, the dashboard's own plumbing) is already device-agnostic once a profile is filled in.

`quest3` is the only profile that has ever been run against hardware, and its values are exactly the
literals the harness used before this module existed -- so a Quest 3 run reduces byte-for-byte the
same as it did. The module exists so the next headset is data, not a hunt through cell.py:

  * a second Meta headset (Quest 2/3S/Pro): same profile, different panel/refresh defaults.
  * Pico 4 Ultra / Neo 3, Vive Focus: Android and adb like the Quest, so `wifi_status_cmd` stays
    `cmd wifi status` and `/proc/net/*` works; what changes is the streaming client's process name,
    the log tags (VrApi/QC2Comp are Meta's and Qualcomm's), and which vendor `dumpsys` services exist
    -- OculusWifi, vrpowermanager, cm_wifi and Strata have no equivalent off Meta hardware.
  * Steam Frame: SteamOS, not Android. adb still works (Valve's developer mode enables ssh/adb/rdp),
    but there is no `dumpsys`, no `logcat` and no `cmd wifi`, so it needs a profile whose
    `wifi_status_cmd` is `iw dev <if> station dump`-shaped and whose `logcat_tags` is empty. That
    profile also needs a transport field this module does not have yet, because nothing here is
    non-adb today -- do not add one until there is a device to drive with it.

Filenames are per-profile because the Quest's are named `quest_*`; a new profile picks its own and the
readers follow it (cell.ARTIFACTS, dashboard), so nothing has to be renamed on disk to add a device.
"""
import qsite

# Which profile the harness runs against: `device` in site.json, or QUEST3_DEVICE.
DEFAULT = "quest3"

PROFILES = {
    "quest3": {
        "label": "Meta Quest 3 (Android, wireless adb)",

        # The streaming client's process, as `ps -A -o NAME` on the headset reports it. `{procs}` is
        # joined with `|` into one grep -E alternation; the trailing `tr` folds the matches onto one
        # line so session.json's `proc` field stays a single string.
        "session_procs": ("VirtualDesktop", "xrstreamingclient"),
        "session_cmd": "ps -A -o NAME | grep -E '{procs}' | tr '\\n' ' '",
        # Substring of that proc string -> the stack the run belongs to. First match wins.
        "stacks": (("VirtualDesktop", "vd"), ("xrstreamingclient", "airlink")),
        "default_stack": "vd",

        # The PC-side half of the stack, for the wizard's "is the streamer running" check.
        "pc_streamer_procs": ("OVRServer_x64.exe", "VirtualDesktop.Streamer.exe",
                              "VirtualDesktop.Server.exe"),

        # Sample-Quest.ps1 and the artifact each of its outputs writes. `outputs` is what monitor()
        # always collects; `sf` is the SurfaceFlinger latency sampler capture() opts into, because it
        # costs a per-second adb round trip. Argument names are the sampler's own switches.
        "sampler": {
            "script": "quest_sampler",
            "outputs": (("OutFile", "wifi"), ("NetFile", "net"), ("EnvFile", "env"),
                        ("CmFile", "cm")),
            "sf": ("SfFile", "sf"),
        },

        # logcat -s filter. VrApi is the VR runtime's per-second compositor telemetry (the only frame
        # source that also covers Air Link); QC2Comp is the hardware decoder's own output rate/bitrate
        # and carries the codec identity in its instance name. The client tags are captured
        # opportunistically -- see cell.py's comment and QUEST-AGENT-PLAYBOOK.md for why they are
        # expected to be silent on this rig.
        "logcat_tags": "VrApi QC2Comp VirtualDesktop.Android OVRMediaCodec VR_Engine ALVR",

        # WifiInfo: RSSI, link speeds, freq and the four MAC counters the harness reduces. AOSP
        # `cmd wifi`, so any Android headset answers the same shape.
        "wifi_status_cmd": "cmd wifi status",

        # OVR Metrics CSV, pulled from the headset at reduce time (Meta's metrics service).
        "ovr_metrics_dir": ("/sdcard/Android/data/com.oculus.ovrmonitormetricsservice/files/"
                            "CapturedMetrics"),

        "artifacts": {
            "wifi": "quest_wifi_samples.tsv",
            "net": "quest_net_samples.tsv",
            "env": "quest_env_samples.tsv",
            "sf": "sf_latency_samples.tsv",
            "layers": "sf_layers.log",
            "logcat": "headset_logcat.txt",
            "cm": "cm_wifi_snapshots.txt",
            "ovr": "ovr_metrics.csv",
        },
    },
}

_CACHE = {}


def active():
    """The profile named by site.json's `device` (default: quest3)."""
    name = (qsite.get("device") or DEFAULT).strip() or DEFAULT
    if name not in PROFILES:
        raise SystemExit(f"site config names device '{name}', which this build does not know; "
                         f"known devices: {', '.join(sorted(PROFILES))}")
    if name not in _CACHE:
        _CACHE[name] = PROFILES[name]
    return _CACHE[name]


def session_cmd(profile=None):
    """The headset command whose output tells monitor() whether a streaming session is running."""
    p = profile or active()
    return p["session_cmd"].format(procs="|".join(p["session_procs"]))


def stack_from_proc(proc, profile=None):
    """'vd' / 'airlink' from a session.json segment's `proc` string, or None when unrecognised."""
    p = profile or active()
    for needle, stack in p["stacks"]:
        if needle in (proc or ""):
            return stack
    return None
