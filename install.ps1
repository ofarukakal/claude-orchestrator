# Install the manager skill for Claude Code on Windows, then prepare the machine.
$ErrorActionPreference = 'Stop'
$dest = Join-Path $env:USERPROFILE '.claude\skills\manager'

New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'manager\*') $dest -Recurse -Force
"Skill installed: $dest"
''

& (Join-Path $dest 'scripts\setup.ps1') -Install
exit $LASTEXITCODE
