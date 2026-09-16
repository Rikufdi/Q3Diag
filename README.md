# Quest 3 PCVR Wi-Fi streaming diagnostics

Instrumentation and results for a measurement study of the wireless leg in PCVR streaming:
**PC → 2.5 GbE → TP-Link Archer BE550 (access point) → 6 GHz → Meta Quest 3**, under load from
Virtual Desktop 1.34.22 (VDXR) and Meta Air Link.

The question was practical: *where does wireless PCVR actually break down — the link, the encoder,
the transport, or the headset?* The answer, over 15 matrix cells and ~4 h of monitored gameplay, is
that this link never became the bottleneck, and two of the community's favourite failure modes did
not reproduce. Negative results and the corrections that followed are in
[`findings.md`](findings.md); the measured tables are in [`report.md`](report.md).

**This is Windows-only.** The harness drives Windows-specific instrumentation — `pktmon` for packet
capture, PowerShell for the samplers, `nvidia-smi` and Windows performance counters for the encoder
side. The headset side is plain `adb` and would port; the host side would not. See
[Requirements](#requirements).

---

## Headline results

| | |
|---|---|
| 6 GHz TCP downlink (PC → headset) | **1050 Mbps**, zero retransmits |
| 6 GHz TCP uplink | 717 Mbps (UDP send is headset-CPU-capped at ~495 Mbps) |
| 5 GHz radio of the same AP | 17–29 Mbps — **defective**, persists across width/channel/reboot |
| VD H.264+ at 500 Mbps | 492.51 Mbps measured on the wire, **0 % loss** |
| VD HEVC 10-bit / AV1 10-bit | 200 Mbps each, **0 % loss** |
| Air Link H.264 | capped at ~200 Mbps; 350 overrides fall back to ~15 Mbps |
| Loss knee | **none** — no cell exceeded 0.09 % loss anywhere in the matrix |
| Reported "20–40 min bitrate decay" | **did not reproduce** in any of ~4 h recorded session |

Four findings worth calling out, all documented with raw evidence:

1. **VD 1.34.22 streams over TCP, not UDP.** The original capture and accounting assumed UDP; the
   tooling was corrected to `tcp.len + udp.length`.
2. **The "20–40 min decay" was an operator reading with no artifact behind it.** The saved capture
   for the cell in question measures 349.88 Mbps against a 350 cap. The most likely origin is VD's
   *by-design* bitrate re-mapping after a codec cycle — which is also why the measurement rule is
   "set codec first, then bitrate".
3. **Quest Pro controllers do not use the 5 GHz Wi-Fi link.** They run a P2P group on 2462 MHz
   (2.4 GHz ch 11), so the AP's 2.4 GHz radio is their real near-neighbour. `p2p0` counters and
   `dumpsys cm_wifi` are the instruments for that path.
4. **The monitoring rig has a measurable cost**: +5.8 CPU points on the headset and +0.4 Mbps
   (0.07 %) of wire traffic, and it does *not* hold the radio out of power save.

---

## Layout

```
report.md          results: matrices, live sessions, deviating findings
findings.md        open findings, corrections, version timeline
results.csv        one row per executed run (138 columns)
instrumentation.json  device identity + which probes the headset supports

tools/             the harness (see below), plus QUEST-AGENT-PLAYBOOK.md
runs/<run_id>/     per-run artifacts: samplers' TSVs, OVR CSV, VrApi logcat, results.json
probe/<ts>/        read-only device snapshots + agent findings
baseline/          iperf3 ramps, ping/AP-ping, 5 GHz vs 6 GHz comparison
topology/          AP config, radio settings, environment lock
```

### Harness

| file | role |
|---|---|
| `tools/cell.py` | the workhorse: `serial`, `layer`, `capture`, `monitor`, `watch`, `passive`, `results`, `fingerprint`, `linktest` |
| `tools/Sample-Quest.ps1` | headset sampler: MAC counters, `/proc/net/*`, radio state, thermals |
| `tools/Sample-PC.ps1` | PC sampler: NVENC/GPU utilisation, windows TCP retransmits, streamer RSS |
| `tools/Sample-GameFPS.ps1` | optional PC-side game frame-time capture via PresentMon (not vendored) |
| `tools/dashboard.py` | live local web dashboard for a running `monitor` session |
| `tools/Quest-Probe.ps1` | read-only capability snapshot of a connected headset |
| `tools/Run-Cell.ps1` | one-cell orchestration for the pcap matrix (phase 1) |
| `tools/analyze.py` | pcap reduction via `tshark` |
| `tools/elevated-do.ps1`, `tools/elevated-manager.ps1` | helper that runs `pktmon` through a scheduled task |
| `tools/qsite.py`, `tools/Site.ps1`, `tools/site.example.json` | site configuration (below) |
| `tools/QUEST-AGENT-PLAYBOOK.md` | the operating manual: what the headset exposes, what is read-only, what needs approval |

`QUEST-AGENT-PLAYBOOK.md` is the best entry point for understanding the project — it is a map of
everything a Quest 3 exposes to `adb` without root, including the dead ends, written so an agent or
a human can pick the rig up cold.

---

## Requirements

- **Windows 10/11 host.** `pktmon` (built in), PowerShell 5.1+, Windows `TCPv4` performance counters.
- **Python 3.10+** for `cell.py` / `analyze.py`.
- **Android platform-tools** (`adb`) on PATH or configured in `site.json`.
- **Wireshark** (for `tshark`, used by the phase-1 pcap path and `analyze.py`). Only needed if you
  are reducing captures.
- **NVIDIA GPU + `nvidia-smi`** for the PC-side encoder sampler.
- **Virtual Desktop Streamer 1.34.22** and/or **Meta Link** — the streaming stacks under test.
- **Meta Quest 3** with wireless debugging enabled (Settings → Developer).
- *Optional* — **[PresentMon](https://github.com/GameTechDev/PresentMon/releases/latest)** (the
  console-app build, e.g. `PresentMon-2.5.1-x64.exe`) for PC **game** frame-time capture during
  `monitor` — download the `.exe`, save it anywhere (e.g. `tools/vendor/`, which is git-ignored), and
  set `presentmon_exe` to that path in `tools/site.json`. Leave it unset to skip this sampler entirely;
  everything else in the harness works without it. No install step beyond that — it's a standalone
  console exe, not a service.

## Setup

**On the headset, before any of this works:**

1. Enable **Developer Mode** — requires a (free) Meta Horizon developer organization; toggled from the
   Meta Horizon mobile app under Menu → Devices → \<your headset\> → Developer Mode. This is a one-time,
   account-level step and has nothing to do with adb itself.
2. In the headset, **Settings → System → Developer**, turn on **USB Connection Dialog** and
   **Wireless Debugging**.
3. **Connect the headset to the PC with a USB-C cable at least once.** Put the headset on — it will show
   two separate permission dialogs the first time: **"Allow USB debugging?"** (check "always allow from
   this computer" so this doesn't repeat) and **"Allow access to device data?"** (MTP/file access —
   accept this too; some Windows USB driver stacks won't finish enumerating the ADB interface without
   it). Both need to be accepted *in the headset*, so you have to be wearing it, or at least able to see
   the panel, for this one-time step.

Only after that is wireless adb possible at all — the actual connection setup is:

```powershell
git clone <your-fork> ; cd Q3Diag

# 1. describe this machine to the harness
Copy-Item tools/site.example.json tools/site.json
notepad tools/site.json        # fill in quest_ip; leave tools you already have on PATH empty

# 2. attach the headset (after the one-time USB step above)
adb pair <ip>:<port>           # once, from the headset's Settings -> Developer -> Wireless Debugging dialog
python tools/cell.py serial    # resolves the endpoint (5555, then mDNS)

# 3. sanity check: read-only capability snapshot
powershell -NoProfile -ExecutionPolicy Bypass -File tools/Quest-Probe.ps1
```

### Troubleshooting: "no Quest reachable" / adb sees nothing

Wireless debugging is flaky by design — it drops on every headset reboot and on some Wi-Fi Debugging
toggles (see `findings.md`), so this happens routinely, not just at first setup:

1. Plug the headset in over USB-C. Put it on and accept the debugging dialog if it reappears.
2. `adb devices` should now show the headset as `device` (over USB). If it shows **nothing at all**
   despite Windows clearly seeing the headset (Device Manager → the headset appears as a composite USB
   device with an "ADB Interface"), the adb **server** is the usual culprit, not the cable: a stale
   `adb.exe` server process from an earlier session can sit there indefinitely without rescanning USB.
   Fix: `adb kill-server` then `adb devices` (which auto-starts a fresh server and rescans).
3. Once the headset shows up over USB: `adb tcpip 5555` re-arms wireless debugging for this boot, then
   unplug the cable. `python tools/cell.py serial` (or any harness command) will find it over Wi-Fi from
   here via `:5555` first, then mDNS.

`tools/site.json` is git-ignored — every machine-specific value lives there, and every harness
script resolves through it (`qsite.path("tshark")` in Python, `Resolve-SiteTool $Site 'tshark'` in
PowerShell). Any key can be overridden per-invocation with `QUEST3_<KEY>`, e.g.
`QUEST3_QUEST_IP=192.0.2.10 python tools/cell.py serial`. Empty keys fall back to PATH and the
standard install locations, then fail with instructions.

## Running a monitored session

```bash
python tools/cell.py monitor <run_id> 10800     # 3 h of sampling; survives across shells
python tools/cell.py watch   <run_id> 30        # optional: alert on state change only
python tools/cell.py results <run_id>           # reduce -> results.json + a row in results.csv

# zero-perturbation control arm: no adb traffic during play at all
python tools/cell.py passive <run_id> start     # before play
python tools/cell.py passive <run_id> end       # after play
```

### Live dashboard, fingerprinting, and link testing

```bash
# watch a live monitor session in a browser (poll the same TSVs monitor is writing)
python tools/dashboard.py <run_id>              # -> http://127.0.0.1:8765/

# after `results`, compare this run against the saved baseline for its configuration
# (same stack/codec/bitrate/band) and report which metrics drifted
python tools/cell.py fingerprint <run_id>                    # diff vs. saved baseline
python tools/cell.py fingerprint <run_id> --save-baseline    # (re-)establish the baseline instead

# live RSSI/retry watch for physically testing AP/headset placement or cable routing -- no
# monitor/stream needed. Plays a soft descending chime when the link degrades past threshold, and an
# ascending one on recovery, so you can move hardware around without staring at the terminal.
python tools/cell.py linktest                   # add --no-beep to silence it

# `watch` (used during `monitor`) can beep too, for testing placement during a real stream:
python tools/cell.py watch <run_id> 10 --beep
```

![Live dashboard for a running `monitor` session](docs/dashboard.jpg)

The PC chime plays through the standard Windows audio (multimedia) path, so it should reach whatever
your default playback device is, however unusual the setup. Both commands also accept
`--headset-beep`, but be aware of what it actually does: this Horizon OS build's `cmd notification post`
has no way to attach sound/vibration to a shell-posted notification, so it only leaves a silent,
timestamped entry in the headset's notification history — useful for correlating a degradation event
after the fact, not as a live alert. See `findings.md` for how that was confirmed.

`fingerprint`'s baselines live in `baseline/fingerprints/<tag>.json` (git-ignored: they're this rig's
own "current normal", not a portable result) and the per-run diff is written to
`runs/<run_id>/fingerprint_diff.json`; `dashboard.py` surfaces any flagged drift as a banner if that
file exists for the run it's watching.

PC-side game frame rate (as opposed to the headset compositor's rate, which is all the other samplers
see) is optional: set `presentmon_exe` in `tools/site.json` to a downloaded
[PresentMon](https://github.com/GameTechDev/PresentMon) build and `monitor` captures it automatically;
`results` reduces it into `pc_game_fps_mean` / `pc_game_fps_1pct_low` / etc.

Per-run outputs are listed in `report.md` and in the playbook. Two operational rules that were
learned the hard way:

- **Windows file locks.** The PowerShell samplers briefly hold their TSVs open; readers must retry.
  `cell.py` does (`_read_text` / `read_tsv`) — do not open those files with a bare `open()`.
- **`hub stop` / process kill hard-kills.** A monitor's `finally` may not run, so the clock and cell
  window are written at *start*. Do not move that write into cleanup.

## Data policy

The repo carries the **reductions and the text telemetry** (~35 MB), not the raw byte streams:

| excluded | why |
|---|---|
| `runs/*/cap.pcapng`, `cap.etl` (875 MB) | regenerable by re-running a cell; no diff value |
| `probe/**/raw/`, `probe/**/*-raw/` | raw `adb` dumps; regenerate with `Quest-Probe.ps1` |
| `runs/*/pc_config.json` | contains Virtual Desktop's DPAPI blobs (`ProtectedComputerID`, account tokens) — machine-bound secrets. A redacted copy is tracked as `pc_config.redacted.json` |
| `topology/ap-radio.png` | the AP administration page renders the Wi-Fi passphrase in plaintext |
| `tools/iperf3.*` | a third-party aarch64 Android build (BSD-3-Clause); fetch or build your own |

**Identifiers are redacted** in everything that ships: headset and controller serials, MAC
addresses/BSSIDs, the desktop hostname and the Wi-Fi passphrase are replaced with `<placeholders>`;
neighbouring networks seen in scan lists are redacted too. The measurements themselves are
untouched.

## Third-party

- `iperf3` (baseline ramps) is BSD-3-Clause; the binary is deliberately not vendored.
- The Virtual Desktop codec enumeration in `tools/vd-codec-enum.md` was read out of
  `VirtualDesktop.Streamer.exe` with reflection; it is documented for measurement reproducibility
  and is not affiliated with or endorsed by Virtual Desktop or Meta.

## License

Split by content type:

- **Code** — `tools/**` (the harness) is **MIT**: see [`LICENSE`](LICENSE).
- **Data and write-ups** — `report.md`, `findings.md`, `results.csv`, `runs/**`, `probe/**`,
  `baseline/**`, `topology/**`, and the documentation parts of `tools/` are **CC BY 4.0**: see
  [`LICENSE-DATA`](LICENSE-DATA).

If you use the data, attribute it as:

> "Quest 3 PCVR Wi-Fi streaming diagnostics" by Rikufdi, licensed CC BY 4.0.
