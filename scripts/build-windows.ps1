# Build the Windows release locally from a regular x64 Windows PowerShell (5.1+) or PowerShell 7.
# Requires Python 3.12 x64, Node 22+, Rust/MSVC, VS C++ Build Tools + Windows SDK, LLVM, CMake,
# NSIS and WebView2 (the Edison runner prompt lists the exact packages).
#
# Produces (see scripts/windows/package.ps1): the per-user NSIS installer and portable zip with
# the app only, the engine and cuBLAS-only GPU component zips and their manifest input. The app
# downloads the engine on first run, verified against the signed component manifest.
# Unsigned unless WINDOWS_CERT_PFX_BASE64/WINDOWS_CERT_PASSWORD are set. Verified in source only.
[CmdletBinding()]
param([switch]$SkipComponents)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Native {
    param([string]$File, [string[]]$NativeArgs)
    & $File @NativeArgs
    if ($LASTEXITCODE -ne 0) { throw "$File failed with exit code $LASTEXITCODE" }
}

if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows x64.' }
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    . (Join-Path $PSScriptRoot 'prepare-windows-native.ps1')
    foreach ($Command in @('python', 'node', 'npm', 'cargo', 'rustup', 'cmake', 'clang-cl', 'cl.exe')) {
        if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
            throw "Missing prerequisite: $Command (see docs/building.md)"
        }
    }
    if ($env:RUSTFLAGS) { throw 'Release RUSTFLAGS must be empty: the build enforces the x86-64/SSE2 baseline.' }
    $env:RUSTFLAGS = '-C target-cpu=x86-64 -C target-feature=-avx,-avx2,-fma,-f16c,-bmi2,-sse4.2'
    foreach ($Flag in @('GGML_NATIVE','GGML_AVX','GGML_AVX2','GGML_FMA','GGML_F16C','GGML_BMI2','GGML_SSE42','GGML_CPU_ALL_VARIANTS')) {
        [Environment]::SetEnvironmentVariable($Flag, 'OFF', 'Process')
    }
    if (-not $env:CMAKE_BUILD_PARALLEL_LEVEL) { $env:CMAKE_BUILD_PARALLEL_LEVEL = '2' }
    if (-not $env:CARGO_BUILD_JOBS) { $env:CARGO_BUILD_JOBS = '2' }
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

    Write-Host '[1/4] Freezing the engine (hash-locked wheels, cuDNN/NVRTC gate)...'
    & (Join-Path $PSScriptRoot 'windows\freeze-engine.ps1')
    Write-Host '[2/4] Building the UI...'
    Invoke-Native 'npm' @('ci')
    Invoke-Native 'npm' @('run', 'build')
    Write-Host '[3/4] Building murmur.exe and the NSIS installer...'
    Invoke-Native 'rustup' @('target', 'add', 'x86_64-pc-windows-msvc')
    $TauriArgs = @('tauri', 'build', '--target', 'x86_64-pc-windows-msvc')
    & (Join-Path $PSScriptRoot 'windows\authenticode.ps1') -Import
    if ($env:MURMUR_SIGN_THUMBPRINT) {
        $SignCommand = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\windows\authenticode.ps1`" -Sign %1"
        # A config file, not inline JSON: Windows PowerShell 5.1 strips quotes from native arguments.
        $SignConfig = Join-Path $env:TMP 'tauri.sign.conf.json'
        @{ bundle = @{ windows = @{ signCommand = $SignCommand } } } | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $SignConfig
        $TauriArgs += @('--config', $SignConfig)
    }
    try {
        Invoke-Native 'npx' $TauriArgs
    } finally {
        & (Join-Path $PSScriptRoot 'windows\authenticode.ps1') -Remove
    }
    $Release = Join-Path $env:CARGO_TARGET_DIR 'x86_64-pc-windows-msvc\release'
    Invoke-Native 'python' @((Join-Path $PSScriptRoot 'audit-llama-cpu.py'), $Release, '--artifact', (Join-Path $Release 'murmur.exe'))
    Write-Host '[4/4] Packaging the installer, portable zip and components...'
    & (Join-Path $PSScriptRoot 'windows\package.ps1') -Release $Release -SkipComponents:$SkipComponents
    Write-Host 'Done. Runtime behaviour on Windows hardware is checked with docs/parity/windows-checklist.md.'
} finally { Pop-Location }
