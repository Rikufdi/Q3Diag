#!/usr/bin/env python3
"""build_release.py - assemble a portable, self-contained Windows release of the wizard.

    python release/build_release.py

Produces release/dist/Q3Diag-Wizard/ (a folder you can zip and hand to someone -- also written as
release/dist/Q3Diag-Wizard-<version>-win64.zip) containing:

  Q3Diag-Wizard.exe          entry point -- packages tools/wizard.py plus everything it imports
                              (cell.py, qsite.py, dashboard.py, analyze.py) via PyInstaller
  _internal/                 bundled Python runtime + the .ps1 samplers + site.example.json
  adb.exe, AdbWinApi.dll,
  AdbWinUsbApi.dll           vendored Android platform-tools (see THIRD_PARTY_NOTICES.md) --
                              a fallback only: an adb already on PATH or in site.json wins
  NOTICE.txt                 Google's upstream notice for adb, shipped verbatim
  THIRD_PARTY_NOTICES.md, LICENSE, LICENSE-DATA, README.md   copied in as-is
  docs/dashboard.jpg         the dashboard screenshot the README embeds (not local-only docs/)
  vendor/                    placeholders for the tools you supply yourself: drop PresentMon.exe
                              here and the wizard auto-detects it; drop iperf3.exe plus an aarch64
                              iperf3 and it offers its link-capacity check (see each *_here.txt)

adb is the only third-party binary bundled -- it's load-bearing (nothing works without talking to
the headset). PresentMon and iperf3 deliberately are not, even though both are used elsewhere in
this project: neither is needed for full use of the tool (PresentMon is an optional PC-side fps
enhancement; iperf3 isn't wired into any code path at all), so they're not worth the extra binaries
in a release that's already working to avoid antivirus false positives on adb alone. PresentMon still
auto-detects a copy placed next to the exe (drop in PresentMon.exe yourself -- see
qsite.presentmon_exe() and the startup message monitor() prints when it's missing) without needing
site.json edited.

Nothing under release/ except this script, release/README.md and release/version_info.txt is meant
to be committed -- release/build/ (scratch: downloaded platform-tools) and release/dist/ (the
output) are both git-ignored and fully regenerable by re-running this script.

Requires: `pip install pyinstaller` (done automatically below if missing), and internet access for
the one-time platform-tools download (cached in release/build/ after that). Windows only, same as
the rest of this harness.
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
RELEASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(RELEASE_DIR, "build")
DIST_DIR = os.path.join(RELEASE_DIR, "dist")
APP_NAME = "Q3Diag-Wizard"

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
PLATFORM_TOOLS_ZIP = os.path.join(BUILD_DIR, "platform-tools.zip")
PLATFORM_TOOLS_DIR = os.path.join(BUILD_DIR, "platform-tools")
ADB_FILES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt")

# Sampler scripts + the config template the harness reads at runtime -- see tools/qsite.py's
# _SCRIPTS dict and EXAMPLE_PATH. Not Python imports, so PyInstaller's dependency analysis can't
# find these on its own; they have to be listed explicitly.
DATA_FILES = ("Sample-Quest.ps1", "Sample-PC.ps1", "Sample-GameFPS.ps1", "Quest-Probe.ps1",
              "Trace-Session.ps1", "site.example.json")

# Placeholders for the optional third-party tools the user supplies themselves, written into the
# release's vendor/ folder (regenerated on every build -- vendor/ is not user state, so anything
# dropped there is NOT preserved across rebuilds; a dev's own copy belongs in tools/vendor/, which
# site.json points at). PresentMon is auto-detected from here by qsite.presentmon_exe(); iperf3 is
# called by no code path at all, which its note says outright rather than implying the tool uses it.
VENDOR_NOTES = {
    "presentmon_here.txt": """\
PresentMon is not bundled -- download it yourself.
==================================================

1. Get the console-app build (e.g. PresentMon-2.5.1-x64.exe) from:

     https://github.com/GameTechDev/PresentMon/releases/latest

2. Rename it to exactly  PresentMon.exe  and drop it in this folder, so you end up with:

     vendor\\PresentMon.exe

That is all. The wizard auto-detects it on the next run -- session setup will say
"PresentMon found: ..." -- and then offers optional PC-side frame-rate capture for the game
itself, as opposed to the headset compositor's frame rate that every other sampler sees.

You can also leave this folder alone and point the tool at a build kept anywhere else, by
setting "presentmon_exe" in site.json (or exporting QUEST3_PRESENTMON_EXE). A real install
wins over this folder.

Why it is not included: PresentMon is MIT-licensed and free to redistribute, but every extra
executable in this download makes antivirus tools more likely to flag it as suspicious.
""",
    "iperf3_here.txt": """\
iperf3 is not bundled, but the tool can drive it -- two builds go here.
=====================================================================

What it is for: measuring the raw PC <-> headset link with no video running, so a session that
reads badly can be told apart from a radio that simply cannot carry the bitrate. The wizard
offers that check before a session starts -- TCP down and up, then a UDP ramp -- but only when
BOTH halves below are present. The check is the only thing in the tool that uses iperf3.

How this was set up on the machine the tool was written on:

1. PC side. One command, nothing to copy across:

     winget install ar51an.iPerf3

   That installs iperf3 3.21 and puts iperf3.exe on PATH, which the tool searches. scoop/choco
   or your own build work too, as does dropping the exe in here as vendor\\iperf3.exe, or
   setting iperf3_exe in site.json.

2. Headset side. This is the awkward half: the headset runs the *server*, so it needs an
   aarch64 Android build. In order of least effort:

   a. If an iperf3 server has ever been run on this headset, take that build back out instead of
      making a new one -- /data/local/tmp is where such things get staged:

        adb shell ls -l /data/local/tmp/iperf3
        adb pull /data/local/tmp/iperf3 vendor/iperf3

      That is how the copy on this rig was recovered: 133,600 bytes of aarch64, pushed by hand
      during the original baseline measurements on 2026-09-15 at 17:15 -- four minutes before
      the first recorded test. A headset does NOT come with this: nothing about a Quest ships
      iperf3, and on one that has never been set up this step simply finds nothing, so go to (b).

      No adb on PATH? The release bundles one at _internal\\adb.exe.

   b. Otherwise build it. Upstream ships SOURCE ONLY -- every asset on
      https://github.com/esnet/iperf/releases is a .tar.gz, there is no official Android or
      Windows binary -- so a headset build means cross-compiling for aarch64-linux-android
      with the Android NDK (the tarball's ./configure plus the NDK's clang wrappers, which
      needs a POSIX shell: MSYS2 or WSL).

   c. A prebuilt binary from somewhere else works as well, but that is your call -- you are
      about to run it on your headset as a server.

   Name the file exactly vendor\\iperf3: no extension, and uncompressed. Nothing here will
   unpack an archive into a path it then executes; that is the user's job on purpose.

   Once the link check has run, the tool leaves the build at /data/local/tmp/iperf3 and skips
   re-pushing it while the size matches -- so a copy dropped or lost later can always be pulled
   back with the step (a) commands.

The errors usually say which half is wrong: a build for the wrong architecture is reported as
"not an aarch64 Android binary", or as "server exited immediately" if it only turns out to be
wrong once the headset tries to run it. And if only the TCP *uplink* test fails, the reverse
(-R) connection back to this PC is being blocked -- a firewall is the first suspect; the
downlink still measures correctly without it.

If either half is missing, the link check is simply not offered and nothing else changes.
""",
}


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found -- installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)


def fetch_adb():
    """Download+extract Google's platform-tools once, cached in release/build/. See
    THIRD_PARTY_NOTICES.md for why bundling adb.exe this way is fine license-wise (Apache 2.0,
    same as scrcpy's precedent for bundling it)."""
    os.makedirs(BUILD_DIR, exist_ok=True)
    if all(os.path.exists(os.path.join(PLATFORM_TOOLS_DIR, f)) for f in ADB_FILES):
        print(f"adb: using cached copy in {PLATFORM_TOOLS_DIR}")
        return
    print(f"adb: downloading {PLATFORM_TOOLS_URL} ...")
    urllib.request.urlretrieve(PLATFORM_TOOLS_URL, PLATFORM_TOOLS_ZIP)
    with zipfile.ZipFile(PLATFORM_TOOLS_ZIP) as z:
        for f in ADB_FILES:
            z.extract(f"platform-tools/{f}", BUILD_DIR)
    print(f"adb: extracted to {PLATFORM_TOOLS_DIR}")


USER_STATE_NAMES = ("runs", "baseline", "site.json")


def _preserve_user_state():
    """A frozen build's own base_dir (where it keeps runs/, baseline/, site.json -- see
    qsite.PERSIST_DIR) is the same folder PyInstaller writes its output into, since that's simply
    "next to the exe". A naive rebuild wipes that whole folder to clear the old PyInstaller output --
    which means it silently destroys every real test run and baseline anyone has accumulated by using
    a previous build, the moment you rebuild. Confirmed the hard way, 2026-09-18: a rebuild mid-session
    deleted a user's second real test run before it could be looked at. Save these aside before the
    wipe, restore after."""
    app_dir = os.path.join(DIST_DIR, APP_NAME)
    saved = {}
    for name in USER_STATE_NAMES:
        src = os.path.join(app_dir, name)
        if os.path.exists(src):
            dst = os.path.join(BUILD_DIR, f"_preserved_{name}")
            if os.path.exists(dst):
                shutil.rmtree(dst) if os.path.isdir(dst) else os.remove(dst)
            shutil.move(src, dst)
            saved[name] = dst
    if saved:
        print(f"preserving existing user data across rebuild: {', '.join(saved)}")
    return saved


def _restore_user_state(saved):
    app_dir = os.path.join(DIST_DIR, APP_NAME)
    if saved and not os.path.isdir(app_dir):
        # The build itself failed before producing app_dir at all -- moving into a directory that
        # doesn't exist would raise and mask whatever error PyInstaller actually hit. Leave the
        # preserved copies in place and say exactly where, instead.
        print(f"build did not produce {app_dir} -- preserved user data is still at "
              f"{', '.join(saved.values())}, not lost, just not restored yet. Re-run the build and "
              f"it will pick these up automatically next time (same source paths).")
        return
    for name, src in saved.items():
        shutil.move(src, os.path.join(app_dir, name))


def _our_adb_server_pids():
    """PIDs of running processes whose image is the adb.exe inside the previous build.

    Anything executing *that* file is ours by construction -- nothing else launches our bundled
    copy -- which is what makes stopping it safe. This is deliberately filtered by image path
    instead of just calling `adb kill-server`: that kills whichever server owns port 5037, whoever
    started it, so a rebuild would take down an unrelated adb server (Android Studio's, scrcpy's,
    another project's) that merely happens to be up at the same time."""
    adb = os.path.join(DIST_DIR, APP_NAME, "_internal", "adb.exe")
    if not os.path.exists(adb):
        return []
    ps = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='adb.exe'\" | "
         "ForEach-Object { \"$($_.ProcessId)`t$($_.ExecutablePath)\" }"],
        capture_output=True, text=True)
    want = os.path.normcase(os.path.abspath(adb))
    pids = []
    for line in ps.stdout.splitlines():
        pid, _, exe = line.strip().partition("\t")
        if pid.isdigit() and exe and os.path.normcase(os.path.abspath(exe)) == want:
            pids.append(int(pid))
    return pids


def _stop_stale_adb_server():
    """Stop a leftover adb server, but only if it is running our own bundled adb.exe.

    Windows refuses to delete a running program's image file, and the wizard launches
    `_internal/adb.exe` as the adb *server*, which outlives the wizard by design (it stays up until
    `kill-server` or reboot). So the first rebuild after any wizard run dies with
    `[WinError 5] Access is denied: ...\\_internal\\adb.exe` (hit live 2026-09-19). This clears that
    and nothing else -- an adb server someone else started is left strictly alone. Returns whether
    anything was stopped."""
    pids = _our_adb_server_pids()
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True)
    return bool(pids)


def _wipe_dist():
    """Delete the previous build's output, recovering from the one routine failure.

    A leftover adb server holding the previous build's `_internal/adb.exe` makes rmtree raise
    PermissionError, and that is the common case (it is why this exists) -- so stop that server (and
    only that one, see _stop_stale_adb_server) and try once more. Anything still locked after that,
    e.g. the wizard itself running, is a real error and is surfaced as-is."""
    if not os.path.exists(DIST_DIR):
        return
    for attempt in (1, 2):
        try:
            shutil.rmtree(DIST_DIR)
            return
        except PermissionError:
            if attempt == 2 or not _stop_stale_adb_server():
                raise
            print("dist/ is locked by the previous build's own adb server -- stopped it, retrying the wipe...")
            time.sleep(1.0)


def _build():
    """Wipe dist/ and run one PyInstaller pass over the current tools/ tree."""
    _wipe_dist()
    pyinstaller_work = os.path.join(BUILD_DIR, "pyinstaller")
    args = [
        sys.executable, "-m", "PyInstaller",
        os.path.join(TOOLS_DIR, "wizard.py"),
        "--name", APP_NAME,
        "--onedir", "--console", "--noconfirm", "--noupx",
        # Both of these are known, well-documented mitigations for antivirus false positives on
        # PyInstaller executables (Defender's ML heuristic in particular flags the bare bootloader
        # pattern -- self-extracting, dynamic DLL loading -- fairly indiscriminately for unsigned
        # binaries from unknown publishers, regardless of actual behavior): --noupx because a
        # UPX-compressed binary is itself a strong, independent AV signal (packers are heavily
        # associated with malware, on top of the bootloader pattern); the version resource because
        # a binary with no publisher/product metadata at all reads as more suspicious than one that
        # identifies itself. Neither is a complete fix -- the actually effective one is Authenticode
        # code-signing with a reputable certificate, which costs money and isn't set up here yet.
        "--version-file", os.path.join(RELEASE_DIR, "version_info.txt"),
        "--distpath", DIST_DIR,
        "--workpath", pyinstaller_work,
        "--specpath", pyinstaller_work,
    ]
    for f in DATA_FILES:
        args += ["--add-data", f"{os.path.join(TOOLS_DIR, f)};."]
    for f in ADB_FILES:
        # adb.exe/DLLs are executables, not data -- --add-binary so PyInstaller doesn't try to
        # analyze them for further Python dependencies. NOTICE.txt is plain text; --add-data for it.
        flag = "--add-data" if f.endswith(".txt") else "--add-binary"
        args += [flag, f"{os.path.join(PLATFORM_TOOLS_DIR, f)};."]
    print("Running:", " ".join(args))
    subprocess.run(args, check=True, cwd=REPO_ROOT)


def run_pyinstaller():
    saved_state = _preserve_user_state()
    try:
        _build()
    finally:
        # Always attempt this, even if the wipe or PyInstaller failed -- otherwise a failed build
        # leaves the preserved runs/baseline/site.json stranded in release/build/_preserved_* with no
        # obvious way back, instead of simply back where they were. This has now happened twice: once
        # for a PyInstaller failure (2026-09-18, the case this guard was written for), and again on
        # 2026-09-19 when the *wipe* raised on a locked _internal/adb.exe -- the try used to begin
        # after the wipe, so that stranded the user's state too. `_build()` now does the wipe, so the
        # whole destructive part is inside the guard.
        _restore_user_state(saved_state)


def copy_extras():
    app_dir = os.path.join(DIST_DIR, APP_NAME)
    for name in ("THIRD_PARTY_NOTICES.md", "LICENSE", "LICENSE-DATA", "README.md"):
        src = os.path.join(REPO_ROOT, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(app_dir, name))
    # README.md embeds docs/dashboard.jpg, so the released tree needs that file at the same relative
    # path or the packaged README renders with a broken image. Listed explicitly rather than copying
    # docs/ wholesale: docs/ also holds local-only material (the git-ignored bug report), and a whole
    # directory copy would silently start shipping whatever gets dropped in there next.
    shot = os.path.join(REPO_ROOT, "docs", "dashboard.jpg")
    if os.path.exists(shot):
        shot_dir = os.path.join(app_dir, "docs")
        os.makedirs(shot_dir, exist_ok=True)
        shutil.copy2(shot, os.path.join(shot_dir, "dashboard.jpg"))
    # vendor/ = where the user drops PresentMon (and optionally iperf3) themselves. Written rather
    # than copied from the repo so it exists even in a fresh checkout, and regenerated on every build.
    vendor_dir = os.path.join(app_dir, "vendor")
    os.makedirs(vendor_dir, exist_ok=True)
    for name, text in VENDOR_NOTES.items():
        with open(os.path.join(vendor_dir, name), "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(text)


def make_zip():
    """Zip the app for distribution -- deliberately WITHOUT the builder's own user state.

    `dist/<app>/` is also where a frozen build keeps runs/, baseline/ and site.json (see
    qsite.PERSIST_DIR, and _preserve_user_state() above, which exists precisely to protect them), so
    archiving that folder wholesale ships the builder's private test runs and their site.json -- real
    LAN IPs, install paths, per-run captures -- inside the one file whose entire purpose is to be
    published. Confirmed 2026-09-19: the beta zip contained a site.json with the rig's IPs and 91
    entries under runs/. Same three names _preserve_user_state() treats as user data."""
    app_dir = os.path.join(DIST_DIR, APP_NAME)
    zip_base = os.path.join(DIST_DIR, f"{APP_NAME}-beta-win64")
    skip = set(USER_STATE_NAMES)

    def excluded(rel):
        return rel.replace("\\", "/").split("/", 1)[0] in skip

    count = 0
    with zipfile.ZipFile(zip_base + ".zip", "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(app_dir):
            dirs[:] = [d for d in dirs
                       if not excluded(os.path.relpath(os.path.join(root, d), app_dir))]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), app_dir)
                if excluded(rel):
                    continue
                z.write(os.path.join(root, f), os.path.join(APP_NAME, rel))
                count += 1
    print(f"\nRelease folder: {app_dir}")
    print(f"Release zip:    {zip_base}.zip  ({count} files; excluded {', '.join(sorted(skip))})")


def main():
    ensure_pyinstaller()
    fetch_adb()
    run_pyinstaller()
    copy_extras()
    make_zip()


if __name__ == "__main__":
    main()
