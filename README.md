## Quest 3 PCVR Wi-Fi streaming diagnostics ##

A tool for checking whether your wireless PCVR streaming setup via Virtual Desktop or Air Link is
actually working well, or just *feels* off. It watches your Wi-Fi link, your GPU encoder, and the
headset's frame rate during a real play session, then tells you plainly whether anything about
this run looks worse than your own established normal.

Windows-only.

--------------------------------------------------------------------------------------------------

## Requirements ##

- Windows 10/11, Python 3.10+ and adb installed (the packaged release bundles adb, so a release
  download needs neither of these beyond Windows itself).
- NVIDIA GPU (nvidia-smi, used to read encoder utilization).
- Virtual Desktop Streamer and/or Air Link, already set up.
- Meta Quest 3, debugging enabled (one-time setup below).

## Setup ##

On the headset:

1. Turn on Developer Mode — via the Meta Horizon mobile app: Menu → Devices → your headset →
   Developer Mode. (Requires a free Meta Horizon developer account)
2. In the headset, go to Settings → Developer and turn on MTP Notification.
3. Connect the headset to your PC with a USB cable. Put the headset on and you should see a
   notification like: "Allow USB Connection" or similar. If you don't see a notification,
   check the notification history and press the notification there to allow.

That USB connection is also the entire tool configuration: on the first run the wizard reads the
headset's Wi-Fi IP off the device and remembers it, so later runs need no cable and no config file.

## Running a session ##

Launch the tool — `Q3Diag-Wizard.exe` from a release, or `python tools/wizard.py` from a checkout.
The wizard is the supported entry point; it walks through everything below in order.

While a session is running you can watch it live at **http://127.0.0.1:8765/** — that page shows
the link, encoder, and frame-rate numbers as they come in, plus where the run is writing and how
much has landed on disk so far.

1. **Choose Quick Test or Advanced.**
   - **Quick Test** asks nothing. It measures whatever is happening right now and files its
     comparison baseline under `codec/bitrate: auto`, so it is a fast health check rather than a
     controlled comparison.
   - **Advanced** asks for a target bitrate and a content type (motion/static). Set that same fixed
     bitrate in the streamer/headset first (dynamic bitrate off), so runs are comparable to each
     other and to their own baseline.
   Stack and Wi-Fi band are detected from the run itself in both modes; you are never asked for
   something the harness can see for itself.
2. **Connect the headset.** Already paired over Wi-Fi, it just connects. If the wireless link has
   dropped (common after a headset reboot), it will ask you to plug in the USB cable once, re-pair
   wireless adb, and tell you when you can unplug. "Not reachable over Wi-Fi" is normal and
   self-resolving — the wizard retries, then falls back to the cable.
3. **Confirm the streaming stack is running** (Virtual Desktop / OVR server), and start it if not.
4. **Optional: PC game frame-rate capture (PresentMon).** Only offered when PresentMon is installed
   (see *Optional extra capture* below). Type the game's name and it will be matched once you
   actually launch it — it does not need to be running yet. `all` captures every presenting process;
   blank genuinely skips PC-side capture.
5. **Set a max session length** in minutes. It stops by itself at that point if you forget to.
6. **Optional: record a Windows Performance Recorder trace** (one admin prompt). Off by default;
   see the size warning below before saying yes.
7. **Play.** Press **Enter** when you're done — deliberately not `q` — and the wizard stops cleanly,
   crunches the numbers, and prints a plain verdict.

```
Verdict: consistent with the saved baseline -- no metric drifted beyond its threshold.
```

or, if something's actually off:

```
Verdict: 1 metric(s) drifted beyond the baseline:
  - retry_rate_pct: 2.599 -> 4.802 (delta +2.203)
```

First time running a particular combination (say, Virtual Desktop + H.264+ + 500 Mbps)? There's no
baseline to compare against yet — the wizard says so and saves this run *as* the baseline, so every
future session with that same setup has something to be checked against. If a drift is expected and
fine, you can also save the new run as the new baseline at the end of a session.

## Where your data is saved, and how big it gets ##

Every session writes into its own folder:

```
<install folder>/runs/<run_id>/
```

That is `runs/<run_id>/` next to `Q3Diag-Wizard.exe` for a release, or next to `tools/` in a
checkout. Each run defaults to a `priv_` prefix (`priv_quick…`, `priv_adv…`) and `runs/` is
git-ignored, so a run is private and is only ever published if you publish it yourself. Nothing is
uploaded anywhere.

The wizard prints the exact folder and an estimate for your chosen session length before it starts,
and the live dashboard tracks how much has actually been written. Rough rates, measured from real
runs, so you can size a session:

| Artifact | Written to | Typical size |
|---|---|---|
| Always-on samplers (Wi-Fi counters, thermals, headset fps/logcat, OVR metrics, PC samples, ping) | several small files in the run folder | **~0.15 MB/min** (≈ 9 MB/hour) |
| PresentMon game frame times *(optional)* | `presentmon.csv` | **~2.5 MB/min** (≈ 150 MB/hour at ~160 fps) — scales with the game's frame rate |
| Windows Performance Recorder *(optional)* | `trace.etl` | **~1.3 GB/min** (≈ 78 GB/hour) — the big one |

Everything except PresentMon and the trace grows with the session clock and stays small; the trace
is in a class of its own. If you enable it:

- it is staged in `%TEMP%` (on your system drive) while recording and moved into the run folder when
  the session ends, so **both** the system drive and the run drive need the headroom;
- it also perturbs the session it is measuring — a traced session stutters more than an untraced one
  — so keep traced sessions short and treat them as a separate investigation, not a normal run.

The wizard checks free space on the run drive and warns before starting if the estimate could
exhaust it.

## Optional extra capture ##

Neither of these is bundled; both are optional and only used if you point the tool at them.

- **PresentMon** — accurate PC-side fps for the game itself. Download the console-app build from
  [PresentMon releases](https://github.com/GameTechDev/PresentMon/releases/latest) and either drop
  it in as `PresentMon.exe` beside the tool or set `presentmon_exe` in `site.json`.
- **iperf3** — throughput baseline testing, same policy: bring your own copy.

## Troubleshooting

"Headset not reachable over Wi-Fi" — this is normal and usually resolves itself; the wizard
retries automatically. If it still can't connect, it'll ask you to plug in a USB cable — put the
headset on and accept the debugging prompt if it reappears, and the wizard re-establishes the
wireless connection from there.

Nothing happens when you plug in USB, adb devices shows nothing — adb was started before cable
plugged in. Run `adb kill-server` then try again.

Numbers look worse than expected right after a hardware/driver change — that's exactly what
this tool is for. Run a session, look at the verdict; if you're confident the new normal is fine,
save the new run as the new baseline.

Downloaded release flagged by Windows Defender — the packaged exe is unsigned, and unsigned
PyInstaller binaries routinely trip Defender's ML heuristic. Only a submitted-and-whitelisted hash
or a paid signing certificate reliably clears it.

## License

- **Code** (`tools/**`) is **MIT** — see [`LICENSE`](LICENSE).
- **Data and write-ups** are **CC BY 4.0** — see [`LICENSE-DATA`](LICENSE-DATA) for the details and
  attribution.
