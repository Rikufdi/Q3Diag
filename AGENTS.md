# AGENTS.md

Instructions for an AI agent working with this tool: either in this repository, or in an installed
copy (a release folder next to `Q3Diag-Wizard.exe`). Humans read `README.md`; this file is the
operational detail an agent needs to collect data and to help someone set the tool up.

## What the tool is

Windows-only diagnostics for wireless PCVR over Virtual Desktop or Air Link. It records a real play
session (headset Wi-Fi counters, thermals, compositor frames, the PC's encoder/GPU/DPC/memory state,
the game process, ping, optionally PresentMon frame times), reduces it, and compares the result
against a saved baseline for the same configuration, naming the metrics that drifted.

## Entry points

| Command | What it does |
|---|---|
| `Q3Diag-Wizard.exe` / `python tools/wizard.py` | the supported entry point: connect, Quick Test or Advanced, optional iperf3 link check, optional WPR trace, session, verdict, baseline prompt |
| `python tools/cell.py monitor <run_id> [max_seconds]` | the measurement core on its own, no wizard (dashboard not served) |
| `python tools/cell.py stop <run_id>` | ask a running monitor to shut down cleanly (drops `runs/<id>/.stop`) |
| `python tools/cell.py results <run_id>` | reduce a finished run into `results.json` (+ a row in `results.csv`) |
| `python tools/cell.py fingerprint <run_id> [--save-baseline]` | diff against the saved baseline, or save this run as it |
| `python tools/cell.py watch <run_id> [interval]` | live alerting on retry rate / RSSI drift |
| `python tools/cell.py linktest` | standalone RSSI/retry watcher, no stream needed |
| `python tools/dashboard.py <run_id>` | serve the live dashboard standalone |
| `powershell -File tools/Quest-Probe.ps1` | read-only capability snapshot of a connected headset |

`cell.py` also offers `serial`, `capture`, `passive` and `layer`. Run it with no arguments for the
usage block.

## Where data goes

`base_dir` is the repository root in a checkout, and the folder containing the exe in a release. All
run data is under `<base_dir>/runs/<run_id>/`. Nothing is uploaded anywhere.

| File | Contents |
|---|---|
| `settings.json` | the run's identity: stack, codec, bitrate, content, band, Bluetooth state |
| `session.json`, `clock.json` | detected stream start/end segments; headset to PC clock offset |
| `quest_wifi_samples.tsv` | headset MAC counters: link rate, RSSI, retries, losses, band |
| `quest_net_samples.tsv` | `/proc/net/dev` and `/proc/net/snmp`: wlan0/p2p0 bytes, errors, TCP retransmits |
| `quest_env_samples.tsv` | radio state, SoC/GPU/CPU thermals, GPU busy, mount state |
| `sf_latency_samples.tsv`, `sf_layers.log` | SurfaceFlinger panel latency and the active layer |
| `cm_wifi_snapshots.txt` | `dumpsys` controller-link / P2P state changes |
| `headset_logcat.txt` | filtered headset logcat: `VrApi` compositor fps / stale / tear / early frames, predicted period, quality-scaling state (`DpuScale`) and dropped-frame counters, plus `QC2Comp` decoder stats whose instance name carries the decoded codec. The streaming-client tags (`VirtualDesktop.Android`, `OVRMediaCodec`, `VR_Engine`, `ALVR` — see `headset_log_tags`) are captured opportunistically: on the reference rig VD's own tag emits only SELinux audit lines, so client-side bitrate/connection telemetry does not exist there |
| `pc_samples.tsv` | NVENC and GPU use, TCP retransmits, DPC/ISR per core, memory, game process CPU / working set / thread-wait histogram |
| `ping_samples.txt` | 1 Hz PC to headset ICMP |
| `ovr_metrics.csv` | Oculus Metrics CSV pulled from the headset |
| `presentmon.csv` | optional PC game frame times (PresentMon) |
| `linkcheck.json`, `linkcheck/` | optional iperf3 link check: TCP both ways, UDP ramp, plus the raw JSON per test |
| `trace.etl`, `trace-state.json` | optional Windows Performance Recorder trace and its size/window metadata |
| `results.json` | the reduced run: identity fields, per-metric readings, notes |
| `fingerprint_diff.json` | this run's drift against its baseline, if one existed |
| `codec_events.tsv`, `decay_episodes.tsv`, `controller_events.tsv` | event tables written by `results` |

Other locations:

- `<base_dir>/results.csv` is the local aggregate, one row per run. Private runs (run ids starting
  with `priv_`) are excluded from it, and the wizard names every run `priv_*`.
- `<base_dir>/baseline/fingerprints/<tag>.json` holds saved baselines, keyed by
  stack/codec/bitrate/content/band/Bluetooth.
- Probe snapshots go to `<base_dir>/probe/<timestamp>/`.
- A WPR trace is staged in `%TEMP%` while recording and moved into the run folder at the end, so its
  peak cost lands on the system drive, not only on `base_dir`.

Sizes per minute of session (see `README.md` for the table): samplers about 0.15 MB, PresentMon about
2.5 MB, WPR trace about 1.3 GB. The trace is the only one that can fill a disk by itself.

## Collecting a session

1. Ask for the headset to be available and awake. `python tools/cell.py serial` prints the wireless
   adb endpoint, or tells you it cannot find the headset.
2. If adb reports `offline`, `adb disconnect <ip>:5555` then `adb connect <ip>:5555` usually fixes it.
   If the headset has rebooted, or `adb devices` shows nothing at all, it needs one USB connection:
   the wizard does that handoff and writes the headset's IP into `site.json` as a side effect.
3. Run the wizard. Quick Test needs no decisions; Advanced asks for a target bitrate and content type
   so runs stay comparable. Both detect stack and band from the run afterwards.
4. Declining every optional prompt produces a small, unperturbed session. The WPR trace is the one
   option that changes what it measures, so keep traced sessions short and do not compare them with
   untraced ones.
5. The session ends on Enter, `q`, `Ctrl+C`, or its time limit. The stop key is ignored for the first
   8 seconds on purpose, and the tool says when it is armed.
6. After the session read `results.json` and `fingerprint_diff.json` first, then reach for the raw
   TSVs. `results.json` carries `pc_game_fps_note` and `headset_data_note` when something about the
   run was not measurable, which is worth reading before drawing conclusions.

## Configuration

`site.json` sits next to the exe in a release, and in `tools/` in a checkout. `site.example.json` is
the template. Every key has an environment override (`QUEST3_<KEY>`), and `quest_ip` is discovered
automatically on the first USB connection. A stale `quest_ip` is a common time-waster: it makes the
wizard fall back to the USB path even when the headset is on Wi-Fi under a new address, so check it
before concluding the headset is unreachable.

Two optional third-party tools are user-supplied and never bundled. PresentMon (`vendor/PresentMon.exe`,
or `presentmon_exe`) adds the game's own frame times. iperf3 needs both a Windows client and an
aarch64 Android build in `vendor/` for the link check. `release/vendor_notes/*.txt` are the notes a
release ships to explain exactly that.

## Rules

- Never commit run data. `runs/`, `baseline/`, `probe/` and `results.csv` are git-ignored by design,
  and captured device data should not be published from this repository.
- Do not hand-redact captured artifacts. Identifiers are replaced with ordinals (`mac1`, `ip1`,
  `ssid1`) at write time and again at teardown by `cell.redact_artifacts()`. The Quest's own
  `192.168.49.0/24` controller-link subnet is deliberately preserved: it is a device constant.
- Do not bundle third-party binaries into a release. Extra executables in the download make antivirus
  false positives more likely, which is why adb is the only one shipped.
- Analytics live outside the run folder. Anything worth keeping belongs in a document, not in a
  tracked copy of the device's own output.
- The tool is Windows-only, and `msvcrt`, `nvidia-smi`, PowerShell and adb are assumed to be present
  in the ways `qsite.py` resolves them.
