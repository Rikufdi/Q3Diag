## Quest 3 PCVR Wi-Fi streaming diagnostics ##

A tool for checking whether your wireless PCVR streaming setup via Virtual Desktop or Air Link is
actually working well, or just *feels* off. It watches your Wi-Fi link, your GPU encoder, and the
headset's frame rate during a real play session, then tells you plainly whether anything about
this run looks worse than your own established normal.

Windows-only

--------------------------------------------------------------------------------------------------

## Requirements ##

- Windows 10/11, with Python 3.10+ and adb installed.
- NVIDIA GPU** (nvidia-smi, used to read encoder utilization).
- Virtual Desktop Streamer and/or *Air Link, already set up.
- Meta Quest 3, debugging enabled (one-time setup below).

## Setup ##

On the headset:

1. Turn on Developer Mode — via the Meta Horizon mobile app: Menu → Devices → your headset →
   Developer Mode. (Requires a free Meta Horizon developer account)
2. In the headset, go to Settings → Developer and turn on MTP Notification.
3. Connect the headset to your PC with a USB cable. Put the headset on and you should see a
   notification like: "Allow USB Connection" or similar. If you don't see a notification,
   check the notification history and press the notification there to allow.

## Running a session (while live you can watch it on localhost (http://127.0.0.1:8765/)

Before starting the watcher, decide on a codec and bitrate you want to use as a baseline and
set that up first in the VD streamer app (untick dynamic bitrate) and in the Streaming
settings in the VD headset app. If using air link, just set a fixed bitrate in the headset.

First session is to record a baseline which is used to evaluate the later runs. Recommended
to do an advanced setup where you set the expected bitrate (same as you chose earlier)
and then set content type accordingly (motion if recording while playing, static otherwise).

1. Connect headset to PC with a cable with data transfer. Some usb cables only do charging.
2. Launch Q3Diag.exe
3. The tool connects to the headset via cable and adb, checks the ip of the headset and then
   starts adb wirelessly. When wireless adb is running, you can unplug your usb cable.
4. In the tool you will first have 2 choices, quick and advanced.
   4.1. Quick test - starts a measuring session straight away and auto-detecs most settings.
   4.2. Advanced - configure the watcher to measure against a specific scenario.
      4.2.1 Set bitrate target. Useful to test numbers on specific target bitrates.
      4.2.2 Set the watcher to expect a regular play session with motion or a static one.
   4.3 Set max time in minutes the watcher will run before terminating.
5. Press enter to start the watcher.
6. You press Enter when you're done playing. It stops cleanly, crunches the numbers, and
   prints a plain verdict.

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
future session with that same setup has something to be checked against.

## Troubleshooting

"Headset not reachable over Wi-Fi" — this is normal and usually resolves itself; the wizard
retries automatically. If it still can't connect, it'll ask you to plug in a USB cable — put the
headset on and accept the debugging prompt if it reappears, and the wizard re-establishes the
wireless connection from there.

Nothing happens when you plug in USB, adb devices shows nothing — adb was started before cable
plugged in, Run `adb kill-server` then try again.

Numbers look worse than expected right after a hardware/driver change — that's exactly what
this tool is for. Run a session, look at the verdict; if you're confident the new normal is fine,
save the new run as the new baseline

## What this data is used for

Every session you run gets reduced into a small results file "Priv_*" and a row in
results.csv. The data gathered is automatically excluded from commits (if forking the repo) when
they're named "Priv_*"

## License

- **Code** (`tools/**`) is **MIT** — see [`LICENSE`](LICENSE).
- **Data and write-ups** are **CC BY 4.0** — see [`LICENSE-DATA`](LICENSE-DATA) and
  [`docs/RESEARCH.md`](docs/RESEARCH.md) for the details and attribution.
