# Quest-Probe.ps1 - read-only capability snapshot of a connected Quest 3 over adb.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\Quest-Probe.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\Quest-Probe.ps1 -Serial <quest_ip>:<port> -OutDir <base_dir>\probe\run1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\Quest-Probe.ps1 -Only wifi_dumpsys,oculuswifi
#
# Writes: <OutDir>\raw\<probe>.txt  (verbatim command output)
#         <OutDir>\index.json       (per-probe exit code / bytes / ms)
#         <OutDir>\SUMMARY.md       (identity, capability roll-up, thermal table)
# Strictly read-only: `adb shell <cmd>` only. Nothing pushed, nothing changed on device.
# Mutating operations (settings put, am force-stop, push, screenrecord) are deliberately absent.
#
# Notes from the first probe run (2026-09-16):
#   * The headset re-advertises wireless adb over mDNS as _adb-tls-connect; port 5555 is usually dead.
#   * /sys/class/thermal is permission-denied for shell -> use `dumpsys thermalservice` for zone temps.
#   * `dumpsys media.codec` / `dmesg` need root; `dumpsys wifi` embeds a WifiScoreReport CSV with
#     per-3s rssi / tx_good / tx_retry / tx_bad / bcnCnt / throughput for the whole session.
#   * Vendor services worth dumping: OculusWifi (radio/TX power), OVRMetricsService, DiagnosticsCollectorService,
#     HologramService, Strata, FanMonitorService (pwm-tach-fan0), snapvrs.
#   * Streaming app package names: VD = VirtualDesktop.Android ; Air Link = com.oculus.xrstreamingclient.

param(
    [string]$Adb,
    [string]$Serial,
    [string]$QuestIp,
    [string]$OutDir,
    [int]$TimeoutSec = 30,
    [string[]]$Only
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Site.ps1')
$Site = Get-Site
if (-not $QuestIp) { $QuestIp = Get-SiteValue $Site 'quest_ip' }

function Resolve-Adb {
    if ($Adb -and (Test-Path $Adb)) { return $Adb }
    return Resolve-SiteTool $Site 'adb' @((Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe'))
}

function Get-ConnectedSerial([string]$adb) {
    $lines = & $adb devices 2>$null | Where-Object { $_ -match '\sdevice$' }
    if ($lines) { return ($lines | Select-Object -First 1).Split("`t")[0] }
    return $null
}

function Connect-Quest([string]$adb, [string]$serial, [string]$ip) {
    if (-not $serial) { $serial = Get-ConnectedSerial $adb }
    if ($serial) { return $serial }
    & $adb connect "${ip}:5555" 2>$null | Out-Null
    $serial = Get-ConnectedSerial $adb
    if ($serial) { return $serial }
    $svc = (& $adb mdns services 2>$null | Select-String '_adb-tls-connect' | Select-Object -First 1)
    if ($svc) {
        $endpoint = ($svc.ToString() -split '\s+')[-1]
        & $adb connect $endpoint 2>$null | Out-Null
        $serial = Get-ConnectedSerial $adb
        if ($serial) { return $serial }
    }
    throw "no Quest reachable: tried ${ip}:5555 and mDNS _adb-tls-connect"
}

function Invoke-Probe([string]$adb, [string]$serial, [string]$name, [string]$remoteCmd, [string]$rawDir, [int]$timeoutSec) {
    $out = Join-Path $rawDir "$name.txt"
    $err = Join-Path $rawDir "$name.err"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $adb -ArgumentList @('-s', $serial, 'shell', $remoteCmd) `
        -RedirectStandardOutput $out -RedirectStandardError $err -NoNewWindow -PassThru
    $done = $p.WaitForExit($timeoutSec * 1000)
    if (-not $done) { $p.Kill(); $p.WaitForExit() }
    $sw.Stop()
    $bytes = if (Test-Path $out) { (Get-Item $out).Length } else { 0 }
    $errText = if ((Test-Path $err) -and (Get-Item $err).Length -gt 0) { (Get-Content $err -Raw).Trim() } else { '' }
    Remove-Item $err -ErrorAction SilentlyContinue
    [pscustomobject]@{
        probe = $name; cmd = $remoteCmd
        exit = if ($done) { $p.ExitCode } else { -1 }
        timed_out = (-not $done); bytes = $bytes; ms = [int]$sw.ElapsedMilliseconds; stderr = $errText
    }
}

$adb = Resolve-Adb
$serial = Connect-Quest $adb $Serial $QuestIp
if (-not $OutDir) {
    $stamp = (Get-Date).ToString('yyyy-MM-dd-HHmmss')
    $OutDir = Join-Path (Join-Path (Get-SiteValue $Site 'base_dir') 'probe') $stamp
}
$rawDir = Join-Path $OutDir 'raw'
New-Item -ItemType Directory -Force -Path $rawDir | Out-Null

$probes = [ordered]@{
    identity         = 'getprop'
    packages_3       = 'pm list packages -3'
    cmd_services     = 'cmd -l'
    dumpsys_services = 'dumpsys -l'
    uptime           = 'uptime'
    disk             = 'df -h'
    battery          = 'dumpsys battery'
    power            = 'dumpsys power'
    thermalservice   = 'dumpsys thermalservice'
    cpuinfo          = 'dumpsys cpuinfo'
    meminfo          = 'cat /proc/meminfo'
    net_dev          = 'cat /proc/net/dev'
    net_snmp         = 'cat /proc/net/snmp'
    net_tcp          = 'cat /proc/net/tcp'
    net_udp          = 'cat /proc/net/udp'
    netstats         = 'dumpsys netstats'
    connectivity     = 'dumpsys connectivity'
    wifi_commands    = 'cmd wifi'
    wifi_status      = 'cmd wifi status'
    wifi_scan        = 'cmd wifi list-scan-results'
    wifi_dumpsys     = 'dumpsys wifi'
    oculuswifi       = 'dumpsys OculusWifi'
    ovrmetricssvc    = 'dumpsys OVRMetricsService'
    diagnostics      = 'dumpsys DiagnosticsCollectorService'
    hologram         = 'dumpsys HologramService'
    strata           = 'dumpsys Strata'
    fan              = 'dumpsys FanMonitorService'
    snapvrs          = 'dumpsys snapvrs'
    guardian         = 'dumpsys guardian'
    sf_list          = 'dumpsys SurfaceFlinger --list'
    sf_latency       = 'dumpsys SurfaceFlinger --latency com.oculus.vrshell/com.oculus.vrshell.HomeActivity'
    gfxinfo_vd       = 'dumpsys gfxinfo VirtualDesktop.Android'
    gfxinfo_airlink  = 'dumpsys gfxinfo com.oculus.xrstreamingclient'
    media_codec      = 'dumpsys media.codec'
    media_metrics    = 'dumpsys media.metrics'
    media_player     = 'dumpsys media.player'
    activity_top     = 'dumpsys activity top'
    settings_global  = 'settings list global'
    settings_secure  = 'settings list secure'
    devicecfg_wifi    = 'device_config list wifi'
    logcat_tail      = 'logcat -d -b all -t 400'
    ovr_metrics_dir  = 'ls -l /sdcard/Android/data/com.oculus.ovrmonitormetricsservice/files/CapturedMetrics'
}

if ($Only) {
    $filtered = [ordered]@{}
    foreach ($k in $probes.Keys) { if ($Only -contains $k) { $filtered[$k] = $probes[$k] } }
    $probes = $filtered
}

$results = foreach ($k in $probes.Keys) { Invoke-Probe $adb $serial $k $probes[$k] $rawDir $TimeoutSec }
$results | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $OutDir 'index.json') -Encoding UTF8

# ---- roll-up -------------------------------------------------------------
function Raw([string]$n) { $p = Join-Path $rawDir "$n.txt"; if (Test-Path $p) { [string](Get-Content $p -Raw) } else { '' } }
function Has([string]$n, [string]$pat) { $t = Raw $n; return ($t -ne '' -and $t -match $pat) }
$props = Raw 'identity'
function Prop([string]$name) {
    $m = [regex]::Match($props, "(?m)^\[$([regex]::Escape($name))\]: \[(.*)\]\s*$")
    if ($m.Success) { $m.Groups[1].Value.Trim() } else { '' }
}
$kernel = (& $adb -s $serial shell uname -r) 2>$null

# thermal zones: "Temperature{mValue=62.76, mType=0, mName=cpuss-0, mStatus=0}"
$temps = [regex]::Matches((Raw 'thermalservice'), 'Temperature\{mValue=([\d.]+), mType=(\d+), mName=([^,]+),') |
    ForEach-Object { [pscustomobject]@{ name = $_.Groups[3].Value; c = [double]$_.Groups[1].Value; type = [int]$_.Groups[2].Value } } |
    Where-Object { $_.type -ne 6 -and $_.type -ne 7 -and $_.c -gt 1 -and $_.c -lt 130 } |
    Group-Object name | ForEach-Object { $_.Group[-1] }
$hot = $temps | Sort-Object c -Descending | Select-Object -First 12

$lines = @()
$lines += '# Quest probe summary'
$lines += ''
# The adb endpoint is <ip>:<port> and the ip is the operator's own LAN address; publish the port only.
# ro.serialno is the headset's own serial number, which is device-identifying: keep the field, drop the
# value. Raw outputs (unredacted) stay under OutDir\raw, which .gitignore keeps out of the repository.
$serialPort = ($serial -split ':')[-1]
$lines += "- captured: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')   serial: <quest-ip>:$serialPort"
$lines += "- model: $(Prop 'ro.product.model')   horizon: $(Prop 'ro.build.display.id')   android: $(Prop 'ro.build.version.release')   kernel: $kernel"
$lines += "- build: $(Prop 'ro.build.fingerprint')"
$lines += "- soc: $(Prop 'ro.soc.model')   abi: $(Prop 'ro.product.cpu.abi')   serial-props: <headset-serial>"
$lines += "- vr: vrapi=$(Prop 'ro.vrapi.version') hw=$(Prop 'ro.vr.hardware') ocms=$(Prop 'ro.ocms.version')"
$lines += "- mem: $((Raw 'meminfo') -split "`n" | Where-Object { $_ -match 'MemTotal|MemAvailable' } | ForEach-Object { $_.Trim() })"
$lines += ''
$lines += '## Capability roll-up'
$lines += ''
$lines += '| capability | present | evidence |'
$lines += '|---|---|---|'
$lines += "| wifi MAC counters (tx/retry/lost) | $(if (Has 'wifi_status' 'retriedTxPackets') { 'yes' } else { 'no' }) | ``cmd wifi status`` |"
$lines += "| wifi score time series (rssi/tput/retry) | $(if (Has 'wifi_dumpsys' 'WifiScoreReport') { 'yes' } else { 'no' }) | ``dumpsys wifi`` -> WifiScoreReport |"
$lines += "| vendor radio state (band/TX power/coex) | $(if (Has 'oculuswifi' 'STA TX Power') { 'yes' } else { 'no' }) | ``dumpsys OculusWifi`` |"
$lines += "| thermal zones | $($temps.Count) | ``dumpsys thermalservice`` |"
$lines += "| fan / cooling device | $(if (Has 'thermalservice' 'CoolingDevice') { 'yes' } else { 'no' }) | ``dumpsys FanMonitorService`` |"
$lines += "| per-socket TCP table | $(if (Has 'net_tcp' 'local_address') { 'yes' } else { 'no' }) | ``/proc/net/tcp`` |"
$lines += "| TCP retransmit counters | $(if (Has 'net_snmp' 'RetransSegs') { 'yes' } else { 'no' }) | ``/proc/net/snmp`` |"
$lines += "| per-iface byte/drop counters | $(if (Has 'net_dev' 'wlan0') { 'yes' } else { 'no' }) | ``/proc/net/dev`` |"
$lines += "| compositor layer list | $(if (Has 'sf_list' '\w') { 'yes' } else { 'no' }) | ``dumpsys SurfaceFlinger --list`` |"
$lines += "| compositor frame timings | $(if ((Raw 'sf_latency').Trim() -match '^1\d{7}') { 'yes (layer had no frames if all-zero)' } else { 'no' }) | ``dumpsys SurfaceFlinger --latency <layer>`` |"
$lines += "| gfxinfo for VD client | $(if (Has 'gfxinfo_vd' 'Total frames') { 'yes' } else { 'needs app running' }) | ``dumpsys gfxinfo VirtualDesktop.Android`` |"
$lines += "| gfxinfo for Air Link client | $(if (Has 'gfxinfo_airlink' 'Total frames') { 'yes' } else { 'needs app running' }) | ``dumpsys gfxinfo com.oculus.xrstreamingclient`` |"
$lines += "| media codec dump | $(if (Has 'media_codec' 'Codec') { 'yes' } else { 'empty (root or no codec)' }) | ``dumpsys media.codec`` |"
$lines += "| dropdown of wireless adb commands | $(if (Has 'wifi_commands' 'set-wifi-enabled') { 'yes' } else { 'no' }) | ``cmd wifi`` |"
$lines += ''
$lines += '## Hottest thermal zones (C)'
$lines += ''
$lines += '| zone | C |'
$lines += '|---|---|'
foreach ($t in $hot) { $lines += "| $($t.name) | $([math]::Round($t.c, 1)) |" }
$lines += ''
$lines += '## Probe results'
$lines += ''
$lines += '| probe | exit | bytes | ms |'
$lines += '|---|---|---|---|'
foreach ($r in $results) { $lines += "| $($r.probe) | $($r.exit)$(if ($r.timed_out) { ' (timeout)' }) | $($r.bytes) | $($r.ms) |" }
$lines += ''
$lines += "Raw outputs: ``$rawDir``"
$lines -join "`n" | Set-Content -Path (Join-Path $OutDir 'SUMMARY.md') -Encoding UTF8

Write-Host "probe dir: $OutDir"
foreach ($r in $results) {
    "{0,-18} exit={1,-4} {2,9:N0} B {3,6} ms{4}" -f $r.probe, $r.exit, $r.bytes, $r.ms,
        $(if ($r.timed_out) { '  TIMEOUT' } elseif ($r.stderr) { "  err: $($r.stderr)" } else { '' })
}
