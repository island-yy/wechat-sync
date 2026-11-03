$ErrorActionPreference = 'Stop'
$skillDir = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$venv = Join-Path $skillDir '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $basePython = & py -3 -c 'import sys; print(sys.executable)'
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $basePython = (Get-Command python).Source
    } else {
        throw 'Install 64-bit Python 3.11+ for your Windows user, then rerun this script.'
    }
    & $basePython -c "import sys,struct,platform; assert sys.version_info >= (3,11) and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64','x86_64'), 'Requires Windows x64 and Python 3.11+'"
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported Python runtime.' }
    & $basePython -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated Python environment.' }
}
& $python -m pip install --only-binary=:all: -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install Windows dependencies.' }
& $python -c "import frida,zstandard; from sqlcipher3 import dbapi2 as db; c=db.connect(':memory:'); assert hasattr(c,'set_key'); assert c.execute('PRAGMA cipher_version').fetchone(); c.close(); print('Dependencies ready.')"
if ($LASTEXITCODE -ne 0) { throw 'Native dependency check failed.' }
Write-Output 'Next: .\run.ps1 init'
