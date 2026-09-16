# Sample-GameFPS.ps1 -- optional PC-side game frame-time capture via PresentMon.
#
# PresentMon (Intel's open-source frame-presentation capture tool, GameTechDev/PresentMon on GitHub) is
# NOT vendored here, same policy as iperf3 -- download the console-app exe yourself
# (PresentMon-<ver>-x64.exe from https://github.com/GameTechDev/PresentMon/releases/latest) and point
# `presentmon_exe` at it in tools/site.json. Verified against the real v2.5.1 console app CLI/CSV on
# 2026-09-16: no admin privilege needed for a system-wide capture (a warning about "<unknown>"
# short-lived/other-account processes is normal and harmless -- it does not block capture, it just can't
# name those specific processes), and the default CSV already includes "Application"/"MsBetweenPresents".
#
# Why this exists: every other sampler in this harness measures the HEADSET's own compositor frame rate
# (OVR CSV, VrApi logcat) or the wireless link. None of them can see the PC GAME's own present rate --
# the one layer findings.md calls out as an open gap. PresentMon captures every presenting process
# system-wide when no --process_name is given, so no game-specific process name has to be known in
# advance; `presentmon_reduce()` in cell.py picks out the dominant non-streamer process from the CSV
# afterwards (same dedup strategy vr_api_reduce uses for VrApi's two emitting pids).
#
# CLI flags can still differ between PresentMon versions/branches. The default below targets the
# verified v2.5.1 console-app CLI; if your build differs, set `presentmon_args` in site.json (a raw,
# space-separated argument string) to override it wholesale -- this script splits that string on
# whitespace and uses it verbatim instead.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File Sample-GameFPS.ps1 `
#            -PresentMonExe <path> -OutFile <csv> -Seconds 5400 [-ExtraArgs "..."]

param(
    [Parameter(Mandatory=$true)][string]$PresentMonExe,
    [Parameter(Mandatory=$true)][string]$OutFile,
    [int]$Seconds = 5400,
    [string]$ExtraArgs = ''
)

$ErrorActionPreference = 'SilentlyContinue'
if (-not (Test-Path $PresentMonExe)) {
    Write-Error "presentmon_exe not found: $PresentMonExe (set it in tools/site.json -- PresentMon is not vendored)"
    exit 1
}

if ($ExtraArgs -and $ExtraArgs.Trim()) {
    $argList = $ExtraArgs -split '\s+'
} else {
    # System-wide capture (no --process_name filter), one CSV, timed + self-terminating, quiet console.
    $argList = @('--no_console_stats', '--stop_existing_session', '--timed', "$Seconds",
                 '--terminate_after_timed', '--output_file', "$OutFile")
}

& $PresentMonExe @argList
