# Freeze the speech engine as a PyInstaller onedir on Windows x64, from the hash-locked
# engine/requirements-windows-release.txt, then prune CTranslate2's unused cuDNN shim and run
# the cuDNN/NVRTC gate (pefile + unfrozen site-packages). Output: engine\dist\murmur-engine.
# Used by scripts/build-windows.ps1 and .github/workflows/windows-build.yml.
# Not yet run on Windows (verified in source only); Edison CI is the first run.
[CmdletBinding()]
param([string]$Python = 'python')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows x64.' }

function Invoke-Native {
    param([string]$File, [string[]]$NativeArgs)
    & $File @NativeArgs
    if ($LASTEXITCODE -ne 0) { throw "$File failed with exit code $LASTEXITCODE" }
}

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Push-Location $Root
try {
    Invoke-Native $Python @('-c', "import struct, sys; assert sys.version_info[:2] == (3, 12), 'Python 3.12 required (lock is cp312)'; assert struct.calcsize('P') == 8, '64-bit Python required'")
    $Venv = Join-Path $Root 'engine\.release-venv'
    if (Test-Path $Venv) { Remove-Item $Venv -Recurse -Force }
    Invoke-Native $Python @('-m', 'venv', $Venv)
    $VenvPython = Join-Path $Venv 'Scripts\python.exe'
    Invoke-Native $VenvPython @('-m', 'pip', 'install', '--require-hashes', '--only-binary', ':all:', '--no-deps',
        '-r', 'engine\requirements-windows-release.txt')
    Invoke-Native $VenvPython @('-m', 'pip', 'check')
    Invoke-Native $VenvPython @('-c', 'import faster_whisper, ctranslate2, PyInstaller, pefile')

    Push-Location (Join-Path $Root 'engine')
    try {
        # Onedir: no per-start unpack into %TEMP%. CUDA comes from the GPU add-on through
        # MURMUR_CUDA_DIR (murmur_engine/runtime.py -> os.add_dll_directory).
        Invoke-Native $VenvPython @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
            '--name', 'murmur-engine', '--paths', '.', '--exclude-module', 'nvidia',
            '--exclude-module', 'av', '--exclude-module', 'hf_xet',
            '--collect-all', 'faster_whisper', '--collect-all', 'ctranslate2',
            '--collect-all', 'tokenizers', '--collect-binaries', 'onnxruntime',
            '--collect-data', 'onnxruntime', '--hidden-import', 'onnxruntime.capi._pybind_state',
            'murmur_engine/__main__.py')
    } finally { Pop-Location }

    $Frozen = Join-Path $Root 'engine\dist\murmur-engine'
    Invoke-Native $VenvPython @('scripts\components\windows_components.py', 'prune-engine', $Frozen)
    Invoke-Native $VenvPython @('-c', "import subprocess, sys; subprocess.run([sys.argv[1], '--help'], check=True, timeout=120, capture_output=True)", (Join-Path $Frozen 'murmur-engine.exe'))
    Invoke-Native $VenvPython @('scripts\check-engine-imports.py', $Frozen, '--site-packages', (Join-Path $Venv 'Lib\site-packages'))
    Write-Host "Frozen engine: $Frozen"
} finally { Pop-Location }
