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
#   tcp_retrans_per_s, tcp_sent_per_s, tcp_conns, dpc_pct_mean, dpc_pct_max, isr_pct_mean, isr_pct_max,
#   mem_avail_mb, mem_pages_per_s, mem_page_faults_per_s,
#   game_name, game_cpu_pct, game_ws_mb, game_prio, game_pf_per_s,
#   vr_match, vr_cpu_pct, vr_ws_mb, streamer_cpu_pct, streamer_ws_mb
#
# dpc_pct_*/isr_pct_* = Deferred Procedure Call / Interrupt Service Routine time, per logical CPU.
# They cover the one interference class nothing else here can see: a driver spending long stretches
# in DPC/ISR, which presents as a PC-side frame stall with an idle GPU (measured 2026-09-19: ~100 ms
# game-frame stalls, MsGPUBusy 1.3 ms, MsGPUWait ~100 ms, invisible to the headset). Vendor audio
# services and APOs are the usual offenders. `mean` covers all logical CPUs; `max` is the worst
# single one, because a storm confined to one core hides inside a 16-core average.
#
# mem_* / game_* exist to tell two otherwise identical-looking failures apart, both of which present
# as the game dropping to ~10 fps with the GPU idle (measured 2026-09-19: repeated 10-18 s episodes
# at ~100 ms/frame, during which the machine's TCP send rate collapsed from ~43k to ~256 segments/s):
#   * MEMORY STARVATION -> mem_avail_mb falls and mem_pages_per_s / mem_page_faults_per_s spike
#     (hard faults), and the game's own game_pf_per_s spikes with it. NOTE these are capacity/paging
#     counters: they say nothing about memory BANDWIDTH, so a downclocked 4-DIMM kit cannot show up
#     here at all.
#   * CPU THROTTLING (Windows EcoQoS / efficiency mode) -> game_cpu_pct flatlines while the game is
#     not being scheduled at all, and game_prio goes to "Idle".
#
# game_threads / game_thr_active / game_thr_wait answer the question the other columns cannot: when
# the game's frame time jumps to ~100 ms, its CPU share collapses to ~0.2 % and the GPU, encoder and
# wire all go idle in sympathy (measured 2026-09-19), WHAT is it blocked on? Nothing else is consuming
# the CPU and no counter moves -- so it is a wait, and the wait REASON is the whole answer. Reasons are
# Windows' own (Thread.WaitReason), and they discriminate cleanly:
#   WrUserRequest / WrDelayExecution       idle worker thread, benign -- expected in bulk
#   WrQueue / WrLpcReceive / WrLpcReply    waiting on ANOTHER PROCESS (a queue, an LPC round trip)
#   WrEventPair / WrRendezvous             waiting on an event/fence
#   WrMutex / WrResource / WrPushLock      internal lock contention
#   WrPageIn / WrVirtualMemory             paging
#   WrCpuRateControl                       Windows throttling the process (EcoQoS)
# `game_thr_wait` carries the per-sample histogram of the top reasons as "Reason:count;..." so a stall
# can be read straight off the row. NOTE this needs the process handle -- fine for a normal game, but
# like the streamer a higher-integrity process would deny it.
#
# vr_*/streamer_* cover the other side of "the game is waiting on something": the VR runtime and the
# Virtual Desktop stack. They matter because a game blocked in its frame submission shows up as the
# GPU idling while the runtime picks up the slack (or stops picking it up).
#
# WHY THE PER-PROCESS NUMBERS COME FROM PERF COUNTERS, NOT Get-Process: Windows denies a
# medium-integrity caller the handle it needs to read TotalProcessorTime/PriorityClass from a
# higher-integrity process -- measured 2026-09-19 against VirtualDesktop.Streamer.exe, where
# WorkingSet64 read fine but TotalProcessorTime and PriorityClass both came back empty, silently
# blanking the old streamer_cpu_s column. Perf counters need no process handle, so
# \Process(*)\% Processor Time / Working Set / Page Faults/sec are authoritative here. All CPU figures
# are reported as % of the whole machine (counter value / logical CPUs), matching game_cpu_pct.
# `game_prio` still comes from Get-Process, since only .NET exposes PriorityClass.

param(
    [Parameter(Mandatory=$true)][string]$OutFile,
    [int]$Seconds = 5400,
    [double]$IntervalSec = 1.0,
    [string]$StreamerProcess = 'VirtualDesktop.Streamer',
    # Instance names as the perf counters spell them: no ".exe", lower case. Covers the VR runtimes
    # (Oculus, SteamVR -- including the SteamVR *compositor*, which runs as its own process and is
    # easy to miss) and the Virtual Desktop stack. Names that aren't running simply don't appear in
    # the vr_match column, so listing a stack you aren't using costs nothing and says so honestly.
    # A VD/VDXR title shouldn't light up any of the SteamVR ones; vr_match is what proves it.
    [string[]]$VrProcesses = @('ovrserver_x64',
                               'vrserver', 'vrcompositor', 'vrmonitor', 'vrdashboard', 'vrwebhelper',
                               'virtualdesktop.streamer', 'virtualdesktop.server', 'virtualdesktop.service')
)

$ErrorActionPreference = 'SilentlyContinue'
$TAB = [char]9
$header = [string]::Join($TAB, @("timestamp", "gpu_util_pct", "enc_util_pct", "gpu_mem_util_pct", "sm_clock_mhz",
    "gpu_temp_c", "gpu_power_w", "tcp_retrans_per_s", "tcp_sent_per_s", "tcp_conns",
    "dpc_pct_mean", "dpc_pct_max", "isr_pct_mean", "isr_pct_max",
    "mem_avail_mb", "mem_pages_per_s", "mem_page_faults_per_s",
    "game_name", "game_cpu_pct", "game_ws_mb", "game_prio", "game_pf_per_s",
    "game_threads", "game_thr_active", "game_thr_wait",
    "vr_match", "vr_cpu_pct", "vr_ws_mb", "streamer_cpu_pct", "streamer_ws_mb",
    "top_procs"))
if (-not (Test-Path $OutFile)) { Set-Content -Path $OutFile -Value $header -Encoding UTF8 }

$cores = [Environment]::ProcessorCount
$script:gameName = ''
function Get-GameName {
    # cell.py's monitor resolves the free-text hint to an exact image name mid-run and writes it to
    # this file; reading it here means the sampler needs no new plumbing to learn what "the game" is.
    if ($script:gameName) { return $script:gameName }
    $p = Join-Path (Split-Path -Parent $OutFile) 'presentmon_target.json'
    if (Test-Path $p) {
        try {
            $j = Get-Content $p -Raw | ConvertFrom-Json
            if ($j.process) { $script:gameName = [string]$j.process }
        } catch { }
    }
    return $script:gameName
}

function Get-Gpu {
    $out = & nvidia-smi --query-gpu=utilization.gpu,utilization.encoder,utilization.memory,clocks.sm,temperature.gpu,power.draw --format=csv,noheader,nounits 2>$null
    if (-not $out) { return $null }
    $f = ($out -split ',') | ForEach-Object { $_.Trim() }
    if ($f.Count -lt 6) { return $null }
    return @{ gpu = $f[0]; enc = $f[1]; mem = $f[2]; clk = $f[3]; temp = $f[4]; pow = $f[5] }
}

function Get-Counters([string]$GameExe) {
    # ONE Get-Counter call for all of these. The call costs ~1 s on this box no matter how many counters
    # are in it (measured 2026-09-19: 3 counters = 1141 ms, 613 samples = 1045 ms, and even a single
    # wildcard counter = 1171 ms), so collecting per-process data for every process is effectively free
    # and a second call would merely double the sampler's period.
    $paths = @('\TCPv4\Segments Retransmitted/sec', '\TCPv4\Segments Sent/sec', '\TCPv4\Connections Established',
               '\Processor(*)\% DPC Time', '\Processor(*)\% Interrupt Time',
               '\Memory\Available MBytes', '\Memory\Pages/sec', '\Memory\Page Faults/sec',
               '\Process(*)\% Processor Time', '\Process(*)\Working Set', '\Process(*)\Page Faults/sec')
    $s = (Get-Counter $paths -ErrorAction SilentlyContinue).CounterSamples
    $r = @{ retrans = ''; sent = ''; conns = ''
            dpc_mean = ''; dpc_max = ''; isr_mean = ''; isr_max = ''
            mem_avail = ''; mem_pages = ''; page_faults = ''
            game_pf = ''; proc_cpu = @{}; proc_ws = @{} }
    $dpc = New-Object System.Collections.ArrayList
    $isr = New-Object System.Collections.ArrayList
    foreach ($c in $s) {
        $p = $c.Path.ToLower()
        # Perf-counter process instances read "...\process(foo)\...", with no extension and lower case.
        if ($p -like '*process(*)*') {
            if ($p -match 'process\(([^)]+)\)') {
                $inst = $Matches[1]
                if ($p -like '*% processor time*') { $r.proc_cpu[$inst] = [double]$c.CookedValue }
                elseif ($p -like '*working set*')   { $r.proc_ws[$inst] = [double]$c.CookedValue }
                elseif ($p -like '*page faults/sec*' -and $r.game_pf -eq '') {
                    $want = ''
                    if ($GameExe) { $want = ([IO.Path]::GetFileNameWithoutExtension($GameExe)).ToLower() }
                    if ($want -and $inst -eq $want) { $r.game_pf = [math]::Round($c.CookedValue, 1) }
                }
            }
            continue
        }
        if ($p -like '*segments retransmitted*')      { $r.retrans = [math]::Round($c.CookedValue, 1) }
        elseif ($p -like '*segments sent*')           { $r.sent = [math]::Round($c.CookedValue, 1) }
        elseif ($p -like '*connections established*') { $r.conns = [int]$c.CookedValue }
        elseif ($p -like '*available mbytes*')        { $r.mem_avail = [math]::Round($c.CookedValue, 1) }
        elseif ($p -like '*pages/sec*')               { $r.mem_pages = [math]::Round($c.CookedValue, 1) }
        elseif ($p -like '*page faults/sec*')         { $r.page_faults = [math]::Round($c.CookedValue, 1) }
        elseif ($p -like '*% dpc time*')              { if ($p -notlike '*_total*') { [void]$dpc.Add($c.CookedValue) } }
        elseif ($p -like '*% interrupt time*')        { if ($p -notlike '*_total*') { [void]$isr.Add($c.CookedValue) } }
    }
    # _Total is the across-core average; the per-core instances are what catch a single-core storm.
    if ($dpc.Count) {
        $r.dpc_mean = [math]::Round(($dpc | Measure-Object -Average).Average, 2)
        $r.dpc_max  = [math]::Round(($dpc | Measure-Object -Maximum).Maximum, 2)
    }
    if ($isr.Count) {
        $r.isr_mean = [math]::Round(($isr | Measure-Object -Average).Average, 2)
        $r.isr_max  = [math]::Round(($isr | Measure-Object -Maximum).Maximum, 2)
    }
    return $r
}

$deadline = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $deadline) {
    $now = Get-Date
    $ts = $now.ToString('yyyy-MM-dd HH:mm:ss.fff')
    $game = Get-GameName
    $g = Get-Gpu
    $t = Get-Counters $game
    $gpu = ''; $enc = ''; $mem = ''; $clk = ''; $temp = ''; $pow = ''
    if ($g) { $gpu = $g.gpu; $enc = $g.enc; $mem = $g.mem; $clk = $g.clk; $temp = $g.temp; $pow = $g.pow }
    $retrans = ''; $sent = ''; $conns = ''
    $dpc_mean = ''; $dpc_max = ''; $isr_mean = ''; $isr_max = ''
    $mem_avail = ''; $mem_pages = ''; $page_faults = ''
    if ($t) {
        $retrans = $t.retrans; $sent = $t.sent; $conns = $t.conns
        $dpc_mean = $t.dpc_mean; $dpc_max = $t.dpc_max; $isr_mean = $t.isr_mean; $isr_max = $t.isr_max
        $mem_avail = $t.mem_avail; $mem_pages = $t.mem_pages; $page_faults = $t.page_faults
    }
    # Game: CPU and working set from the counters, priority class from .NET (only it exposes that).
    $gname = ''; $gcpu = ''; $gws = ''; $gprio = ''; $game_pf = ''
    $gthr = ''; $grun = ''; $gwait = ''
    if ($t) { $game_pf = $t.game_pf }
    if ($game) {
        $gname = $game
        $inst = ([IO.Path]::GetFileNameWithoutExtension($game)).ToLower()
        if ($t -and $t.proc_cpu.ContainsKey($inst)) { $gcpu = [math]::Round($t.proc_cpu[$inst] / $cores, 2) }
        if ($t -and $t.proc_ws.ContainsKey($inst))  { $gws = [math]::Round($t.proc_ws[$inst] / 1MB, 1) }
        $gp = Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($game)) -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($gp) {
            $gprio = [string]$gp.PriorityClass
            # What the threads are actually waiting on. Every other column says the machine is idle
            # during a stall; this one says what it is idle *waiting for*.
            try {
                $reasons = @{}
                $active = 0
                $total = 0
                foreach ($th in $gp.Threads) {
                    $total++
                    if ([string]$th.ThreadState -eq 'Wait') {
                        $wr = [string]$th.WaitReason
                        if (-not $wr) { $wr = 'Unknown' }
                        if ($reasons.ContainsKey($wr)) { $reasons[$wr]++ } else { $reasons[$wr] = 1 }
                    } else {
                        $active++
                    }
                }
                $gthr = $total
                $grun = $active
                $gwait = (($reasons.GetEnumerator() | Sort-Object Value -Descending |
                           Select-Object -First 6 | ForEach-Object { "$($_.Key):$($_.Value)" }) -join ';')
            } catch { }
        }
    }
    # VR runtime / Virtual Desktop stack, summed. This is the other candidate for what a blocked game
    # is waiting on.
    $vrNames = @(); $vrCpu = 0.0; $vrWs = 0.0
    if ($t) {
        foreach ($n in $VrProcesses) {
            if ($t.proc_cpu.ContainsKey($n)) {
                $vrNames += $n
                $vrCpu += $t.proc_cpu[$n]
                if ($t.proc_ws.ContainsKey($n)) { $vrWs += $t.proc_ws[$n] }
            }
        }
    }
    $vrMatch = ($vrNames -join ',')
    $vrCpuPct = ''
    if ($vrNames.Count) { $vrCpuPct = [math]::Round($vrCpu / $cores, 2) }
    $vrWsMb = ''
    if ($vrNames.Count) { $vrWsMb = [math]::Round($vrWs / 1MB, 1) }
    $stCpuPct = ''; $stWsMb = ''
    $stInst = $StreamerProcess.ToLower()
    if ($t -and $t.proc_cpu.ContainsKey($stInst)) { $stCpuPct = [math]::Round($t.proc_cpu[$stInst] / $cores, 2) }
    if ($t -and $t.proc_ws.ContainsKey($stInst))  { $stWsMb = [math]::Round($t.proc_ws[$stInst] / 1MB, 1) }
    # Top CPU consumers right now, as "% of machine". The counter call already returns per-process CPU
    # for every process, so this is free -- and it is the only view we have of what ELSE is running
    # while the game stalls. A WPA export of the traced 10:18 run put MsMpEng.exe (Windows Defender's
    # engine) third behind only Idle and the game, at 55 s of CPU -- ~5x the VD streamer -- with the
    # machine otherwise 86% idle. Whether that spikes during the stalls in ordinary, untraced runs is
    # exactly what this column answers.
    $top = ''
    if ($t -and $t.proc_cpu.Count) {
        $top = (($t.proc_cpu.GetEnumerator() |
                 Where-Object { $_.Key -notin @('idle', '_total') } |
                 Sort-Object Value -Descending | Select-Object -First 3 |
                 ForEach-Object { "$($_.Key):$([math]::Round($_.Value / $cores, 2))" }) -join ';')
    }
    $line = [string]::Join($TAB, @($ts, $gpu, $enc, $mem, $clk, $temp, $pow, $retrans, $sent, $conns,
        $dpc_mean, $dpc_max, $isr_mean, $isr_max,
        $mem_avail, $mem_pages, $page_faults,
        $gname, $gcpu, $gws, $gprio, $game_pf,
        $gthr, $grun, $gwait,
        $vrMatch, $vrCpuPct, $vrWsMb, $stCpuPct, $stWsMb,
        $top))
    Add-Content -Path $OutFile -Encoding UTF8 -Value $line
    Start-Sleep -Milliseconds ([int]($IntervalSec * 1000))
}
