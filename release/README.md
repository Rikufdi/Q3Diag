# Building a release

```powershell
python release/build_release.py
```

Produces a self-contained `release/dist/Q3Diag-Wizard/` folder (and a matching `.zip`) with the
wizard, its bundled Python runtime, the PowerShell samplers, and a vendored `adb.exe` fallback —
see [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) for why bundling `adb.exe` is fine
license-wise. Nothing else in this repo needs to change to build a new release; the script always
packages whatever's currently in `tools/`.

**Run it from `release/dist/Q3Diag-Wizard/`, not `release/build/`.** Both contain a
`Q3Diag-Wizard.exe`-named file, but `release/build/` is PyInstaller's intermediate working
directory -- incomplete, not what actually ships. Easy mix-up (confirmed live: it launches, then
closes immediately with no visible error, which looks identical to a crash). Only `dist/` is the
finished, runnable package.

Requires `pyinstaller` (installed automatically if missing) and, the first time, internet access to
download platform-tools (cached afterward in the git-ignored `release/build/`).

This produces a **beta**: it's been smoke-tested against a real headset, but hasn't been run
through a from-scratch install on a machine that never had this repo's dev environment on it. If
you hit something that only breaks in the packaged build (not `python tools/wizard.py` from a
normal checkout), that's exactly the kind of thing worth reporting.

## TODO before/at the first GitHub release

- **Submit the exe to Microsoft's false-positive portal**: https://www.microsoft.com/en-us/wdsi/filesubmission.
  Unsigned PyInstaller binaries routinely get flagged by Defender's ML heuristic
  (`Trojan:Win32/Wacatac.C!ml` specifically, seen on the 2026-09-17 beta build) — `--noupx` and the
  embedded version resource (both already in this build script) are known mitigations but don't
  reliably clear it on their own; only a submitted-and-whitelisted hash or a paid Authenticode
  signing certificate reliably does. Do this submission against the *actual* file you're about to
  publish, since the hash changes on every rebuild — a submission made now against a beta build
  won't cover the release build later.
