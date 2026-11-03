# Prepare native llama.cpp build tools on a Windows x64 runner.
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows x64 runner required.' }
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path $vswhere)) { throw 'Visual Studio Build Tools with MSVC and Windows SDK required.' }
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'MSVC x64 tools missing.' }
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw 'vcvars64.bat missing.' }
cmd /c "`"$vcvars`" >nul && set" | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process') }
}
if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) { throw 'MSVC environment was not initialized.' }
$lib = $env:LIBCLANG_PATH
if ($lib -and -not (Test-Path (Join-Path $lib 'libclang.dll'))) { throw 'Configured LIBCLANG_PATH lacks libclang.dll.' }
if (-not $lib) {
    foreach ($candidate in @("$env:ProgramFiles\LLVM\bin", "${env:ProgramFiles(x86)}\LLVM\bin")) {
        if (Test-Path (Join-Path $candidate 'libclang.dll')) { $lib = $candidate; break }
    }
}
if (-not $lib -or -not (Get-Command cmake -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command choco -ErrorAction SilentlyContinue)) { throw 'Install LLVM and CMake, or use windows-latest with Chocolatey.' }
    $packages = @()
    if (-not $lib) { $packages += 'llvm' }
    if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) { $packages += 'cmake' }
    choco install @packages -y --no-progress
    if ($LASTEXITCODE -ne 0) { throw 'Native prerequisites failed to install.' }
    if (-not $lib) { $lib = "$env:ProgramFiles\LLVM\bin" }
    $env:Path = "$env:ProgramFiles\CMake\bin;$lib;$env:Path"
}
if (-not (Test-Path (Join-Path $lib 'libclang.dll'))) { throw 'libclang.dll missing.' }
if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) { throw 'CMake missing.' }
$env:LIBCLANG_PATH = $lib
if ($env:GITHUB_ENV) {
    foreach ($name in @('LIBCLANG_PATH','INCLUDE','LIB','LIBPATH','VCINSTALLDIR','VCToolsInstallDir','WindowsSdkDir','PATH')) {
        $value = [Environment]::GetEnvironmentVariable($name, 'Process')
        if ($value) { "$name=$value" | Out-File -FilePath $env:GITHUB_ENV -Append }
    }
}
Write-Host "MSVC, CMake and libclang ready: $lib"
