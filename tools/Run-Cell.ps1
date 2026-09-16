# Run-Cell.ps1 -- one measurement cell of the PCVR Wi-Fi streaming matrix.
#
# Must run from an ELEVATED PowerShell (pktmon requires admin).
#   powershell -NoProfile -ExecutionPolicy Bypass -File Run-Cell.ps1 `
#     -Stack vd -Band 6g -Codec "H264+" -BitrateMbps 500 -Content motion -QuestIp 192.168.1.x
#
# Behaviour (per plan Step 8): create runs/<run_id>/, snapshot settings.json, run the
# background Wi-Fi sampler, snapshot NIC/pktmon counters before, capture 128-byte pktmon
# UDP trace for warmup+session, stop and convert to pcapng, snapshot counters after,
# prompt the operator for in-headset overlay readings, pull OVR Metrics CSV and
# screenshots, reduce wire bitrate via analyze.py, and write results.json + results.csv.

param(
    [Parameter(Mandatory=$true)][ValidateSet('vd','airlink')][string]$Stack,
    [Parameter(Mandatory=$true)][ValidateSet('6g','5g')][string]$Band,
    [Parameter(Mandatory=$true)][string]$Codec,
    [Parameter(Mandatory=$true)][int]$BitrateMbps,
    [Parameter(Mandatory=$true)][ValidateSet('motion','static')][string]$Content,
    [string]$QuestIp = '',
    [int]$CodecMaxMbps = 0,
    [string]$QualityPreset = 'Ultra',
    [int]$RefreshHz = 90,
    [string]$SswAsw = 'off',
    [string]$ApChannel = '',
    [string]$ApWidthMhz = '160',
    [string]$Ssid = '',
    [string]$Notes = '',
    [string]$BaseDir = '',
    [int]$WarmupSec = 30,
    [int]$SessionSec = 120
)

$ErrorActionPreference = 'Stop'

# ---- site profile (machine-specific paths/IPs) -----------------------------
. (Join-Path $PSScriptRoot 'Site.ps1')
$Site = Get-Site
if (-not $BaseDir) { $BaseDir = Get-SiteValue $Site 'base_dir' }
$siteQuestIp = Get-SiteValue $Site 'quest_ip'

# ---- tool path resolution -------------------------------------------------
$winGet = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
$ADB    = Resolve-SiteTool $Site 'adb'    @((Join-Path $winGet 'Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe'))
$TSHARK = Resolve-SiteTool $Site 'tshark' @('C:\Program Files\Wireshark\tshark.exe')
$PKTMON = Resolve-SiteTool $Site 'pktmon' @('C:\Windows\System32\PktMon.exe')
$PYTHON = Resolve-SiteTool $Site 'python' @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python310\python.exe'))
$FFPROBE = Resolve-SiteTool $Site 'ffprobe' @('C:\Program Files\Virtual Desktop Streamer\ffprobe.exe')

# ---- elevation check ------------------------------------------------------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning "Not elevated -- pktmon capture will fail. Re-run from an elevated PowerShell."
}

# ---- run id + dirs --------------------------------------------------------
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$runId = "$Stack`_$Band`_$Codec`_$BitrateMbps`_$Content`_$ts"
$runDir = Join-Path (Join-Path $BaseDir 'runs') $runId
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

# ---- headset identity -----------------------------------------------------
# Wireless adb dies with every headset reboot, and the Android 11+ Wireless Debugging endpoint only
# advertises over mDNS (the classic tcpip 5555 port is not configured on this build).
function Get-AttachedQuest([string]$prefix) {
    $lines = & $ADB devices 2>$null | Select-String '\sdevice$'
    foreach ($l in $lines) {
        $s = $l.ToString().Split("`t")[0].Trim()
        if (-not $prefix -or $s.StartsWith($prefix)) { return $s }
    }
    return $null
}
function Resolve-Quest([string]$hint) {
    foreach ($h in @($hint, $siteQuestIp)) {
        if (-not $h) { continue }
        $s = Get-AttachedQuest $h; if ($s) { return $s }
        if ($h -notmatch ':') {
            & $ADB connect "${h}:5555" 2>$null | Out-Null
            $s = Get-AttachedQuest $h; if ($s) { return $s }
        }
        $svc = (& $ADB mdns services 2>$null | Select-String '_adb-tls-connect' | Select-Object -First 1)
        if ($svc) {
            & $ADB connect (($svc.ToString() -split '\s+')[-1]) 2>$null | Out-Null
            $s = Get-AttachedQuest $h; if ($s) { return $s }
        }
    }
    throw "no authorized adb device (tried '$hint', :5555, mDNS _adb-tls-connect)"
}
$QuestIp = Resolve-Quest $QuestIp
Write-Host "headset: $QuestIp"

# ---- settings.json snapshot ----------------------------------------------
$wifi = (& $ADB -s $QuestIp shell cmd wifi status 2>$null | Out-String)
function Rx([string]$pat) { if ($wifi -match $pat) { $Matches[1] } else { '' } }
$questRssi  = Rx 'RSSI:\s*(-?\d+)'
$questLink  = Rx 'Link speed:\s*(\d+)Mbps'
$questFreq  = Rx 'Frequency:\s*(\d+)MHz'

$nic = $null
$pcNic = Get-SiteValue $Site 'pc_nic'
if (-not $pcNic) {
    # No NIC configured: take the interface that actually routes to the headset.
    $pcNic = (Find-NetRoute -RemoteIPAddress $QuestIp -ErrorAction SilentlyContinue | Select-Object -First 1).InterfaceAlias
}
if ($pcNic) { $nic = Get-NetAdapter -Name $pcNic -ErrorAction SilentlyContinue }
$pcNicLink = if ($nic) { $nic.LinkSpeed } else { '' }

$vdCfg = $null
$vdSettings = Get-SiteValue $Site 'vd_settings_json' 'C:\ProgramData\Virtual Desktop\StreamerSettings.json'
if (Test-Path $vdSettings) {
    $vdCfg = Get-Content $vdSettings -Raw | ConvertFrom-Json
}
$vdVer = ''
$vdExe = Get-SiteValue $Site 'vd_streamer_exe' 'C:\Program Files\Virtual Desktop Streamer\VirtualDesktop.Streamer.exe'
if (Test-Path $vdExe) { $vdVer = (Get-Item $vdExe).VersionInfo.FileVersion }

$ovrVer = ''
$ovrExe = Get-SiteValue $Site 'ovr_server_exe' 'C:\Program Files\Oculus\Support\oculus-runtime\OVRServer_x64.exe'
if (Test-Path $ovrExe) { $ovrVer = (Get-Item $ovrExe).VersionInfo.FileVersion }

$displayMode = ''
$d = Get-CimInstance Win32_VideoController | Select-Object -First 1
if ($d) { $displayMode = "$($d.CurrentHorizontalResolution)x$($d.CurrentVerticalResolution) @ $($d.CurrentRefreshRate) Hz" }

$settings = [ordered]@{
    run_id = $runId
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    stack = $Stack
    codec = $Codec
    codec_max_mbps = $CodecMaxMbps
    bitrate_mbps = $BitrateMbps
    dynamic_bitrate = 'off'
    refresh_hz = $RefreshHz
    quality_preset = $QualityPreset
    ssw_asw = $SswAsw
    content_mode = $Content
    band = $Band
    ssid = $Ssid
    ap_channel = $ApChannel
    ap_width_mhz = $ApWidthMhz
    pc_nic = $pcNic
    pc_nic_link_speed = $pcNicLink
    pc_display_mode = $displayMode
    quest_serial = (& $ADB devices | Select-String 'device$' | Select-Object -First 1).ToString().Split("`t")[0]
    quest_link_rate_mbps = $questLink
    quest_rssi_dbm = $questRssi
    quest_frequency_mhz = $questFreq
    vd_streamer_version = $vdVer
    oculus_service_version = $ovrVer
    notes = $Notes
}
$settings | ConvertTo-Json -Depth 3 | Set-Content -Path (Join-Path $runDir 'settings.json') -Encoding UTF8

# ---- sampler (background) -------------------------------------------------
# The sampler also records /proc/net counters (TCP retransmits, wlan0 errors), vendor radio + thermal
# state, and the SurfaceFlinger advance of the active panel layer.
$samplesFile = Join-Path $runDir 'quest_wifi_samples.tsv'
$sampler = Join-Path $PSScriptRoot 'Sample-Quest.ps1'
$sfLayer = ''
try { $sfLayer = (& $PYTHON (Join-Path $PSScriptRoot 'cell.py') layer 2>$null | Out-String).Trim() } catch { }
Write-Host "sf layer: $(if ($sfLayer) { $sfLayer } else { '(none advancing)' })"
$sfArgs = @()
if ($sfLayer) { $sfArgs = @('-SfFile', "`"$(Join-Path $runDir 'sf_latency_samples.tsv')`"", '-SfLayer', "`"$sfLayer`"") }
$samplerJob = Start-Process -FilePath 'powershell.exe' -ArgumentList (@(
    '-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$sampler`"",
    '-Adb',"`"$ADB`"",'-Serial',$QuestIp,'-OutFile',"`"$samplesFile`"",
    '-NetFile',"`"$(Join-Path $runDir 'quest_net_samples.tsv')`"",
    '-EnvFile',"`"$(Join-Path $runDir 'quest_env_samples.tsv')`"",
    '-Seconds',(($WarmupSec + $SessionSec + 20))
) + $sfArgs) -PassThru -WindowStyle Hidden

# ---- per-second frame telemetry (any stack, incl. Air Link) ----------------
# logcat -s VrApi: FPS/Stale/TW/App/CFL/ICFL/PoseAge from the pid owning the VR session. The main
# logcat buffer only holds ~5 min, so it must be streamed to disk during the cell.
$logcatFile = Join-Path $runDir 'vr_api_logcat.txt'
$logcatJob = Start-Process -FilePath $ADB -ArgumentList @('-s', $QuestIp, 'logcat', '-v', 'time', '-s', 'VrApi') `
    -RedirectStandardOutput $logcatFile -PassThru -WindowStyle Hidden -NoNewWindow

# ---- headset<->PC clock offset --------------------------------------------
$clockFile = Join-Path $runDir 'clock.json'
try {
    $devEpoch = [double]((& $ADB -s $QuestIp shell 'date +%s.%N' 2>$null | Out-String).Trim())
    $uptime = ((& $ADB -s $QuestIp shell 'cat /proc/uptime' 2>$null | Out-String).Trim() -split '\s+')[0]
    @{ dev_epoch_s = $devEpoch; pc_epoch_s = [math]::Round(((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds, 3)
       offset_s = [math]::Round($devEpoch - ((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds, 3)
       dev_uptime_s = [double]$uptime } | ConvertTo-Json | Set-Content -Path $clockFile -Encoding UTF8
} catch { }

# ---- NIC + pktmon counters before ----------------------------------------
$nicBefore = Join-Path $runDir 'nic_before.txt'
Get-NetAdapterStatistics -Name $pcNic | Format-List * | Out-File -FilePath $nicBefore -Encoding UTF8

& $PKTMON filter remove 2>$null | Out-Null
# VD 1.34.22 streams over TCP, so a UDP filter misses the video; `filter add -i <ip>` matches the
# source address only and misses the PC->headset direction. Capture everything; analyze.py classifies
# packets by IP.
& $PKTMON filter add Quest3 2>$null | Out-Null
$dropsBefore = (& $PKTMON counters --type drop --drop-reason 2>$null | Out-String)
$dropsBefore | Out-File -FilePath (Join-Path $runDir 'pktmon_drops_before.txt') -Encoding UTF8
& $PKTMON reset 2>$null | Out-Null

$etl = Join-Path $runDir 'cap.etl'
& $PKTMON start --capture --pkt-size 128 --file-name $etl --log-mode circular --file-size 4096 2>$null | Out-Null

# ---- ping (background, during session) ------------------------------------
$pingFile = Join-Path $runDir 'ping.txt'
$pingJob = Start-Process -FilePath 'ping.exe' -ArgumentList @('-n',($WarmupSec + $SessionSec),'-w','1000',$QuestIp) -RedirectStandardOutput $pingFile -PassThru -WindowStyle Hidden -NoNewWindow

# ---- warmup + session ------------------------------------------------------
Write-Host "== $runId =="
Write-Host "Warm-up ${WarmupSec}s then session ${SessionSec}s -- play the pattern now."
Start-Sleep -Seconds $WarmupSec
Write-Host "SESSION START"
Start-Sleep -Seconds $SessionSec
Write-Host "SESSION END"

# ---- stop capture + counters after ----------------------------------------
& $PKTMON stop 2>$null | Out-Null
$pcap = Join-Path $runDir 'cap.pcapng'
& $PKTMON etl2pcap $etl --out $pcap 2>$null | Out-Null

$dropsAfter = (& $PKTMON counters --type drop --drop-reason 2>$null | Out-String)
$dropsAfter | Out-File -FilePath (Join-Path $runDir 'pktmon_drops_after.txt') -Encoding UTF8

$nicAfter = Join-Path $runDir 'nic_after.txt'
Get-NetAdapterStatistics -Name $pcNic | Format-List * | Out-File -FilePath $nicAfter -Encoding UTF8

# ---- overlay readings (operator) ------------------------------------------
function Read-Num([string]$prompt) {
    $v = Read-Host $prompt
    if ($v -match '^\s*-?\d+(\.\d+)?\s*$') { return [double]$v }
    return $null
}
Write-Host "--- read the in-headset overlay (leave blank if absent) ---"
$overlayFps     = Read-Num 'overlay FPS'
$overlayTotal   = Read-Num 'overlay total latency (ms)'
$overlayGame    = Read-Num 'overlay game latency (ms)'
$overlayEnc     = Read-Num 'overlay encode latency (ms)'
$overlayNet     = Read-Num 'overlay network latency (ms)'
$overlayDec     = Read-Num 'overlay decode latency (ms)'
$overlayBitrate = Read-Num 'overlay bitrate (Mbps)'
$overlayWifi    = Read-Num 'overlay Wi-Fi link (Mbps)'

# ---- pull screenshots + OVR metrics ---------------------------------------
$shotsDir = Join-Path $runDir 'screenshots'
New-Item -ItemType Directory -Force -Path $shotsDir | Out-Null
& $ADB -s $QuestIp pull '/sdcard/Oculus/Screenshots/.' $shotsDir 2>$null | Out-Null

$ovrCsv = Join-Path $runDir 'ovr_metrics.csv'
$ovrDir = Get-SiteValue $Site 'ovr_metrics_dir' '/sdcard/Android/data/com.oculus.ovrmonitormetricsservice/files/CapturedMetrics'
$newest = (& $ADB -s $QuestIp shell "ls -t $ovrDir 2>/dev/null | head -1" 2>$null | Out-String).Trim()
if ($newest) {
    & $ADB -s $QuestIp pull "$ovrDir/$newest" $ovrCsv 2>$null | Out-Null
}

# ---- wait for background jobs, then delegate reduction to cell.py ----------
$samplerJob | Wait-Process -Timeout (($WarmupSec + $SessionSec + 30)) -ErrorAction SilentlyContinue
$pingJob | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
if ($logcatJob -and -not $logcatJob.HasExited) { $logcatJob | Stop-Process -Force -ErrorAction SilentlyContinue }

# cell.py owns ALL reduction (wire bitrate, MAC counters, ping distribution, VrApi frame telemetry,
# TCP/net counters, thermal+radio samples, SurfaceFlinger advance, OVR metrics) so there is exactly one
# implementation - this script only captures and prompts.
$overlayJson = Join-Path $runDir 'overlay.json'
@{ fps = $overlayFps; lat_total = $overlayTotal; lat_game = $overlayGame; lat_encode = $overlayEnc
   lat_network = $overlayNet; lat_decode = $overlayDec; bitrate_mbps = $overlayBitrate
   wifi_mbps = $overlayWifi } | ConvertTo-Json | Set-Content -Path $overlayJson -Encoding UTF8

& $PYTHON (Join-Path $PSScriptRoot 'cell.py') results $runId $overlayJson "$WarmupSec,$($WarmupSec + $SessionSec)"

Write-Host "DONE $runId  (results: runs\$runId\results.json)"
