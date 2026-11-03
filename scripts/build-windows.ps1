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
    foreach ($Command in @('python', 'node', 'npm', 'cargo', 'rustup', 'cmake', 'clang-cl')) {
        if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
            throw "Missing prerequisite: $Command (see README.md)"
        }
    }
    if (-not $env:LIBCLANG_PATH) {
        foreach ($Candidate in @("$env:ProgramFiles\LLVM\bin", "${env:ProgramFiles(x86)}\LLVM\bin")) {
            if (Test-Path -LiteralPath (Join-Path $Candidate 'libclang.dll')) {
                $env:LIBCLANG_PATH = $Candidate
                break
            }
        }
    }
    if (-not $env:LIBCLANG_PATH -or -not (Test-Path -LiteralPath (Join-Path $env:LIBCLANG_PATH 'libclang.dll'))) {
        throw 'Missing LLVM libclang.dll; set LIBCLANG_PATH to its directory.'
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
        $FreezeArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
            '--name', 'murmur-engine', '--paths', '.', '--runtime-hook', 'runtime_hook_cuda.py',
            '--collect-all', 'faster_whisper', '--collect-all', 'ctranslate2',
            '--collect-all', 'tokenizers', '--collect-all', 'onnxruntime')
        foreach ($Package in @('nvidia.cudnn', 'nvidia.cublas', 'nvidia.cuda_nvrtc')) {
            # Missing optional modules must not fail a CPU-capable build.
            $Probe = "import importlib.util, sys; top = importlib.util.find_spec('nvidia'); sys.exit(0 if top and importlib.util.find_spec('$Package') else 1)"
            & $Python -c $Probe
            if ($LASTEXITCODE -eq 0) { $FreezeArgs += @('--collect-all', $Package) }
        }
        $FreezeArgs += 'murmur_engine/__main__.py'
        Invoke-Native $Python $FreezeArgs
    } finally { Pop-Location }
    $Frozen = Join-Path $Root 'engine\dist\murmur-engine.exe'
    Assert-Artifact $Frozen
    $Sidecar = Join-Path $Root 'src-tauri\binaries\murmur-engine-x86_64-pc-windows-msvc.exe'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Sidecar) | Out-Null
    Copy-Item -LiteralPath $Frozen -Destination $Sidecar -Force
    if ((Get-FileHash $Frozen).Hash -ne (Get-FileHash $Sidecar).Hash) { throw 'Sidecar copy failed verification' }
    Invoke-Native $Python @('-c', "import subprocess, sys; subprocess.run([sys.argv[1], '--help'], check=True, timeout=120)", $Sidecar)

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
    $PortableEngine = Join-Path $Release 'murmur-engine.exe'
    Assert-Artifact $App
    # The portable pair must work without the checkout or Python installed.
    Copy-Item -LiteralPath $Frozen -Destination $PortableEngine -Force
    Assert-Artifact $PortableEngine
    if ((Get-FileHash $Frozen).Hash -ne (Get-FileHash $PortableEngine).Hash) { throw 'Portable sidecar copy failed verification' }
    $Installers = @(Get-ChildItem -Path (Join-Path $Release 'bundle\nsis\*.exe') -File)
    if ($Installers.Count -eq 0) { throw 'NSIS installer was not generated' }
    Write-Host "Portable app: $App"
    Write-Host "Required adjacent sidecar: $PortableEngine"
    foreach ($Installer in $Installers) {
        Assert-Artifact $Installer.FullName
        Write-Host "NSIS installer: $($Installer.FullName)"
    }
    Write-Host 'Unsigned build. Runtime is unverified on real Windows hardware.'
} finally { Pop-Location }
