# Third-party notices

This project's own code is MIT-licensed (`tools/**`, see [`LICENSE`](LICENSE)) or CC BY 4.0
(data/write-ups, see [`LICENSE-DATA`](LICENSE-DATA)). Packaged releases additionally bundle one
third-party binary:

## adb (Android Debug Bridge)

- **What**: `adb.exe`, `AdbWinApi.dll`, `AdbWinUsbApi.dll` — from Google's Android SDK Platform
  Tools.
- **Why it's bundled**: so a packaged release works without a separate Android platform-tools
  install. If you already have `adb` on `PATH` or configured in `tools/site.json`, that's used
  instead — the bundled copy is only a fallback.
- **License**: `adb` itself (part of the Android Open Source Project, `platform/system/core`) is
  licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0). The
  Android SDK's own terms restrict redistribution of the SDK as a whole, but explicitly carve out
  an exception for components covered by their own third-party license — which Apache 2.0 on `adb`
  is. The full notice as shipped by Google is included verbatim in every packaged release as
  `NOTICE.txt` alongside the binaries (also archived here: `release/build/platform-tools/NOTICE.txt`
  after running the build script).
- **Source**: <https://android.googlesource.com/platform/system/core/> · downloaded from
  <https://developer.android.com/tools/releases/platform-tools> by `release/build_release.py`,
  not modified.
- **Not affiliated with or endorsed by Google or the Android Open Source Project.**

**Not bundled, on purpose**: PresentMon and iperf3, both used elsewhere in this project, are
deliberately left out of packaged releases — neither is needed for full use of the tool, and every
extra bundled binary is another thing for antivirus tools to flag (see `release/README.md`'s TODO
on that). PresentMon (optional PC-side game frame-time capture) auto-detects a copy you place
yourself as `PresentMon.exe` next to the app (`qsite.presentmon_exe()`); `monitor()` prints where
to get one if it's missing. iperf3 is used only by the optional link-capacity check
(`tools/linkcheck.py`), and it is not bundled either — that check needs a user-supplied Windows
client plus an aarch64 Android build to run on the headset.

This project is not affiliated with, endorsed by, or sponsored by Google, Meta, or Virtual Desktop
(Guy Godin / VRD LLC). Product names are used for identification purposes only.
