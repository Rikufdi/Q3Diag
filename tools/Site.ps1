# Site.ps1 -- machine-specific paths/IPs for the Quest 3 diagnostics harness.
#
# Same contract as tools/site.py: tools/site.json (git-ignored) overrides
# tools/site.example.json, and QUEST3_<KEY> environment variables override both.
#
# Usage:
#   . (Join-Path $PSScriptRoot 'Site.ps1')
#   $Site = Get-Site
#   $Site.quest_ip
#   $tshark = Resolve-SiteTool $Site 'tshark' @('C:\Program Files\Wireshark\tshark.exe')

Set-StrictMode -Version Latest

$script:SiteToolsDir = $PSScriptRoot

function Get-Site {
    [CmdletBinding()]
    param([string]$Path)

    $toolsDir = $script:SiteToolsDir
    $cfgPath  = if ($Path) { $Path }
                elseif ($env:QUEST3_SITE) { $env:QUEST3_SITE }
                else { Join-Path $toolsDir 'site.json' }
    $examplePath = Join-Path $toolsDir 'site.example.json'

    $merged = [ordered]@{
        base_dir         = (Split-Path -Parent $toolsDir)
        quest_ip         = ''
        pc_ip            = ''
        pc_nic           = ''
        elev_task        = 'PCVR-Elev'
        ovr_metrics_dir  = '/sdcard/Android/data/com.oculus.ovrmonitormetricsservice/files/CapturedMetrics'
        adb              = ''
        python           = ''
        tshark           = ''
        pktmon           = ''
        ffprobe          = ''
        vd_streamer_exe  = ''
        vd_settings_json = ''
        ovr_server_exe   = ''
        vd_switcher_exe  = ''
    }

    foreach ($file in @($examplePath, $cfgPath)) {
        if (-not (Test-Path $file)) { continue }
        $json = Get-Content -Path $file -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($prop in $json.PSObject.Properties) {
            if ($prop.Name -like '_*') { continue }
            if ([string]::IsNullOrWhiteSpace([string]$prop.Value)) { continue }
            $merged[$prop.Name] = [string]$prop.Value
        }
    }

    foreach ($key in @($merged.Keys)) {
        $envKey = 'QUEST3_' + $key.ToUpperInvariant()
        $envVal = [Environment]::GetEnvironmentVariable($envKey)
        if (-not [string]::IsNullOrWhiteSpace($envVal)) { $merged[$key] = $envVal }
    }

    return $merged
}

function Get-SiteValue {
    param([Parameter(Mandatory = $true)]$Site, [Parameter(Mandatory = $true)][string]$Key, [string]$Default = '')
    if ($Site -is [System.Collections.IDictionary] -and $Site.Contains($Key) -and $Site[$Key]) { return [string]$Site[$Key] }
    return $Default
}

# Resolve an executable: profile value -> PATH -> known fallbacks -> throw.
function Resolve-SiteTool {
    param(
        [Parameter(Mandatory = $true)]$Site,
        [Parameter(Mandatory = $true)][string]$Key,
        [string[]]$Fallbacks = @()
    )

    $configured = Get-SiteValue $Site $Key
    if ($configured) {
        if ((Test-Path $configured) -or (Get-Command $configured -ErrorAction SilentlyContinue)) { return $configured }
        throw "site.json: '$Key' is set to '$configured', which does not exist. Fix $($script:SiteToolsDir)\site.json or unset it to use auto-discovery."
    }

    # PATH lookup. A 0-byte match is a broken shim, not an executable: this machine
    # carries a stray C:\Windows\System32\python that shadows the real interpreter.
    foreach ($candidate in @(Get-Command $Key -All -ErrorAction SilentlyContinue)) {
        $src = $candidate.Source
        if ([string]::IsNullOrWhiteSpace($src)) { continue }
        if (-not (Test-Path -LiteralPath $src)) { continue }
        $item = Get-Item -LiteralPath $src -ErrorAction SilentlyContinue
        if ($item -and $item.Length -gt 0) { return $src }
    }

    foreach ($candidate in $Fallbacks) {
        if (Test-Path $candidate) { return $candidate }
    }

    throw "could not locate '$Key'. Set it in $($script:SiteToolsDir)\site.json (copy site.example.json) or export QUEST3_$($Key.ToUpperInvariant())."
}
