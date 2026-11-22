# Prepare a Windows machine for the manager skill.
#
#   setup.ps1            report what is present and what is missing (changes nothing)
#   setup.ps1 -Install   install what is missing with winget
#
# Exit codes: 0 ready, 1 something is missing (re-run with -Install),
#             2 a step only the user can do is pending (printed as [you]).
param([switch] $Install)

$ErrorActionPreference = 'Continue'
$script:missing = $false
$script:needUser = $false

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Find-Tool([string] $n) { $c = Get-Command $n -ErrorAction SilentlyContinue | Select-Object -First 1; if ($c) { $c.Source } }
function Write-Ok([string] $m)   { "[ok]      $m" }
function Write-Miss([string] $m) { "[missing] $m"; $script:missing = $true }
function Write-You([string] $m)  { "[you]     $m"; $script:needUser = $true }

# The Microsoft Store python.exe stub is not a real interpreter.
function Find-Python {
    foreach ($n in 'python', 'python3', 'py') {
        $p = Find-Tool $n
        if ($p -and $p -notlike '*\WindowsApps\*') { return $p }
    }
}

Update-SessionPath
$packages = @(
    @{ Name = 'python'; Id = 'Python.Python.3.12';    Test = { Find-Python } },
    @{ Name = 'tmux';   Id = 'marlocarlo.psmux';      Test = { Find-Tool 'tmux' } },
    @{ Name = 'agy';    Id = 'Google.AntigravityCLI'; Test = { Find-Tool 'agy' } }
)

foreach ($p in $packages) {
    $path = & $p.Test
    if (-not $path -and $Install) {
        if (-not (Find-Tool 'winget')) { Write-You "winget is not available; install $($p.Name) by hand."; continue }
        "Installing $($p.Name) ($($p.Id)) with winget..."
        winget install --id $p.Id -e --source winget --accept-source-agreements --accept-package-agreements --disable-interactivity | Select-Object -Last 2
        Update-SessionPath
        $path = & $p.Test
    }
    if ($path) { Write-Ok ("{0,-7} {1}" -f $p.Name, $path) }
    else { Write-Miss "$($p.Name)  (fix: winget install --id $($p.Id) -e)" }
}

# Optional tools: never make the result "not ready".
#   claude - Claude-model workers (for example the Opus reviewer)
#   gh     - pull requests
$optional = @(
    @{ Name = 'claude'; Id = 'Anthropic.ClaudeCode'; Login = 'claude auth login' },
    @{ Name = 'gh';     Id = 'GitHub.cli';           Login = 'gh auth login --web' }
)
foreach ($p in $optional) {
    $path = Find-Tool $p.Name
    if (-not $path -and $Install -and (Find-Tool 'winget')) {
        "Installing $($p.Name) ($($p.Id)) with winget..."
        winget install --id $p.Id -e --source winget --accept-source-agreements --accept-package-agreements --disable-interactivity | Select-Object -Last 2
        Update-SessionPath
        $path = Find-Tool $p.Name
    }
    if (-not $path) { "[opt]     $($p.Name) not installed  (winget install --id $($p.Id) -e)"; continue }
    $note = ''
    if ($p.Name -eq 'gh') {
        & $path auth status *> $null
        if ($LASTEXITCODE -ne 0) { $note = "  (not signed in: $($p.Login))" }
    } else {
        $note = "  (sign in once with: $($p.Login))"
    }
    "[opt]     {0,-7} {1}{2}" -f $p.Name, $path, $note
}

$py = Find-Python
if ($py -and (Find-Tool 'agy')) {
    $out = & $py (Join-Path $PSScriptRoot 'orch.py') check 2>&1
    if ($LASTEXITCODE -eq 0) { Write-Ok 'signed in; Gemini 4 Hydrogen and Neon are available' }
    else {
        Write-You "agy cannot see the Gemini 4 models. Run 'agy' once in a terminal and sign in with Google in the browser."
        '          Hydrogen and Neon need a Google AI Pro, Ultra or Enterprise plan.'
        $out | Select-Object -Last 6 | ForEach-Object { "          | $_" }
    }
}

''
if ($script:needUser) { 'RESULT: waiting on you - do the [you] steps above, then run this again.'; exit 2 }
if ($script:missing)  { 'RESULT: not ready - run again with -Install.'; exit 1 }
'RESULT: ready.'
