# elevated-do.ps1 — one-shot elevated helper. Reads ONE command from elev-do-cmd.txt,
# executes it, writes elev-do-res.txt, exits. Run via scheduled task "PCVR-Elev".
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Site.ps1')
$Site = Get-Site
$cmdFile = Join-Path $PSScriptRoot 'elev-do-cmd.txt'
$resFile = Join-Path $PSScriptRoot 'elev-do-res.txt'
$streamerExe = Get-SiteValue $Site 'vd_streamer_exe' 'C:\Program Files\Virtual Desktop Streamer\VirtualDesktop.Streamer.exe'
$settingsJson = Get-SiteValue $Site 'vd_settings_json' 'C:\ProgramData\Virtual Desktop\StreamerSettings.json'
$pktmonExe = Get-SiteValue $Site 'pktmon' 'C:\Windows\System32\PktMon.exe'
$questIp = Get-SiteValue $Site 'quest_ip'

$codecMap = @{
  'H264Plus'  = @{ v = 5;  name = 'H.264+' }
  'HEVC10bit' = @{ v = 6;  name = 'HEVC 10-bit' }
  'AV110bit'  = @{ v = 11; name = 'AV1 10-bit' }
}

$line = (Get-Content $cmdFile -Raw).Trim()
$i = $line.IndexOf(' ')
if ($i -lt 0) { $op = $line; $arg = '' } else { $op = $line.Substring(0, $i); $arg = $line.Substring($i + 1) }

try {
  switch ($op) {
    'codec' {
      $c = $codecMap[$arg]
      if (-not $c) { throw "unknown codec '$arg'" }
      Stop-Process -Name 'VirtualDesktop.Streamer' -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 3
      $j = Get-Content $settingsJson -Raw | ConvertFrom-Json
      $j.PreferredCodec = $c.v
      $j.CodecName = $c.name
      $j | ConvertTo-Json -Depth 5 | Set-Content $settingsJson -Encoding UTF8
      Start-Process $streamerExe
      Set-Content -Path $resFile -Value "OK codec $arg = $($c.v)" -Encoding UTF8
    }
    'pktmon-start' {
      & $pktmonExe filter remove 2>$null | Out-Null
      & $pktmonExe filter add Quest3 2>$null | Out-Null
      & $pktmonExe reset 2>$null | Out-Null
      & $pktmonExe start --capture --comp 1 --pkt-size 128 --file-name "$arg\cap.etl" --log-mode circular --file-size 4096 2>$null | Out-Null
      Set-Content -Path $resFile -Value "OK pktmon-start $arg" -Encoding UTF8
    }
    'pktmon-stop' {
      & $pktmonExe stop 2>$null | Out-Null
      & $pktmonExe etl2pcap "$arg\cap.etl" --out "$arg\cap.pcapng" 2>$null | Out-Null
      Set-Content -Path $resFile -Value "OK pktmon-stop $arg" -Encoding UTF8
    }
    'list' {
      $out = (& $pktmonExe list 2>&1 | Out-String)
      Set-Content -Path $resFile -Value $out -Encoding UTF8
    }
    'stop-streamer' {
      Stop-Process -Name 'VirtualDesktop.Streamer' -Force -ErrorAction SilentlyContinue
      Set-Content -Path $resFile -Value 'OK stop-streamer' -Encoding UTF8
    }
    'set-codec' {
      $sw = Resolve-SiteTool $Site 'vd_switcher_exe'
      $out = (& $sw --target-codec $arg 2>&1 | Out-String)
      $j = Get-Content $settingsJson -Raw | ConvertFrom-Json
      Set-Content -Path $resFile -Value "set-codec $arg -> PreferredCodec=$($j.PreferredCodec); out=[$out]" -Encoding UTF8
    }
    default { throw "unknown op '$op'" }
  }
} catch {
  Set-Content -Path $resFile -Value "ERROR: $($_.Exception.Message)" -Encoding UTF8
}
