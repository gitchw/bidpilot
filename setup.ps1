param(
    [switch]$Dev
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

if (-not (Test-Path -LiteralPath '.venv')) {
    python -m venv .venv
}

$Python = Join-Path $Root '.venv\Scripts\python.exe'
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Failed to upgrade pip.' }
if ($Dev) {
    & $Python -m pip install -e '.[dev]'
} else {
    & $Python -m pip install -e '.'
}
if ($LASTEXITCODE -ne 0) { throw 'Failed to install BidPilot dependencies.' }

New-Item -ItemType Directory -Force -Path 'data', 'data\secrets', 'outputs\reports' | Out-Null
Write-Host 'BidPilot environment is ready.' -ForegroundColor Green
Write-Host 'Run: .\.venv\Scripts\bidpilot.exe serve'
