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

adb is the only third-party binary bundled -- it's load-bearing (nothing works without talking to
the headset). PresentMon and iperf3 deliberately are not, even though both are used elsewhere in
this project: neither is needed for full use of the tool (PresentMon is an optional PC-side fps
enhancement; iperf3 isn't wired into any code path at all, purely a manual advanced/research recipe
in docs/RESEARCH.md), so they're not worth the extra binaries in a release that's already working to
avoid antivirus false positives on adb alone. PresentMon still auto-detects a copy placed next to the
exe (drop in PresentMon.exe yourself -- see qsite.presentmon_exe() and the startup message
monitor() prints when it's missing) without needing site.json edited.

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
              "site.example.json")


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


def run_pyinstaller():
    saved_state = _preserve_user_state()
    if os.path.exists(DIST_DIR):
        shutil.rmtree(DIST_DIR)
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
    try:
        subprocess.run(args, check=True, cwd=REPO_ROOT)
    finally:
        # Always attempt this, even if PyInstaller failed -- otherwise a failed build leaves the
        # preserved runs/baseline/site.json stranded in release/build/_preserved_* with no obvious
        # way back, instead of simply back where they were.
        _restore_user_state(saved_state)


def copy_extras():
    app_dir = os.path.join(DIST_DIR, APP_NAME)
    for name in ("THIRD_PARTY_NOTICES.md", "LICENSE", "LICENSE-DATA", "README.md"):
        src = os.path.join(REPO_ROOT, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(app_dir, name))


def make_zip():
    app_dir = os.path.join(DIST_DIR, APP_NAME)
    zip_base = os.path.join(DIST_DIR, f"{APP_NAME}-beta-win64")
    shutil.make_archive(zip_base, "zip", root_dir=DIST_DIR, base_dir=APP_NAME)
    print(f"\nRelease folder: {app_dir}")
    print(f"Release zip:    {zip_base}.zip")


def main():
    ensure_pyinstaller()
    fetch_adb()
    run_pyinstaller()
    copy_extras()
    make_zip()


if __name__ == "__main__":
    main()
