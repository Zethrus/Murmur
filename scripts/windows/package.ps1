# Stage the Windows release from a finished `tauri build` and frozen engine:
#   <Out>\Murmur_<ver>_x64-setup.exe       NSIS, per-user, app only (WebView2 bootstrapper embedded)
#   <Out>\murmur-windows-x86_64.zip        portable: murmur.exe + README.txt (engine downloads on first run)
#   <Components>\murmur-engine-windows-x86_64-<ver>.zip
#   <Components>\murmur-cuda-windows-x86_64-<ver>.zip   (cuBLAS only, from the pinned wheel)
#   <Components>\components-windows-inputs.json         manifest input for the signing job
# then enforces the size budgets (scripts/check-release.py). Verified in source only.
[CmdletBinding()]
param(
    [string]$Release = 'src-tauri\target\x86_64-pc-windows-msvc\release',
    [string]$Out = '.cache\artifacts\windows',
    [string]$Components = '.cache\artifacts\components-windows',
    [string]$Python = 'engine\.release-venv\Scripts\python.exe',
    [switch]$SkipComponents
)
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
    $Version = (Get-Content -Raw 'package.json' | ConvertFrom-Json).version
    foreach ($Dir in @($Out, $Components)) {
        if (Test-Path $Dir) { Remove-Item $Dir -Recurse -Force }
        New-Item -ItemType Directory -Force $Dir | Out-Null
    }
    $App = Join-Path $Release 'murmur.exe'
    if (-not (Test-Path $App) -or (Get-Item $App).Length -eq 0) { throw "Missing app: $App" }
    $Installer = Join-Path $Release "bundle\nsis\Murmur_${Version}_x64-setup.exe"
    if (-not (Test-Path $Installer)) { throw "Missing NSIS installer: $Installer" }
    Copy-Item $Installer $Out

    # Portable: the app and a README only. No engine folder (it is a downloadable component).
    $Portable = Join-Path $Out 'portable'
    New-Item -ItemType Directory -Force $Portable | Out-Null
    Copy-Item $App (Join-Path $Portable 'murmur.exe')
    Copy-Item 'scripts\windows-portable-README.txt' (Join-Path $Portable 'README.txt')
    Compress-Archive -Path (Join-Path $Portable '*') -DestinationPath (Join-Path $Out 'murmur-windows-x86_64.zip') -CompressionLevel Optimal
    Remove-Item $Portable -Recurse -Force

    if (-not $SkipComponents) {
        $Frozen = 'engine\dist\murmur-engine'
        Invoke-Native $Python @('scripts\components\windows_components.py', 'engine', '--engine', $Frozen, '--version', $Version, '--output', $Components)
        $Wheels = Join-Path $env:RUNNER_TEMP 'murmur-cublas-wheel'
        if (-not $env:RUNNER_TEMP) { $Wheels = Join-Path $Root '.cache\cublas-wheel' }
        $Wheel = & $Python 'scripts\components\windows_components.py' 'download-cuda' '--dest' $Wheels | Select-Object -Last 1
        if ($LASTEXITCODE -ne 0) { throw 'cuBLAS wheel download failed' }
        Invoke-Native $Python @('scripts\components\windows_components.py', 'cuda', '--wheel', $Wheel, '--version', $Version, '--output', $Components)
        Remove-Item $Wheels -Recurse -Force
        Invoke-Native $Python @('scripts\components\windows_components.py', 'inputs', '--components', $Components, '--version', $Version)
    }
    # Every file in both folders needs a budget and must stay within it (fails closed).
    Invoke-Native $Python @('scripts\check-release.py', 'budgets', $Out)
    Invoke-Native $Python @('scripts\check-release.py', 'budgets', $Components)
    $Budgeted = @(Get-ChildItem $Out -File) + @(Get-ChildItem $Components -File -Filter '*.zip')
    Invoke-Native $Python (@('scripts\check-release.py', 'sizes') + ($Budgeted | ForEach-Object { $_.FullName }))
    Write-Host 'Unsigned unless Authenticode secrets were configured (see docs/releases.md).'
} finally { Pop-Location }
