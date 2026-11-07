# Provision "Edison" (Windows 10/11 x64 + NVIDIA) as the self-hosted GitHub Actions runner for
# Zethrus/Murmur. Script form of .cache/edison-runner-setup-prompt.md; keep the two consistent.
#
# Run in an elevated Windows PowerShell 5.1+ or PowerShell 7 session:
#   .\provision-runner.ps1                         # steps 0-3 and 5: checks, account, tools, model
#   .\provision-runner.ps1 -RunnerToken <token>    # also step 4: register the runner service
#   .\provision-runner.ps1 -VerifyOnly             # print the verification report only
#
# Idempotent: every step checks before acting and can be re-run. It never disables UAC,
# SmartScreen or the firewall, never upgrades the NVIDIA driver, never installs the CUDA Toolkit
# or cuDNN, never prints passwords, and never creates a registration token (the owner supplies a
# one-time token from https://github.com/Zethrus/Murmur/settings/actions/runners/new).
# Written without a Windows machine: verified in source only, never run.
[CmdletBinding()]
param(
    [string]$RunnerToken,
    [string]$RepositoryUrl = 'https://github.com/Zethrus/Murmur',
    [string]$RunnerName = 'edison',
    [string]$Labels = 'self-hosted,Windows,X64,gpu,nvidia',
    [string]$RunnerRoot = 'C:\actions-runner',
    [string]$ModelRoot = 'C:\murmur-release-models',
    [string]$ServiceUser = 'murmur-ci',
    [int]$MinimumFreeGB = 60,
    [string]$MinimumDriver = '528.33',
    [switch]$VerifyOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Log = Join-Path $env:ProgramData 'murmur-provision.log'
Start-Transcript -Path $Log -Append | Out-Null

function Step([string]$Text) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
# For commands WITHOUT secrets only: the error message repeats every argument, and this script
# runs under Start-Transcript. Commands that take the password or the runner token are called
# directly with a fixed error message (see cmdkey and config.cmd below).
function Invoke-Native {
    param([string]$File, [string[]]$NativeArgs, [int[]]$Ok = @(0))
    & $File @NativeArgs
    if ($Ok -notcontains $LASTEXITCODE) { throw "$File $($NativeArgs -join ' ') failed with exit code $LASTEXITCODE" }
}
function Test-Admin {
    $Identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $Identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Update-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function New-Password {
    $Bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($Bytes)
    return [Convert]::ToBase64String($Bytes) + 'a1!'
}

# Packages from the prompt's toolchain table. PowerShell 7 is required by the workflows
# (`shell: pwsh`). Override switches go to the VS Build Tools installer.
$Packages = [ordered]@{
    'Git.Git' = @{ Verify = { git --version } }
    'GitHub.cli' = @{ Verify = { gh --version } }
    'Microsoft.PowerShell' = @{ Verify = { pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()' } }
    'Microsoft.VisualStudio.2022.BuildTools' = @{
        Override = '--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --add Microsoft.VisualStudio.Component.Windows11SDK.22621'
        Verify = { & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath } }
    'Kitware.CMake' = @{ Verify = { cmake --version } }
    'LLVM.LLVM' = @{ Verify = { clang-cl --version } }
    'Rustlang.Rustup' = @{ Verify = { rustup --version } }
    'OpenJS.NodeJS.LTS' = @{ Verify = { node --version } }
    'Python.Python.3.12' = @{ Verify = { py -3.12 --version } }
    'NSIS.NSIS' = @{ Verify = { makensis /VERSION } }
    'Microsoft.EdgeWebView2Runtime' = @{ Verify = { (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}').pv } }
    '7zip.7zip' = @{ Verify = { 7z i | Select-Object -First 3 } }
}

function Show-Report {
    Step 'Verification report'
    $Os = Get-CimInstance Win32_OperatingSystem
    Write-Host "OS: $($Os.Caption) $($Os.Version) $($Os.OSArchitecture)"
    Write-Host "CPU: $((Get-CimInstance Win32_Processor | Select-Object -First 1).Name)"
    Write-Host ("RAM: {0:N1} GB" -f ($Os.TotalVisibleMemorySize / 1MB))
    Get-PSDrive C | ForEach-Object { Write-Host ("C: free {0:N1} GB" -f ($_.Free / 1GB)) }
    Write-Host "Domain joined: $((Get-CimInstance Win32_ComputerSystem).PartOfDomain)"
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv } else { Write-Host 'nvidia-smi: MISSING' }
    Update-Path
    foreach ($Id in $Packages.Keys) {
        try { $Out = (& $Packages[$Id].Verify 2>&1 | Out-String).Trim() } catch { $Out = "MISSING ($($_.Exception.Message))" }
        Write-Host ("{0,-42} {1}" -f $Id, ($Out -split "`n")[0])
    }
    Write-Host "LIBCLANG_PATH (machine): $([Environment]::GetEnvironmentVariable('LIBCLANG_PATH', 'Machine'))"
    Write-Host "RUSTUP_HOME/CARGO_HOME (machine): $([Environment]::GetEnvironmentVariable('RUSTUP_HOME', 'Machine')) / $([Environment]::GetEnvironmentVariable('CARGO_HOME', 'Machine'))"
    $Model = Join-Path $ModelRoot 'distil-small.en'
    foreach ($Name in @('model.bin', 'config.json', 'tokenizer.json', '.complete')) {
        Write-Host ("model {0,-15} {1}" -f $Name, (Test-Path (Join-Path $Model $Name)))
    }
    if (Test-Path (Join-Path $Model 'model.bin')) { Write-Host "model.bin SHA-256: $((Get-FileHash (Join-Path $Model 'model.bin') -Algorithm SHA256).Hash)" }
    Get-Service 'actions.runner.*' -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "Runner service: $($_.Name) $($_.Status) $($_.StartType)"
        sc.exe qc $_.Name | Select-String 'SERVICE_START_NAME'
    }
    Write-Host "`nOwner, set these repository variables (Settings > Secrets and variables > Actions):"
    Write-Host '  WINDOWS_RUNNER          = ["self-hosted","Windows","X64","gpu"]'
    Write-Host '  ENABLE_WINDOWS          = true'
    Write-Host "  WINDOWS_GPU_TEST_MODELS = $ModelRoot"
    Write-Host "Display: $((Get-CimInstance Win32_VideoController | Select-Object -First 1).CurrentHorizontalResolution) x $((Get-CimInstance Win32_VideoController | Select-Object -First 1).CurrentVerticalResolution) (record Windows scaling by hand: Settings > Display)"
    Write-Host "Log: $Log"
}

try {
    if (-not (Test-Admin)) { throw 'Run this script from an elevated (Administrator) PowerShell.' }
    if ($VerifyOnly) { Show-Report; return }

    Step '0. Preconditions'
    $Os = Get-CimInstance Win32_OperatingSystem
    if ($Os.OSArchitecture -notmatch '64') { throw 'x64 Windows required' }
    if ([int]$Os.BuildNumber -lt 19044) { throw "Windows 10 21H2 (build 19044) or later required; found $($Os.BuildNumber)" }
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { throw 'nvidia-smi not found: install the NVIDIA driver first (owner decision; this script never installs drivers).' }
    $Driver = (nvidia-smi --query-gpu=driver_version --format=csv,noheader | Select-Object -First 1).Trim()
    # R528.33 is the Windows minimum for CUDA 12.x runtime libraries; compare as a version (551.86).
    if ([version]$Driver -lt [version]$MinimumDriver) { throw "NVIDIA driver $Driver is older than R$MinimumDriver; ask the owner before upgrading." }
    $Drive = Get-PSDrive ($RunnerRoot.Substring(0, 1))
    if ($Drive.Free / 1GB -lt $MinimumFreeGB) { throw ("Only {0:N0} GB free on {1}: (need {2} GB); pick another -RunnerRoot drive." -f ($Drive.Free / 1GB), $Drive.Name, $MinimumFreeGB) }
    Write-Host "OK: $($Os.Caption) build $($Os.BuildNumber), driver $Driver, $([int]($Drive.Free / 1GB)) GB free"

    Step "1. Standard account $ServiceUser (not an administrator)"
    $Registering = [bool]$RunnerToken -and -not (Get-Service 'actions.runner.*' -ErrorAction SilentlyContinue)
    $Password = $null
    if (Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue) {
        if ($Registering) {
            # The stored password cannot be read back; rotate it for the new service registration.
            # Never rotated while a service exists: that would break the service's logon.
            $Password = New-Password
            Set-LocalUser -Name $ServiceUser -Password (ConvertTo-SecureString $Password -AsPlainText -Force) -PasswordNeverExpires $true
            Write-Host "$ServiceUser exists; password rotated for registration."
        } else { Write-Host "$ServiceUser exists; password unchanged." }
    } else {
        $Password = New-Password
        $Secure = ConvertTo-SecureString $Password -AsPlainText -Force
        New-LocalUser -Name $ServiceUser -Password $Secure -PasswordNeverExpires -UserMayNotChangePassword -Description 'Murmur GitHub Actions runner' | Out-Null
        Add-LocalGroupMember -Group 'Users' -Member $ServiceUser -ErrorAction SilentlyContinue
        Write-Host "$ServiceUser created."
    }
    if (Get-LocalGroupMember -Group 'Administrators' -Member $ServiceUser -ErrorAction SilentlyContinue) { throw "$ServiceUser must not be an administrator" }
    if ($Password) {
        # Secret argument: direct call, output discarded, fixed error message (never the arguments).
        & cmdkey "/add:murmur-ci-runner" "/user:$ServiceUser" "/pass:$Password" *> $null
        if ($LASTEXITCODE -ne 0) { throw "cmdkey could not store the $ServiceUser password (exit code $LASTEXITCODE)" }
        Write-Host 'Password stored in Credential Manager (murmur-ci-runner).'
    }
    # Log on as a service (SeServiceLogonRight) through secedit export/import.
    $Sid = (New-Object Security.Principal.NTAccount $ServiceUser).Translate([Security.Principal.SecurityIdentifier]).Value
    $Policy = Join-Path $env:TEMP 'murmur-secpol.inf'
    Invoke-Native 'secedit' @('/export', '/cfg', $Policy, '/areas', 'USER_RIGHTS') | Out-Null
    $Lines = Get-Content $Policy
    $Right = $Lines | Where-Object { $_ -match '^SeServiceLogonRight' }
    if (-not $Right -or $Right -notmatch [regex]::Escape("*$Sid")) {
        $Lines = if ($Right) { $Lines -replace '^(SeServiceLogonRight\s*=\s*.*)$', "`$1,*$Sid" } else { $Lines -replace '^\[Privilege Rights\]$', "[Privilege Rights]`r`nSeServiceLogonRight = *$Sid" }
        $Lines | Set-Content $Policy -Encoding Unicode
        Invoke-Native 'secedit' @('/configure', '/db', (Join-Path $env:TEMP 'murmur-secpol.sdb'), '/cfg', $Policy, '/areas', 'USER_RIGHTS') | Out-Null
        Write-Host 'Granted "Log on as a service" (secedit).'
    } else { Write-Host '"Log on as a service" already granted.' }
    Remove-Item $Policy, (Join-Path $env:TEMP 'murmur-secpol.sdb') -ErrorAction SilentlyContinue

    Step '2. Toolchain (winget, machine-wide)'
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw 'winget is unavailable; install App Installer from the Microsoft Store (or report and use Chocolatey by hand).' }
    foreach ($Id in $Packages.Keys) {
        $Listed = winget list --id $Id --exact --accept-source-agreements 2>$null | Out-String
        if ($Listed -match [regex]::Escape($Id)) { Write-Host "$Id already installed"; continue }
        $WingetArgs = @('install', '--id', $Id, '--exact', '--silent', '--scope', 'machine', '--accept-package-agreements', '--accept-source-agreements')
        if ($Packages[$Id].ContainsKey('Override')) { $WingetArgs += @('--override', $Packages[$Id].Override) }
        & winget @WingetArgs
        if ($LASTEXITCODE -ne 0 -and $Id -ne 'Microsoft.EdgeWebView2Runtime') { throw "winget install $Id failed ($LASTEXITCODE)" }
    }
    # Rust for every account: machine-wide homes the service user can modify.
    [Environment]::SetEnvironmentVariable('RUSTUP_HOME', 'C:\rust\rustup', 'Machine')
    [Environment]::SetEnvironmentVariable('CARGO_HOME', 'C:\rust\cargo', 'Machine')
    [Environment]::SetEnvironmentVariable('LIBCLANG_PATH', "$env:ProgramFiles\LLVM\bin", 'Machine')
    New-Item -ItemType Directory -Force 'C:\rust' | Out-Null
    Invoke-Native 'icacls' @('C:\rust', '/grant', "${ServiceUser}:(OI)(CI)M", '/T', '/Q') | Out-Null
    $MachinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    foreach ($Entry in @('C:\rust\cargo\bin', "$env:ProgramFiles\LLVM\bin", "$env:ProgramFiles\CMake\bin", "${env:ProgramFiles(x86)}\NSIS", "$env:ProgramFiles\7-Zip")) {
        if (($MachinePath -split ';') -notcontains $Entry) { $MachinePath = "$MachinePath;$Entry" }
    }
    [Environment]::SetEnvironmentVariable('Path', $MachinePath, 'Machine')
    $env:RUSTUP_HOME, $env:CARGO_HOME = 'C:\rust\rustup', 'C:\rust\cargo'
    Update-Path
    Invoke-Native 'rustup' @('default', 'stable')
    Invoke-Native 'rustup' @('target', 'add', 'x86_64-pc-windows-msvc')
    Write-Host 'New PATH entries are visible to new processes; sign out/in or reboot if a tool is not found.'

    Step "3. Offline GPU test model in $ModelRoot"
    $Model = Join-Path $ModelRoot 'distil-small.en'
    if (-not (Test-Path (Join-Path $Model '.complete'))) {
        $Tools = 'C:\murmur-tools\hf'
        if (-not (Test-Path "$Tools\Scripts\python.exe")) { Invoke-Native 'py' @('-3.12', '-m', 'venv', $Tools) }
        Invoke-Native "$Tools\Scripts\python.exe" @('-m', 'pip', 'install', '--upgrade', 'pip', 'huggingface_hub>=0.34,<2')
        $Fetch = Join-Path $env:TEMP 'murmur-stage-model.py'
        @"
from huggingface_hub import snapshot_download
snapshot_download("Systran/faster-distil-whisper-small.en", local_dir=r"$Model",
                  allow_patterns=["model.bin","config.json","tokenizer.json","vocabulary.*","preprocessor_config.json"])
"@ | Set-Content -Encoding utf8 $Fetch
        Invoke-Native "$Tools\Scripts\python.exe" @($Fetch)
        Remove-Item $Fetch
        Set-Content (Join-Path $Model '.complete') '1'
    } else { Write-Host 'Model already staged.' }
    foreach ($Name in @('model.bin', 'config.json', 'tokenizer.json')) {
        if (-not (Test-Path (Join-Path $Model $Name))) { throw "Staged model lacks $Name" }
    }
    Invoke-Native 'icacls' @($ModelRoot, '/grant', "${ServiceUser}:(OI)(CI)RX", '/T', '/Q') | Out-Null

    Step '4. Runner service'
    if (Get-Service 'actions.runner.*' -ErrorAction SilentlyContinue) {
        Write-Host 'A runner service is already registered; leaving it as is (remove it with config.cmd remove to re-register).'
    } elseif (-not $Registering) {
        Write-Host 'Skipped: pass -RunnerToken <one-time token from the owner> to register the runner.'
    } else {
        New-Item -ItemType Directory -Force $RunnerRoot | Out-Null
        if (-not (Test-Path (Join-Path $RunnerRoot 'config.cmd'))) {
            $Release = Invoke-RestMethod 'https://api.github.com/repos/actions/runner/releases/latest' -Headers @{ 'User-Agent' = 'murmur-provision' }
            $Asset = $Release.assets | Where-Object { $_.name -match '^actions-runner-win-x64-[\d.]+\.zip$' } | Select-Object -First 1
            # The release notes carry the SHA-256 per package between these markers.
            if ($Release.body -notmatch '<!-- BEGIN SHA win-x64 -->([0-9a-f]{64})<!-- END SHA win-x64 -->') { throw 'Runner release lists no win-x64 SHA-256' }
            $Expected = $Matches[1]
            $Zip = Join-Path $env:TEMP $Asset.name
            Invoke-WebRequest $Asset.browser_download_url -OutFile $Zip -UseBasicParsing
            if ((Get-FileHash $Zip -Algorithm SHA256).Hash -ne $Expected.ToUpper()) { throw 'Runner download SHA-256 mismatch' }
            Expand-Archive $Zip $RunnerRoot -Force
            Remove-Item $Zip
        }
        Invoke-Native 'icacls' @($RunnerRoot, '/grant', "${ServiceUser}:(OI)(CI)M", '/T', '/Q') | Out-Null
        Push-Location $RunnerRoot
        try {
            # Secret arguments (token, password): direct call with a fixed error message, so a
            # failure never writes them into the transcript. config.cmd does not echo them.
            & .\config.cmd --url $RepositoryUrl --token $RunnerToken --name $RunnerName `
                --labels $Labels --work _work --runasservice --windowslogonaccount $ServiceUser `
                --windowslogonpassword $Password --unattended --replace
            if ($LASTEXITCODE -ne 0) { throw "Runner registration (config.cmd) failed with exit code $LASTEXITCODE; see its output above" }
        } finally { Pop-Location }
        Get-Service 'actions.runner.*' | Set-Service -StartupType Automatic
        Get-Service 'actions.runner.*' | Where-Object Status -ne 'Running' | Start-Service
    }
    $Password = $null
    Show-Report
    Write-Host "`nCI jobs run in session 0 and cannot show windows or receive hotkeys: interactive parity checks follow docs/parity/windows-checklist.md in the logged-in session."
} finally {
    Stop-Transcript | Out-Null
}
