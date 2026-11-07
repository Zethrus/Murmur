# Dormant Authenticode signing. Does nothing unless the repository secrets
# WINDOWS_CERT_PFX_BASE64 and WINDOWS_CERT_PASSWORD exist (none today: builds are unsigned).
#
#   authenticode.ps1 -Import          decode the PFX into a temporary file, import it into
#                                     Cert:\CurrentUser\My, delete the file, export
#                                     MURMUR_SIGN_THUMBPRINT to GITHUB_ENV
#   authenticode.ps1 -Sign <file>     signtool sign (SHA-256, RFC 3161 timestamp) + verify;
#                                     called by Tauri's bundle.windows.signCommand
#   authenticode.ps1 -Remove          delete the imported certificate and its private key
#
# Never prints the password or the PFX. Verified in source only (no certificate exists).
[CmdletBinding(DefaultParameterSetName = 'Sign')]
param(
    [Parameter(ParameterSetName = 'Import')][switch]$Import,
    [Parameter(ParameterSetName = 'Remove')][switch]$Remove,
    [Parameter(ParameterSetName = 'Sign', Position = 0)][string]$Sign,
    [string]$TimestampUrl = 'http://timestamp.digicert.com'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Find-SignTool {
    $Command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    $Kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    $Found = Get-ChildItem $Kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $Found) { throw 'signtool.exe not found (install the Windows SDK)' }
    return $Found.FullName
}

if ($Import) {
    if (-not $env:WINDOWS_CERT_PFX_BASE64 -or -not $env:WINDOWS_CERT_PASSWORD) {
        Write-Host 'No Authenticode certificate configured: the build stays unsigned.'
        exit 0
    }
    $Pfx = Join-Path ([IO.Path]::GetTempPath()) "murmur-$([guid]::NewGuid()).pfx"
    try {
        [IO.File]::WriteAllBytes($Pfx, [Convert]::FromBase64String($env:WINDOWS_CERT_PFX_BASE64))
        $Password = ConvertTo-SecureString $env:WINDOWS_CERT_PASSWORD -AsPlainText -Force
        $Certificate = Import-PfxCertificate -FilePath $Pfx -CertStoreLocation Cert:\CurrentUser\My -Password $Password
    } finally {
        if (Test-Path $Pfx) { Remove-Item $Pfx -Force }
    }
    if (-not $Certificate.HasPrivateKey) { throw 'The PFX has no private key' }
    if ($env:GITHUB_ENV) { "MURMUR_SIGN_THUMBPRINT=$($Certificate.Thumbprint)" | Out-File $env:GITHUB_ENV -Append }
    $env:MURMUR_SIGN_THUMBPRINT = $Certificate.Thumbprint
    Write-Host "Authenticode certificate imported: $($Certificate.Subject), expires $($Certificate.NotAfter)"
    exit 0
}

if ($Remove) {
    if ($env:MURMUR_SIGN_THUMBPRINT) {
        $Path = "Cert:\CurrentUser\My\$($env:MURMUR_SIGN_THUMBPRINT)"
        if (Test-Path $Path) { Remove-Item $Path -DeleteKey }
        Write-Host 'Authenticode certificate removed from the runner.'
    }
    exit 0
}

if (-not $Sign) { throw 'Usage: authenticode.ps1 -Import | -Sign <file> | -Remove' }
if (-not $env:MURMUR_SIGN_THUMBPRINT) {
    # Tauri calls this only when signCommand was configured; reaching here is a pipeline bug.
    throw "No imported certificate; refusing to pretend $Sign is signed"
}
$SignTool = Find-SignTool
& $SignTool sign /fd SHA256 /sha1 $env:MURMUR_SIGN_THUMBPRINT /tr $TimestampUrl /td SHA256 /d Murmur $Sign
if ($LASTEXITCODE -ne 0) { throw "signtool sign failed for $Sign" }
& $SignTool verify /pa $Sign
if ($LASTEXITCODE -ne 0) { throw "signtool verify failed for $Sign" }
