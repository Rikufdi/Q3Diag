# Quest 3 over wireless adb — agent playbook

How any agent (or human) pokes around the headset attached to this project.
Companion tool: `tools/Quest-Probe.ps1` (read-only battery, writes a snapshot dir).

## 1. Connect

Every machine-specific path/IP lives in `tools/site.json` (git-ignored; copy
`tools/site.example.json` and fill it in). The scripts resolve it themselves, e.g.:

```powershell
. tools\Site.ps1
$Site = Get-Site
$adb  = Resolve-SiteTool $Site 'adb' @("$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe")
$ip   = Get-SiteValue $Site 'quest_ip'
& $adb devices -l
```

Python side: `import qsite; qsite.path("adb")`, `qsite.get("quest_ip")`. Any key can be
overridden per-invocation with `QUEST3_<KEY>` (e.g. `QUEST3_QUEST_IP=192.0.2.10`).

Wireless adb drops off whenever the headset reboots or wireless debugging is toggled. The old
`tcpip 5555` link is usually dead; the Android 11+ Wireless Debugging endpoint advertises over mDNS:

```
& $adb mdns services        # find  _adb-tls-connect._tcp   <ip>:<port>
& $adb connect <quest-ip>:<port>
```

`tools/Quest-Probe.ps1` does this automatically (5555 first, then mDNS). If the port changed and
nothing is advertised, the headset needs `adb tcpip 5555` over USB — a physical action, ask the user.

Device facts: model `Quest_3` (eureka), Horizon OS `UP1A.231005.007.A1`, Android 14, kernel 5.10.246,
SoC SXR2230P (Snapdragon XR2 Gen 2), 8 GB RAM, 6 GHz STA at 160 MHz / 2401 Mbps.
Streaming clients: Virtual Desktop = `VirtualDesktop.Android`, Air Link = `com.oculus.xrstreamingclient`.

## 2. Hard rules

**Read-only by default.** Safe: `getprop`, `dumpsys *`, `cmd wifi status|list-scan-results|get-allowed-channel`,
`cat /proc/net/*`, `logcat -d`, `ls`, `df`, `pm list packages`, `dumpsys SurfaceFlinger --list|--latency`,
`dumpsys gfxinfo <pkg>`, `dumpsys thermalservice`, `dumpsys OculusWifi`, `dumpsys Strata`.

**Never run without asking the user first** (device-state mutations / user-visible effects):

| command | why it needs approval |
|---|---|
| `settings put` / `device_config put` | changes persisted device state |
| `am force-stop` / `am start` / `input *` | kills or drives the user's session, can drop a live stream |
| `cmd wifi set-*`, `start-scan`, `forget-network` | radio/network state |
| `adb push` / `shell rm` / `shell mv` | writes to the device |
| `reboot`, `svc power`, `screenrecord`, `screencap` | disruptive or writes to device storage |
| `pm uninstall` / `pm disable` | destructive |
| creating/starting an elevated scheduled task (`PCVR-Elev`, `elevated-do.ps1`, `elevated-manager.ps1`) | the user deliberately removed the elevated task; NEVER re-register it or run the elevated helper without an explicit request |

Also: never experiment while the user is mid-session (VD/Air Link streaming) unless the experiment *is*
the session — killing adbd or the radio mid-stream ruins a run.

**Record everything.** Write every finding as *claim -> command -> verbatim output -> file*. Durable
evidence goes to a file; raw outputs worth keeping go under `probe/<snapshot>/agent-<slice>-raw/`.

## 3. Where things are

| path | contents |
|---|---|
| `probe/<ts>/raw/*.txt` | one file per probe from `Quest-Probe.ps1` |
| `probe/<ts>/SUMMARY.md` | identity + capability roll-up + hottest thermal zones |
| `probe/<ts>/agent-<slice>.md` | agent findings (this playbook's output format below) |
| `tools/Quest-Probe.ps1` | the read-only battery; re-run for a fresh baseline |
| `runs/<run_id>/` | per-cell streaming artifacts (cap.pcapng, OVR CSV, wifi samples) |

Findings format:

```markdown
# <slice> findings
## <finding title>
- claim: <one sentence, falsifiable>
- command: <exact adb command>
- evidence: <verbatim lines or file path + line numbers>
- confidence: measured | inferred
## Dead ends
- <command> -> <what happened>
## Follow-ups for the user
- <command or experiment that needs approval>
```

## 4. Verified capabilities (probed 2026-09-16)

### Radio / link
- `cmd wifi status` — WifiInfo: RSSI, link speeds, freq, **and MAC counters** `successfulTxPackets`,
  `retriedTxPackets`, `lostTxPackets`, `successfulRxPackets` (+ per-second rates). This is the counter
  set the streaming harness reduces. Semantics: retry = 802.11 MPDU retransmission (ACK/BlockAck missed),
  lost = retry limit exhausted. Uplink direction only (headset TX).
- `dumpsys wifi` (~1.2 MB) — contains `WifiScoreReport`, a per-3s CSV: `time,session,netid,rssi,
  filtered_rssi,rssi_threshold,freq,txLinkSpeed,rxLinkSpeed,txTput,rxTput,bcnCnt,tx_good,tx_retry,tx_bad,
  rx_pps,nudrq,nuds,s1,s2,score`. Whole-session radio history (~8.6k rows, newest first). Extract the head:
  `adb shell "dumpsys wifi | grep -A3 '^WifiScoreReport'"` — **not** a `sed -n '/WifiScoreReport/,/^$/p'`
  range (the CSV starts on the line after the header; the sed range runs far past it). The head timestamp
  does not advance while the headset is idle, so a moving head row is itself an activity signal. Also
  contains link-layer config, scan/roam/supplicant state, P2P events.
- `dumpsys OculusWifi` — vendor radio policy state: STA freq/RSSI/bandwidth, **STA TX power (dBm)**,
  TxPowerManager / TxChainManager / CoexManager / ChannelSwitchManager / PowerJumpDetectManager /
  RegdomManager, per-manager thread ids. Explains uplink behaviour the Android layer hides.
- `cmd wifi` — full command list (8.8 KB). Useful read-only: `status`, `list-scan-results`, `list-networks`,
  `get-allowed-channel`, `get-country-code`, `is-verbose-logging`, `get-coex-cell-channels`.
- `cmd wifi list-scan-results` — neighbours incl. 6 GHz; use for congestion context.
- `/proc/net/dev`, `/proc/net/snmp`, `/proc/net/tcp`, `/proc/net/udp` — per-interface bytes/errors/drops,
  device-wide TCP counters (RetransSegs etc.), per-socket table incl. tx/rx queues. **No root needed.**
- `dumpsys netstats`, `dumpsys connectivity` — per-uid traffic, IP/route/wake state.

### Thermal / power
- `dumpsys thermalservice` — 104 zones with current temps: `cpu-*`, `gpuss-*` (GPU), `soc-usr`, `nspss-*`,
  `pcb*`, `rf-usr`, `batt-virt-usr`, `surf-virt-usr`, cams, usb. Plus throttling thresholds
  (CPU/GPU 89/92/95 °C, `surf-virt-usr` 48/50/52 °C) and thermal status level.
- `dumpsys FanMonitorService` — `fan status:normal warnings:0`, `pwm-tach-fan0:normal` (active cooling).
- `dumpsys battery`, `dumpsys power` — battery temp/current, wake locks, power gates.
- **Dead end:** `/sys/class/thermal/*` is permission-denied for shell (needs root).

### Compositor / frame pacing
- `dumpsys SurfaceFlinger --list` — every layer name incl. `#id` suffixes (needed verbatim for `--latency`).
- `dumpsys SurfaceFlinger --latency <layer>` — 3-column ns table (desiredPresent / actualPresent /
  frameReady); first line is the refresh period (`11111111` = 90 Hz). All-zero rows = that layer had no
  frames in the window. Works for panel/app layers; **per-frame timing for a running VR title is the
  open question** (see `--latency` on the vrshell layer while streaming).
- `dumpsys Strata` — Oculus compositor: physical display, **resolution 4128x2208, refresh 90 Hz**,
  HWC layer list (`vr_compositor_*`, `vr_compositor_with_depth_test_*`, `vr_compositor_protected_*`,
  with pid and haveBuffer), current/next strata layer, `ovrState` flags, vsyncEnabled.
- `dumpsys gfxinfo <pkg>` — per-app frame stats; requires the app to be running
  (`VirtualDesktop.Android`, `com.oculus.xrstreamingclient`). Not validated yet.
- **Dead end (verified):** `adb exec-out screencap -p` returns a valid 4128x2208 PNG that is entirely
  black — SurfaceFlinger's 2D capture does not include the VR compositor layers, so it cannot be used to
  see headset content while VR is compositing (it only shows 2D panels if one is in focus). Any capture
  that writes on-device (`screenrecord`) is approval-gated and shares the same path.
- `dumpsys media.player` (~44 KB), `dumpsys media.metrics` — codec/decoder side.
- **Dead ends:** `dumpsys media.codec` ("Can't find service"), `dumpsys snapvrs` (idle, 0 requests),
  `dumpsys guardian`, `dumpsys HologramService`, `dumpsys DiagnosticsCollectorService`,
  `dumpsys OVRMetricsService`, `dumpsys wificond` -> empty; `dmesg` -> permission denied.
- `dumpsys OVRMetricsService` being empty is notable: the OVR metrics CSV path
  (`/sdcard/Android/data/com.oculus.ovrmonitormetricsservice/files/CapturedMetrics`) is populated by the
  app, configured in-headset (it does not log Air Link's foreground app — the known telemetry gap).

### Controller link (Quest Pro controllers / map share)
- `/proc/net/dev` → **`p2p0`** carries the controller link. Its bytes/errs/drops are the direct link-health
  counter set (`Sample-Quest.ps1` now records p2p0 alongside wlan0 in `quest_net_samples.tsv`).
- `dumpsys cm_wifi` (~19 KB, sampled every 30 s into `cm_wifi_snapshots.txt`) holds the sections that matter:
  - `=== Controller Status History ===` — per controller (LeftHand/RightHand, serial, device id) the full
    state history with timestamps: `CONNECTED_ACTIVE`, `CONNECTED_INACTIVE`, `CONNECTING`, `SEARCHING`,
    `DISABLED` (+ `errors: NEEDS_VERSION_CHECK`). Drops/reconnects show up here.
  - `=== Controller Connection History ===` — RPC dials, IP-connectivity confirmations, `onApConnected()`,
    `onConcurrencyModeChanged(RSDB, "<ssid>", <bssid>)`, `Start P2P Wifi`.
  - P2P/WLAN events — `P2P_GO_CREATE_ATTEMPT/SUCCESS frequency: <mhz>`, `P2P_CHANNEL_SWITCH`,
    `P2P_GC_CONNECT macAddress: … frequency: …`, `LOW_LATENCY ON/OFF` (streaming state), plus `WIFI_HEADSET_*`.
  - `=== Tether/P2P band info ===` — selected band/channel; `Was forced to 2.4GHz due to CC or channel: false`.
  - Observed on this headset: P2P GO on **2462 MHz (2.4 GHz ch11)**, `Concurrency Mode: RSDB`, clients
    `<ctrl-mac-left>` / `<ctrl-mac-right>`, map-share chunks (693 KiB) every 60 s.
- `dumpsys oculus.internal.tracking.ITrackingService/default` (~60 KB) — controller tracking state
  (`Enabled/Confident`), per-device in-hand signals, `registerRemote` events, and the HMD health report
  (IMU rate/dropped %, magnetometer). Controller slots `#0..#3` are the legacy direct-link slots and stay
  "Not Registered" with Quest Pro controllers (they arrive as P2P remotes instead).
- `dumpsys oculus.internal.ITrackingFidelityService/default` — tracking fidelity levels / mux mode.

### Inventory / misc
- `cmd -l`, `dumpsys -l` — byte-identical service-name dumps (md5 `7aa6d131676421af5bf1ea0ceff06aab`, ~370
  services, incl. vendor: `OculusWifi`, `OVRMetricsService`, `PerfStream`, `snapvrs`, `Strata`,
  `HologramService`, `FanMonitorService`, `Guardian`, `SensorProxy*`, `XrspBroker`, `hzplatform`,
  `OculusWindowManager`, `DiagnosticsCollectorService`). They say **nothing** about shell-command
  availability — probe per service with `cmd <svc> -h`. Vendor services: none accept `cmd` subcommands
  (`No shell command implementation.` or a binder failure); `dumpsys <svc>` is the only read path.
- `pm list packages [-3]`, `dumpsys activity top`, `dumpsys cpuinfo`, `dumpsys meminfo`, `df -h`,
  `settings list global|secure`, `device_config list <namespace>`, `logcat -d -b all -t N`.

## 6. Running a monitored session (the phase-2 workflow)

```powershell
# start (survives across turns; self-terminates after N seconds; cleans up its children)
hub start  application=<python.exe> args=["tools/cell.py","monitor","<run_id>","10800"]   # or:
python tools/cell.py monitor <run_id> 10800

# optional unattended alerting (prints only on state change + a 5-min heartbeat)
python tools/cell.py watch <run_id> 30

# reduce (writes results.json + a row in results.csv; pulls the OVR CSV with a staleness gate)
python tools/cell.py results <run_id>

# zero-perturbation control arm (no adb traffic during play; session measured from the OVR CSV afterwards)
python tools/cell.py passive <run_id> start      # before play
python tools/cell.py passive <run_id> end        # after
```

Per-run files written by `monitor` (all in `runs/<run_id>/`):

| file | content | cadence |
|---|---|---|
| `quest_wifi_samples.tsv` | RSSI, link speed, MAC counters (tx/retry/lost/rx) | 2 s |
| `quest_net_samples.tsv` | wlan0 **and p2p0** bytes/errs/drops, TCP in/out/retrans/RST | 2 s |
| `quest_env_samples.tsv` | OculusWifi STA state (TX power), thermal zones, GPU busy %, **headset mount state** | 10 s |
| `cm_wifi_snapshots.txt` | `dumpsys cm_wifi` (controller status history, P2P events, RSDB, map-share) | 30 s |
| `sf_latency_samples.tsv` | SurfaceFlinger `--latency` advance of the active panel layer | 1 s |
| `vr_api_logcat.txt` | per-second FPS/Stale/Stale2-5-10-max/TW/App/CFL/ICFL/PoseAge/ASW lines | 1 s |
| `ping_samples.txt` | PC→headset ICMP | 1 s |
| `pc_samples.tsv` | NVENC util, GPU util/clocks/power, Windows TCP retransmits/s + sent/s, streamer RSS | ~2 s |
| `session.json`, `clock.json` | detected stream start/end; headset↔PC clock offset (~0.5 s) and cell window | on events |
| `codec_events.tsv`, `decay_episodes.tsv`, `controller_events.tsv` | written by `results` reducer | — |

Reducers of note in `cell.py`: `decay_events()` (sustained delivered-rate collapses + the encoder/TCP state
inside each one — the discriminator between an encoder stall and a TCP collapse), `codec_events()`
(video-codec lifecycle from `batterystats --history`), `cm_reduce()` (controller/P2P events + map-share
cadence), `p2p_stats()`, `pc_summary()`, `ovr_window()`.

Rules that mattered in practice:
- The elevated pktmon task is deliberately absent — do not re-create it; `wire_*` stays null and the
  delivered rate is reconstructed from wlan0 rx deltas instead.
- Windows file locks: the PowerShell samplers briefly hold their TSVs open, so every reader retries
  (`_read_text`/`read_tsv`); never read those files with a single bare `open()`.
- `hub stop` hard-kills: a monitor's `finally` may not run, so the clock/cell window is written at start.
- Two processes emit `VrApi` lines (the streaming session pid and `vrshell`) — the reducer locks onto the
  session pid and reports the other's line count separately.

## 7. Latency/jitter measurement recipes (the project's open gap)

The matrix never measured under-load latency. Available instrument-free recipes:

1. **ICMP from PC while streaming** (`ping -t` to <quest-ip>) — PC->headset only; headset->PC echo is
   firewalled on the PC. Idle RTT shows a 2..119 ms power-save sawtooth; measure it *under load*.
2. **TCP-socket truth from the headset**: `/proc/net/tcp` before/after, plus `/proc/net/snmp`
   `RetransSegs`/`InErrs` deltas — an independent check of loss on VD's TCP video path (the harness has
   no wire-level loss metric).
3. **Compositor verdict**: `dumpsys SurfaceFlinger --latency <layer>` sampled during a cell ->
   frame-interval distribution straight from the compositor, stack-independent (works for Air Link too).
4. **OVR metrics** (VD only) — 1 Hz CSV; note `stale_frame_count` / `stale_frames_consecutive` are
   *per-second buckets*, not cumulative.
