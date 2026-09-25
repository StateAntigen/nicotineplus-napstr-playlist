# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
<#
.SYNOPSIS
    Installs the NAPSTR Playlist plugin into the local Nicotine+ user folder.

.DESCRIPTION
    Copies plugin/napstr_playlist into "%APPDATA%\nicotine\plugins\" (or the
    folder given with -NicotineDataFolder). Byte-compiled files are left
    behind: they embed absolute paths, and Nicotine+ compiles the plugin
    itself on first load.

    After copying, close and reopen the Preferences dialog in Nicotine+ and
    enable the plugin under Preferences -> Plugins (no restart needed).

.PARAMETER NicotineDataFolder
    Nicotine+ user data folder. Defaults to "%APPDATA%\nicotine".

.PARAMETER Remove
    Uninstall the plugin instead of installing it.

.EXAMPLE
    .\tools\deploy.ps1
    .\tools\deploy.ps1 -Remove
#>

param(
    [string]$NicotineDataFolder = (Join-Path $env:APPDATA "nicotine"),
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$pluginName = "napstr_playlist"
$repoRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repoRoot "plugin\$pluginName"
$pluginsFolder = Join-Path $NicotineDataFolder "plugins"
$target = Join-Path $pluginsFolder $pluginName

if ($Remove) {
    if (Test-Path $target) {
        Remove-Item $target -Recurse -Force
        Write-Host "Removed $target"
    }
    else {
        Write-Host "Nothing to remove at $target"
    }

    Write-Host "Note: the plugin is still listed in Preferences -> Plugins until Nicotine+ is restarted."
    return
}

if (-not (Test-Path $source)) {
    throw "Plugin source not found at $source"
}

if (-not (Test-Path $pluginsFolder)) {
    New-Item -ItemType Directory -Path $pluginsFolder -Force | Out-Null
}

# Remove the previous copy so deleted files cannot linger
if (Test-Path $target) {
    Remove-Item $target -Recurse -Force
}

New-Item -ItemType Directory -Path $target -Force | Out-Null

$files = Get-ChildItem -Path $source -File |
    Where-Object { $_.Extension -in @(".py", ".md") -or $_.Name -eq "PLUGININFO" }

foreach ($file in $files) {
    Copy-Item -Path $file.FullName -Destination (Join-Path $target $file.Name) -Force
}

Write-Host "Installed $($files.Count) file(s) to $target"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Nicotine+ -> Preferences -> Plugins -> enable 'NAPSTR Playlist'"
Write-Host "     (close and reopen the Preferences dialog if it is already open)"
Write-Host "  2. Open its settings pane and set your Nostr key (nsec1... or 64 hex chars)"
Write-Host "  3. In the log pane or a chat: /napstr help"
