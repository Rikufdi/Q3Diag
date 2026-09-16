# elevated-manager.ps1 — runs ELEVATED. Command loop via file channel.
# Writes results to elev-res.txt (created only on command completion).
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Site.ps1')
$Site = Get-Site
$cmdFile = Join-Path $PSScriptRoot 'elev-cmd.txt'
$resFile = Join-Path $PSScriptRoot 'elev-res.txt'
$streamerExe = Get-SiteValue $Site 'vd_streamer_exe' 'C:\Program Files\Virtual Desktop Streamer\VirtualDesktop.Streamer.exe'
$settingsJson = Get-SiteValue $Site 'vd_settings_json' 'C:\ProgramData\Virtual Desktop\StreamerSettings.json'
$pktmonExe = Get-SiteValue $Site 'pktmon' 'C:\Windows\System32\PktMon.exe'
$questIp = Get-SiteValue $Site 'quest_ip'

$codecMap = @{
  'H264Plus'  = @{ v = 5;  name = 'H.264+' }
  'HEVC10bit' = @{ v = 6;  name = 'HEVC 10-bit' }
  'AV110bit'  = @{ v = 11; name = 'AV1 10-bit' }
}

function Write-Res([string]$msg) {
  Set-Content -Path $resFile -Value $msg -Encoding UTF8
}

Set-Content -Path $resFile -Value 'READY' -Encoding UTF8

while ($true) {
  if (Test-Path $cmdFile) {
    Remove-Item $resFile -Force -ErrorAction SilentlyContinue
    $line = (Get-Content $cmdFile -Raw).Trim()
    Remove-Item $cmdFile -Force
    $i = $line.IndexOf(' ')
    if ($i -lt 0) { $op = $line; $arg = '' } else { $op = $line.Substring(0, $i); $arg = $line.Substring($i + 1) }
    try {
      switch ($op) {
        'codec' {
          $c = $codecMap[$arg]
          if (-not $c) { Write-Res "ERROR: unknown codec '$arg'"; break }
          Stop-Process -Name 'VirtualDesktop.Streamer' -Force -ErrorAction SilentlyContinue
          Start-Sleep -Seconds 3
          $j = Get-Content $settingsJson -Raw | ConvertFrom-Json
          $j.PreferredCodec = $c.v
          $j.CodecName = $c.name
          $j | ConvertTo-Json -Depth 5 | Set-Content $settingsJson -Encoding UTF8
          Start-Process $streamerExe
          Write-Res "OK codec $arg = $($c.v) ($($c.name))"
        }
        'pktmon-start' {
          # VD 1.34.22 streams over TCP and `filter add -i <ip>` matches the source address only, so a
          # UDP+IP filter captures nothing useful; capture everything and classify by IP in analyze.py.
          & $pktmonExe filter remove 2>$null | Out-Null
          & $pktmonExe filter add Quest3 2>$null | Out-Null
          & $pktmonExe reset 2>$null | Out-Null
          & $pktmonExe start --capture --pkt-size 128 --file-name "$arg\cap.etl" --log-mode circular --file-size 4096 2>$null | Out-Null
          Write-Res "OK pktmon-start $arg"
        }
        'pktmon-stop' {
          & $pktmonExe stop 2>$null | Out-Null
          & $pktmonExe etl2pcap "$arg\cap.etl" --out "$arg\cap.pcapng" 2>$null | Out-Null
          Write-Res "OK pktmon-stop $arg"
        }
        'exit' { Write-Res 'OK exit'; exit 0 }
        default { Write-Res "ERROR: unknown op '$op'" }
      }
    } catch {
      Write-Res "ERROR: $($_.Exception.Message)"
    }
  }
  Start-Sleep -Milliseconds 300
}
