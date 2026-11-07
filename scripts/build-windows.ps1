# Build from a regular x64 Windows PowerShell (5.1+) or PowerShell 7 session.
# Requires Python, Node 22+, Rust/MSVC, VS C++ Build Tools + Windows SDK, and WebView2.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Native {
    param([string]$File, [string[]]$NativeArgs)
    & $File @NativeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$File failed with exit code $LASTEXITCODE"
    }
}

function Assert-Artifact {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing artifact: $Path"
    }
    if ((Get-Item -LiteralPath $Path).Length -eq 0) { throw "Empty artifact: $Path" }
}

if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows x64.' }
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    . (Join-Path $PSScriptRoot 'prepare-windows-native.ps1')
    foreach ($Command in @('python', 'node', 'npm', 'cargo', 'rustup', 'cmake', 'clang-cl', 'cl.exe')) {
        if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
            throw "Missing prerequisite: $Command (see README.md)"
        }
    }
    if ($env:RUSTFLAGS) { throw 'Release RUSTFLAGS must be empty: the build enforces the x86-64/SSE2 baseline.' }
    $env:RUSTFLAGS = '-C target-cpu=x86-64 -C target-feature=-avx,-avx2,-fma,-f16c,-bmi2,-sse4.2'
    foreach ($Flag in @('GGML_NATIVE','GGML_AVX','GGML_AVX2','GGML_FMA','GGML_F16C','GGML_BMI2','GGML_SSE42','GGML_CPU_ALL_VARIANTS')) {
        [Environment]::SetEnvironmentVariable($Flag, 'OFF', 'Process')
    }
    if (-not $env:CMAKE_BUILD_PARALLEL_LEVEL) { $env:CMAKE_BUILD_PARALLEL_LEVEL = '2' }
    if (-not $env:CARGO_BUILD_JOBS) { $env:CARGO_BUILD_JOBS = '2' }
    Invoke-Native 'python' @('-c', "import struct, sys; assert sys.version_info >= (3, 10); assert struct.calcsize('P') == 8, '64-bit Python required'")
    Invoke-Native 'node' @('-e', "if (Number(process.versions.node.split('.')[0]) < 22) process.exit(1)")
    $env:TMP = Join-Path $Root '.cache\tmp'
    $env:TEMP = $env:TMP
    $env:PIP_CACHE_DIR = Join-Path $Root '.cache\pip'
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $Root '.cache\pyinstaller'
    $env:CARGO_TARGET_DIR = Join-Path $Root 'src-tauri\target'
    $env:HF_HUB_OFFLINE = '1'
    $env:HF_HUB_DISABLE_TELEMETRY = '1'
    $env:DO_NOT_TRACK = '1'
    New-Item -ItemType Directory -Force -Path $env:TMP, $env:PIP_CACHE_DIR | Out-Null

    Write-Host '[1/5] Preparing engine venv and dependencies...'
    Invoke-Native 'python' @('-m', 'venv', 'engine\.venv')
    $Python = Join-Path $Root 'engine\.venv\Scripts\python.exe'
    Invoke-Native $Python @('-m', 'pip', 'install', '-r', 'engine\requirements-cuda.txt', 'pyinstaller')
    Invoke-Native $Python @('-c', 'import faster_whisper, ctranslate2, PyInstaller')

    Write-Host '[2/5] Freezing murmur-engine.exe...'
    Push-Location (Join-Path $Root 'engine')
    try {
        # Onedir engine without bundled CUDA: point MURMUR_CUDA_DIR at a CUDA folder
        # (murmur_engine/runtime.py adds it to the DLL search path).
        $FreezeArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
            '--name', 'murmur-engine', '--paths', '.', '--exclude-module', 'nvidia',
            '--exclude-module', 'av', '--exclude-module', 'hf_xet',
            '--collect-all', 'faster_whisper', '--collect-all', 'ctranslate2',
            '--collect-all', 'tokenizers', '--collect-binaries', 'onnxruntime',
            '--collect-data', 'onnxruntime', '--hidden-import', 'onnxruntime.capi._pybind_state',
            'murmur_engine/__main__.py')
        Invoke-Native $Python $FreezeArgs
    } finally { Pop-Location }
    $FrozenDir = Join-Path $Root 'engine\dist\murmur-engine'
    $Frozen = Join-Path $FrozenDir 'murmur-engine.exe'
    Assert-Artifact $Frozen
    Invoke-Native $Python @('-c', "import subprocess, sys; subprocess.run([sys.argv[1], '--help'], check=True, timeout=120)", $Frozen)

    Write-Host '[3/5] Installing frontend dependencies and building UI...'
    Invoke-Native 'npm' @('ci')
    Invoke-Native 'npm' @('run', 'build')
    Write-Host '[4/5] Preparing Tauri CLI and MSVC target...'
    Invoke-Native 'rustup' @('target', 'add', 'x86_64-pc-windows-msvc')
    # Match the locked npm CLI version, without assuming a global cargo-tauri install.
    $TauriVersion = (Get-Content -Raw 'node_modules\@tauri-apps\cli\package.json' | ConvertFrom-Json).version
    Invoke-Native 'cargo' @('install', 'tauri-cli', '--version', "=$TauriVersion", '--locked')
    Write-Host '[5/5] Building portable app and NSIS installer (unsigned)...'
    # tauri.windows.conf.json is merged automatically; no source/config edits needed.
    Invoke-Native 'cargo' @('tauri', 'build', '--target', 'x86_64-pc-windows-msvc')
    $Release = Join-Path $env:CARGO_TARGET_DIR 'x86_64-pc-windows-msvc\release'
    $App = Join-Path $Release 'murmur.exe'
    $PortableEngine = Join-Path $Release 'engine\murmur-engine.exe'
    Assert-Artifact $App
    Invoke-Native 'python' @((Join-Path $PSScriptRoot 'audit-llama-cpu.py'), $Release, '--artifact', $App)
    # murmur.exe resolves engine\murmur-engine.exe beside itself (src-tauri/src/engine.rs).
    $PortableEngineDir = Join-Path $Release 'engine'
    if (Test-Path $PortableEngineDir) { Remove-Item $PortableEngineDir -Recurse -Force }
    Copy-Item -LiteralPath $FrozenDir -Destination $PortableEngineDir -Recurse
    Assert-Artifact $PortableEngine
    $Installers = @(Get-ChildItem -Path (Join-Path $Release 'bundle\nsis\*.exe') -File)
    if ($Installers.Count -eq 0) { throw 'NSIS installer was not generated' }
    Write-Host "Portable app: $App"
    Write-Host "Required engine folder beside the app: $PortableEngine"
    foreach ($Installer in $Installers) {
        Assert-Artifact $Installer.FullName
        Write-Host "NSIS installer: $($Installer.FullName)"
    }
    Write-Host 'Unsigned build. Runtime is unverified on real Windows hardware.'
} finally { Pop-Location }
