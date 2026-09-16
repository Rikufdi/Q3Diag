# Sample-PC.ps1 -- 1 Hz PC-side sampler for the VD bitrate-decay hunt.
#
# The decay question ("bitrate collapses after 20-40 min, codec cycle fixes it") can be either
# ENCODER-side (NVENC/session stops producing) or TRANSPORT-side (TCP congestion collapse on the
# single 500 Mbps flow). These samples are what discriminates them:
#   * utilization.encoder dropping while the delivered rate drops  -> encoder stopped producing
#   * Segments Retransmitted/sec spiking BEFORE the rate drops      -> transport/congestion collapse
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File Sample-PC.ps1 -OutFile <tsv> -Seconds 5400
#
# Columns (tab-separated):
#   timestamp, gpu_util_pct, enc_util_pct, gpu_mem_util_pct, sm_clock_mhz, gpu_temp_c, gpu_power_w,
#   tcp_retrans_per_s, tcp_sent_per_s, tcp_conns, streamer_cpu_s, streamer_ws_mb

param(
    [Parameter(Mandatory=$true)][string]$OutFile,
    [int]$Seconds = 5400,
    [double]$IntervalSec = 1.0,
    [string]$StreamerProcess = 'VirtualDesktop.Streamer'
)

$ErrorActionPreference = 'SilentlyContinue'
$TAB = [char]9
$header = [string]::Join($TAB, @("timestamp", "gpu_util_pct", "enc_util_pct", "gpu_mem_util_pct", "sm_clock_mhz",
    "gpu_temp_c", "gpu_power_w", "tcp_retrans_per_s", "tcp_sent_per_s", "tcp_conns", "streamer_cpu_s", "streamer_ws_mb"))
if (-not (Test-Path $OutFile)) { Set-Content -Path $OutFile -Value $header -Encoding UTF8 }

function Get-Gpu {
    $out = & nvidia-smi --query-gpu=utilization.gpu,utilization.encoder,utilization.memory,clocks.sm,temperature.gpu,power.draw --format=csv,noheader,nounits 2>$null
    if (-not $out) { return $null }
    $f = ($out -split ',') | ForEach-Object { $_.Trim() }
    if ($f.Count -lt 6) { return $null }
    return @{ gpu = $f[0]; enc = $f[1]; mem = $f[2]; clk = $f[3]; temp = $f[4]; pow = $f[5] }
}

function Get-Tcp {
    $s = (Get-Counter '\TCPv4\Segments Retransmitted/sec','\TCPv4\Segments Sent/sec','\TCPv4\Connections Established' -ErrorAction SilentlyContinue).CounterSamples
    $r = @{ retrans = ''; sent = ''; conns = '' }
    foreach ($c in $s) {
        $p = $c.Path.ToLower()
        if ($p -like '*retransmitted*') { $r.retrans = [math]::Round($c.CookedValue, 1) }
        elseif ($p -like '*sent/sec*')  { $r.sent = [math]::Round($c.CookedValue, 1) }
        elseif ($p -like '*established*') { $r.conns = [int]$c.CookedValue }
    }
    return $r
}

$deadline = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $deadline) {
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff')
    $g = Get-Gpu
    $t = Get-Tcp
    $p = Get-Process -Name $StreamerProcess -ErrorAction SilentlyContinue | Select-Object -First 1
    $gpu = ''; $enc = ''; $mem = ''; $clk = ''; $temp = ''; $pow = ''
    if ($g) { $gpu = $g.gpu; $enc = $g.enc; $mem = $g.mem; $clk = $g.clk; $temp = $g.temp; $pow = $g.pow }
    $retrans = ''; $sent = ''; $conns = ''
    if ($t) { $retrans = $t.retrans; $sent = $t.sent; $conns = $t.conns }
    $cpu = ''; $ws = ''
    if ($p) {
        if ($p.TotalProcessorTime) { $cpu = [math]::Round($p.TotalProcessorTime.TotalSeconds, 2) }
        $ws = [math]::Round($p.WorkingSet64 / 1MB, 1)
    }
    $line = [string]::Join($TAB, @($ts, $gpu, $enc, $mem, $clk, $temp, $pow, $retrans, $sent, $conns, $cpu, $ws))
    Add-Content -Path $OutFile -Encoding UTF8 -Value $line
    Start-Sleep -Milliseconds ([int]($IntervalSec * 1000))
}
