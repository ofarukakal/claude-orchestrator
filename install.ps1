# Install or update the manager skill for Claude Code on Windows.
#
#   install.ps1          copy the skill, then install what the machine lacks
#   install.ps1 -Update  copy the skill only and report what is present (installs nothing)
param([switch]$Update)
$ErrorActionPreference = 'Stop'
$dest = Join-Path $env:USERPROFILE '.claude\skills\manager'

$version = ''
if (Get-Command git -ErrorAction SilentlyContinue) {
    $version = (& git -C $PSScriptRoot log -1 --format='%h %cs' 2>$null | Select-Object -First 1)
}
if (-not $version) { $version = 'unknown ' + (Get-Date -Format 'yyyy-MM-dd') }
$versionFile = Join-Path $dest 'VERSION'
$old = if (Test-Path $versionFile) { (Get-Content $versionFile -TotalCount 1) } else { 'not installed' }

# Build the new copy next to the old one and swap, so files removed from the repository do not linger.
$new = "$dest.new.$PID"
if (Test-Path $new) { Remove-Item $new -Recurse -Force }
New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'manager') $new -Recurse -Force
Set-Content -Path (Join-Path $new 'VERSION') -Value $version -Encoding ascii
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
Move-Item $new $dest
"Skill installed: $dest"
"Version: $old -> $version"
''

if ($Update) { & (Join-Path $dest 'scripts\setup.ps1') }
else { & (Join-Path $dest 'scripts\setup.ps1') -Install }
exit $LASTEXITCODE
