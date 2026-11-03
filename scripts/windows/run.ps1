$ErrorActionPreference = 'Stop'
$skillDir = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$python = Join-Path $skillDir '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Run setup.ps1 first.' }
& $python (Join-Path (Split-Path $PSScriptRoot -Parent) 'wechat_sync.py') @args
exit $LASTEXITCODE
