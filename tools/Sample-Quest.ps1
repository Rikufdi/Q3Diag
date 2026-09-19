# Sample-Quest.ps1 -- background device sampler for the Quest 3.
#
# Core (always): every IntervalSec, `adb shell cmd wifi status`, extract WifiInfo fields + the four
# packet counters, append one TSV row to -OutFile (quest_wifi_samples.tsv).
#
# Extended (opt-in, each writes its own TSV):
#   -NetFile : /proc/net/dev (wlan0) + /proc/net/snmp (Tcp) counters      [per IntervalSec]
#   -EnvFile : OculusWifi STA state + thermalservice zones + GPU busy %   [per EnvIntervalSec]
#   -SfFile  : `dumpsys SurfaceFlinger --latency <SfLayer>` advance stats [per SfIntervalSec]
#   -CmFile  : `dumpsys cm_wifi` snapshots (controller/P2P state: CONNECTED_ACTIVE/INACTIVE history,
#              P2P channel switches, RSDB concurrency, LOW_LATENCY toggles) [per CmIntervalSec]
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File Sample-Quest.ps1 `
#       -Serial <quest_ip>:<port> -OutFile <tsv> -Seconds 180
#   (omit -Serial and pass -QuestIp to let it resolve the wireless-adb endpoint itself)
#
# Columns (tab-separated):
#   quest_wifi_samples.tsv: timestamp, rssi, link_mbps, tx_link_mbps, rx_link_mbps, freq_mhz,
#                           tx_success, tx_retries, tx_lost, rx_success
#   net samples:            timestamp, wlan0_rx_bytes, wlan0_tx_bytes, wlan0_rx_errs, wlan0_rx_drop,
#                           wlan0_tx_errs, wlan0_tx_drop, tcp_in_segs, tcp_out_segs, tcp_retrans_segs,
#                           tcp_in_errs, tcp_out_rsts
#   env samples:            timestamp, sta_rssi, sta_bw_mhz, sta_tx_power_dbm, gpu_busy_pct,
#                           soc_usr_c, gpuss_max_c, cpuss_max_c, batt_virt_c
#   sf samples:             timestamp, nonzero_rows, max_actual_present_ns, frames_since_prev

param(
    [string]$Adb,
    [string]$Serial,
    [string]$QuestIp,
    [Parameter(Mandatory=$true)][string]$OutFile,
    [int]$Seconds = 180,
    [int]$IntervalSec = 2,
    [string]$NetFile = '',
    [string]$EnvFile = '',
    [int]$EnvIntervalSec = 10,
    [string]$SfLayer = '',
    [string]$SfFile = '',
    [int]$SfIntervalSec = 1,
    [string]$CmFile = '',
    [int]$CmIntervalSec = 30
)

$ErrorActionPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'Site.ps1')
$Site = Get-Site
if (-not $QuestIp) { $QuestIp = Get-SiteValue $Site 'quest_ip' }

function Resolve-Adb {
    if ($Adb -and (Test-Path $Adb)) { return $Adb }
    return Resolve-SiteTool $Site 'adb' @((Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe'))
}
$adb = Resolve-Adb

# Wireless adb drops after a headset reboot / wireless-debugging toggle: the classic tcpip 5555 port is
# usually dead and the Android 11+ Wireless Debugging endpoint only advertises over mDNS.
function Get-Attached([string]$prefix) {
    $lines = & $adb devices 2>$null | Select-String '\sdevice$'
    foreach ($l in $lines) {
        $s = $l.ToString().Split("`t")[0].Trim()
        if (-not $prefix -or $s.StartsWith($prefix)) { return $s }
    }
    return $null
}
function Resolve-QuestSerial([string]$serial, [string]$ip) {
    if ($serial) { $s = Get-Attached $serial; if ($s) { return $s } }
    $s = Get-Attached $ip; if ($s) { return $s }
    & $adb connect "${ip}:5555" 2>$null | Out-Null
    $s = Get-Attached $ip; if ($s) { return $s }
    $svc = (& $adb mdns services 2>$null | Select-String '_adb-tls-connect' | Select-Object -First 1)
    if ($svc) {
        & $adb connect (($svc.ToString() -split '\s+')[-1]) 2>$null | Out-Null
        $s = Get-Attached $ip; if ($s) { return $s }
    }
    throw "no Quest device attached (tried '$serial', ${ip}:5555, mDNS _adb-tls-connect)"
}
$ser = Resolve-QuestSerial $Serial $QuestIp

function Sh([string]$cmd) { return (& $adb -s $ser shell $cmd 2>$null | Out-String) }

# ---- core: wifi counters --------------------------------------------------
function Sample-Wifi {
    $out = Sh 'cmd wifi status'
    $row = @{
        rssi=''; link_mbps=''; tx_link_mbps=''; rx_link_mbps=''; freq_mhz=''
        tx_success=''; tx_retries=''; tx_lost=''; rx_success=''
    }
    # WifiInfo line: "WifiInfo: SSID: ..., RSSI: -42, Link speed: 866Mbps, Tx Link speed: 866Mbps, ..."
    if ($out -match 'RSSI:\s*(-?\d+)')      { $row.rssi = $Matches[1] }
    if ($out -match 'Frequency:\s*(\d+)MHz')        { $row.freq_mhz = $Matches[1] }
    if ($out -match 'Tx Link speed:\s*(\d+)Mbps')   { $row.tx_link_mbps = $Matches[1] }
    if ($out -match 'Rx Link speed:\s*(\d+)Mbps')   { $row.rx_link_mbps = $Matches[1] }
    elseif ($out -match 'Link speed:\s*(\d+)Mbps')  { $row.link_mbps = $Matches[1] }
    if ($out -match 'Link speed:\s*(\d+)Mbps')      { $row.link_mbps = $Matches[1] }
    if ($out -match 'successfulTxPackets:\s*(\d+)') { $row.tx_success = $Matches[1] }
    if ($out -match 'retriedTxPackets:\s*(\d+)')    { $row.tx_retries = $Matches[1] }
    if ($out -match 'lostTxPackets:\s*(\d+)')       { $row.tx_lost = $Matches[1] }
    if ($out -match 'successfulRxPackets:\s*(\d+)') { $row.rx_success = $Matches[1] }
    return $row
}

# ---- net counters: /proc/net/dev + /proc/net/snmp --------------------------
function Sample-Net {
    $row = @{ wlan0_rx_bytes=''; wlan0_tx_bytes=''; wlan0_rx_errs=''; wlan0_rx_drop=''
              wlan0_tx_errs=''; wlan0_tx_drop=''; tcp_in_segs=''; tcp_out_segs=''
              tcp_retrans_segs=''; tcp_in_errs=''; tcp_out_rsts=''
              p2p0_rx_bytes=''; p2p0_tx_bytes=''; p2p0_rx_errs=''; p2p0_rx_drop=''
              p2p0_tx_errs=''; p2p0_tx_drop='' }
    $dev = Sh 'cat /proc/net/dev'
    # "wlan0: rx_bytes rx_packets rx_errs rx_drop rx_fifo rx_frame rx_compressed rx_multicast
    #         tx_bytes tx_packets tx_errs tx_drop tx_fifo tx_colls tx_carrier tx_compressed"
    # p2p0 carries the controller link (Quest Pro controllers / map share) - its error counters are the
    # direct measure of controller-link health, so it is sampled alongside wlan0.
    foreach ($iface in 'wlan0', 'p2p0') {
        $m = [regex]::Match($dev, "(?m)^\s*${iface}:\s*(.+)$")
        if (-not $m.Success) { continue }
        $f = ($m.Groups[1].Value -split '\s+') | Where-Object { $_ -ne '' }
        if ($f.Count -lt 16) { continue }
        $row["${iface}_rx_bytes"] = $f[0]; $row["${iface}_rx_errs"] = $f[2]; $row["${iface}_rx_drop"] = $f[3]
        $row["${iface}_tx_bytes"] = $f[8]; $row["${iface}_tx_errs"] = $f[10]; $row["${iface}_tx_drop"] = $f[11]
    }
    $snmp = Sh 'cat /proc/net/snmp'
    # "Tcp: RtoAlgorithm RtoMin ... InSegs OutSegs RetransSegs InErrs OutRsts InCsumErrors"
    # the header row is textual, so only the values row matches this pattern.
    $tcp = [regex]::Matches($snmp, '(?m)^Tcp:\s*((?:[\d\-]+\s*){5,})$')
    if ($tcp.Count -ge 1) {
        $f = ($tcp[$tcp.Count - 1].Groups[1].Value -split '\s+') | Where-Object { $_ -ne '' }
        if ($f.Count -ge 14) {
            $row.tcp_in_segs = $f[9]; $row.tcp_out_segs = $f[10]
            $row.tcp_retrans_segs = $f[11]; $row.tcp_in_errs = $f[12]; $row.tcp_out_rsts = $f[13]
        }
    }
    return $row
}

# ---- env: vendor radio state + thermals -----------------------------------
function Sample-Env {
    $row = @{ sta_rssi=''; sta_bw_mhz=''; sta_tx_power_dbm=''; gpu_busy_pct=''
              soc_usr_c=''; gpuss_max_c=''; cpuss_max_c=''; batt_virt_c=''; hmd_state='' }
    $ow = Sh 'dumpsys OculusWifi'
    if ($ow -match 'STA rssi:\s*(-?\d+)')        { $row.sta_rssi = $Matches[1] }
    if ($ow -match 'STA bandwidth:\s*(\d+)')     { $row.sta_bw_mhz = $Matches[1] }
    if ($ow -match 'STA TX Power:\s*(\d+) dbm')  { $row.sta_tx_power_dbm = $Matches[1] }
    # Headset mount/proximity state: without it, "rate collapsed because the headset was off-head and VD
    # stopped rendering" is indistinguishable from a real encoder decay.
    $vs = Sh 'dumpsys vrpowermanager | grep -m1 -E "^State:"'
    if ($vs -match 'State:\s*(\S+)')             { $row.hmd_state = $Matches[1] }
    $g = Sh 'cat /sys/class/kgsl/kgsl-3d0/gpu_busy_percentage'
    if ($g -match '(\d+)')                       { $row.gpu_busy_pct = $Matches[1] }
    $th = Sh 'dumpsys thermalservice'
    $temps = [regex]::Matches($th, 'Temperature\{mValue=([\d.]+), mType=(\d+), mName=([^,]+),')
    $by = @{}; foreach ($m in $temps) {
        $t = [int]$m.Groups[2].Value
        if ($t -eq 6 -or $t -eq 7) { continue }
        $v = [double]$m.Groups[1].Value
        if ($v -le 1 -or $v -ge 130) { continue }
        $by[$m.Groups[3].Value] = $v      # later sections = current values
    }
    function MaxOf([string]$pat) {
        $vals = @($by.Keys | Where-Object { $_ -like $pat } | ForEach-Object { $by[$_] })
        if ($vals.Count) { return [math]::Round(($vals | Measure-Object -Maximum).Maximum, 1) } else { return '' }
    }
    $row.soc_usr_c  = if ($by.ContainsKey('soc-usr')) { [math]::Round($by['soc-usr'], 1) } else { '' }
    $row.gpuss_max_c = MaxOf 'gpuss-*'
    $row.cpuss_max_c = MaxOf 'cpuss-*'
    $row.batt_virt_c = if ($by.ContainsKey('batt-virt-usr')) { [math]::Round($by['batt-virt-usr'], 1) } else { '' }
    return $row
}

# ---- compositor: SurfaceFlinger --latency advance --------------------------
# Layer name must be passed to the device shell inside single quotes: it contains '#' (comment char).
function Sample-Sf([string]$layer, [ref]$prevMax) {
    $out = & $adb -s $ser shell "dumpsys SurfaceFlinger --latency '$layer'" 2>$null | Out-String
    $lines = $out -split "`r?`n" | Where-Object { $_ -match '^\s*\d' }
    $period = 0; $nonzero = 0; $maxActual = 0
    if ($lines.Count -gt 1) { $period = [long]($lines[0].Trim()) }
    for ($i = 1; $i -lt $lines.Count; $i++) {
        $f = ($lines[$i] -split '\s+') | Where-Object { $_ -ne '' }
        if ($f.Count -lt 3) { continue }
        $a = [long]$f[1]
        if ($a -gt 0) { $nonzero++; if ($a -gt $maxActual) { $maxActual = $a } }
    }
    $framesSince = ''
    if ($prevMax.Value -gt 0 -and $maxActual -gt $prevMax.Value -and $period -gt 0) {
        $framesSince = [math]::Round(($maxActual - $prevMax.Value) / $period, 2)
    }
    $prevMax.Value = $maxActual
    return @{ nonzero_rows = $nonzero; max_actual_present_ns = $maxActual; frames_since_prev = $framesSince }
}

# ---- redaction -------------------------------------------------------------
# These captures can end up in a published run directory, and the raw device text carries network
# identifiers: SSIDs, interface MAC addresses and the operator's LAN addressing. Replace them with
# per-file ordinals at the moment of writing, so nothing identifying is ever on disk to begin with.
#
# Ordinals rather than a hash: a MAC has only 48 bits of entropy, so a hash is trivially reversible,
# whereas mac1/mac2 preserves the relational structure an analysis needs ("the same AP as the previous
# sample"). The Quest's controller/P2P link always lives on 192.168.49.0/24 -- identical on every
# unit -- so it is deliberately left intact rather than turning a device constant into noise.
$script:RedactMac = @{}
$script:RedactIp = @{}
$script:RedactSsid = @{}
function Protect-Identifiers([string]$Text, [string]$Ssid) {
    if (-not $Text) { return $Text }
    $t = [regex]::Replace($Text, '\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b', {
        param($m)
        $k = $m.Value.ToLower()
        if (-not $script:RedactMac.ContainsKey($k)) { $script:RedactMac[$k] = 'mac' + ($script:RedactMac.Count + 1) }
        $script:RedactMac[$k]
    })
    $t = [regex]::Replace($t, '\b(?!192\.168\.49\.)(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3})\b', {
        param($m)
        $k = $m.Value
        if (-not $script:RedactIp.ContainsKey($k)) { $script:RedactIp[$k] = 'ip' + ($script:RedactIp.Count + 1) }
        $script:RedactIp[$k]
    })
    # Quoted form first, then any remaining literal mention of the connected SSID (the dumps quote it
    # outside an SSID: field, e.g. onConcurrencyModeChanged). Order matters: if the earlier pass has
    # already replaced the quoted occurrence with a label, the literal pass finds nothing to remap and
    # the same network keeps one label instead of acquiring a second.
    $t = [regex]::Replace($t, 'SSID:\s*"([^"]+)"', {
        param($m)
        $k = $m.Groups[1].Value
        if (-not $script:RedactSsid.ContainsKey($k)) { $script:RedactSsid[$k] = 'ssid' + ($script:RedactSsid.Count + 1) }
        'SSID: "' + $script:RedactSsid[$k] + '"'
    })
    if ($Ssid) {
        if (-not $script:RedactSsid.ContainsKey($Ssid)) { $script:RedactSsid[$Ssid] = 'ssid' + ($script:RedactSsid.Count + 1) }
        $t = $t.Replace($Ssid, $script:RedactSsid[$Ssid])
    }
    return $t
}

# ---- headers + main loop ---------------------------------------------------
function Ensure-Header([string]$file, [string]$header) {
    if (-not (Test-Path $file)) { Set-Content -Path $file -Value $header -Encoding UTF8 }
}
Ensure-Header $OutFile "timestamp`trssi`tlink_mbps`ttx_link_mbps`trx_link_mbps`tfreq_mhz`ttx_success`ttx_retries`ttx_lost`trx_success"
if ($NetFile) { Ensure-Header $NetFile "timestamp`twlan0_rx_bytes`twlan0_tx_bytes`twlan0_rx_errs`twlan0_rx_drop`twlan0_tx_errs`twlan0_tx_drop`ttcp_in_segs`ttcp_out_segs`ttcp_retrans_segs`ttcp_in_errs`ttcp_out_rsts`tp2p0_rx_bytes`tp2p0_tx_bytes`tp2p0_rx_errs`tp2p0_rx_drop`tp2p0_tx_errs`tp2p0_tx_drop" }
if ($EnvFile) { Ensure-Header $EnvFile "timestamp`tsta_rssi`tsta_bw_mhz`tsta_tx_power_dbm`tgpu_busy_pct`tsoc_usr_c`tgpuss_max_c`tcpuss_max_c`tbatt_virt_c`thmd_state" }
if ($SfFile)  { Ensure-Header $SfFile  "timestamp`tnonzero_rows`tmax_actual_present_ns`tframes_since_prev" }

# The connected SSID, read once, so redaction can replace that exact string wherever the dumps carry it
# (onConcurrencyModeChanged and friends quote it outside any SSID: field).
$Ssid = ''
$st = Sh 'cmd wifi status'
$mm = [regex]::Match($st, 'SSID:\s*"([^"]+)"')
if ($mm.Success) { $Ssid = $mm.Groups[1].Value }

$prevSf = 0
$deadline = (Get-Date).AddSeconds($Seconds)
$nextWifi = Get-Date; $nextNet = Get-Date; $nextEnv = Get-Date; $nextSf = Get-Date; $nextCm = Get-Date
# The loop ticks at 250 ms and each sampler is gated by its own interval, so the SurfaceFlinger probe
# can run at 1 Hz while the (heavier) wifi/net/env probes stay slower.
while ((Get-Date) -lt $deadline) {
    $now = Get-Date
    $ts = $now.ToString('yyyy-MM-dd HH:mm:ss.fff')
    if ($now -ge $nextWifi) {
        $r = Sample-Wifi
        Add-Content -Path $OutFile -Encoding UTF8 -Value "$ts`t$($r.rssi)`t$($r.link_mbps)`t$($r.tx_link_mbps)`t$($r.rx_link_mbps)`t$($r.freq_mhz)`t$($r.tx_success)`t$($r.tx_retries)`t$($r.tx_lost)`t$($r.rx_success)"
        $nextWifi = $now.AddSeconds($IntervalSec)
    }
    if ($NetFile -and $now -ge $nextNet) {
        $n = Sample-Net
        Add-Content -Path $NetFile -Encoding UTF8 -Value "$ts`t$($n.wlan0_rx_bytes)`t$($n.wlan0_tx_bytes)`t$($n.wlan0_rx_errs)`t$($n.wlan0_rx_drop)`t$($n.wlan0_tx_errs)`t$($n.wlan0_tx_drop)`t$($n.tcp_in_segs)`t$($n.tcp_out_segs)`t$($n.tcp_retrans_segs)`t$($n.tcp_in_errs)`t$($n.tcp_out_rsts)`t$($n.p2p0_rx_bytes)`t$($n.p2p0_tx_bytes)`t$($n.p2p0_rx_errs)`t$($n.p2p0_rx_drop)`t$($n.p2p0_tx_errs)`t$($n.p2p0_tx_drop)"
        $nextNet = $now.AddSeconds($IntervalSec)
    }
    if ($EnvFile -and $now -ge $nextEnv) {
        $e = Sample-Env
        Add-Content -Path $EnvFile -Encoding UTF8 -Value "$ts`t$($e.sta_rssi)`t$($e.sta_bw_mhz)`t$($e.sta_tx_power_dbm)`t$($e.gpu_busy_pct)`t$($e.soc_usr_c)`t$($e.gpuss_max_c)`t$($e.cpuss_max_c)`t$($e.batt_virt_c)`t$($e.hmd_state)"
        $nextEnv = $now.AddSeconds($EnvIntervalSec)
    }
    if ($SfFile -and $SfLayer -and $SfLayer.Trim() -and $now -ge $nextSf) {
        $s = Sample-Sf $SfLayer ([ref]$prevSf)
        Add-Content -Path $SfFile -Encoding UTF8 -Value "$ts`t$($s.nonzero_rows)`t$($s.max_actual_present_ns)`t$($s.frames_since_prev)"
        $nextSf = $now.AddSeconds($SfIntervalSec)
    }
    if ($CmFile -and $now -ge $nextCm) {
        Add-Content -Path $CmFile -Encoding UTF8 -Value "== $ts =="
        Add-Content -Path $CmFile -Encoding UTF8 -Value (Protect-Identifiers (Sh 'dumpsys cm_wifi') $Ssid)
        $nextCm = $now.AddSeconds($CmIntervalSec)
    }
    Start-Sleep -Milliseconds 250
}
