# Capture the Windows half of the Settings screenshot pairs (docs/parity/README.md naming:
# windows-<theme>-<page>-<scale>.png) in the LOGGED-IN desktop session on Edison. A CI service
# (session 0) has no visible desktop and cannot run this.
#
#   powershell -ExecutionPolicy Bypass -File scripts\parity\capture-windows.ps1 -App <murmur.exe> [-Out <dir>]
#
# What it automates: a throwaway profile (APPDATA/LOCALAPPDATA under %TEMP%, setup postponed,
# history off), launching Murmur, opening Settings, switching light/dark through config.toml (the
# app reloads it), clicking each page button through UI Automation (WebView2 exposes the buttons
# by name) and saving the Settings window with System.Drawing at the current scaling. Run once
# at 100 % and once at 150 % display scaling. It does NOT capture overlay states, hotkeys, typing
# or tray behaviour: those follow docs/parity/windows-checklist.md by hand.
#
# Written without a Windows machine: verified in source only, never run.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$App,
    [string]$Out = (Join-Path (Get-Location) 'docs\parity\captures\windows'),
    [string[]]$Themes = @('light', 'dark'),
    [string[]]$Pages = @('General', 'Components', 'Audio', 'Behavior', 'Privacy', 'Companion')
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run on Windows in the logged-in desktop session.' }
if ([Environment]::UserInteractive -eq $false) { throw 'No interactive desktop (service session); log in and run it there.' }

Add-Type -AssemblyName System.Drawing, UIAutomationClient, UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class MurmurCapture {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out RECT rect, int size);
}
'@
# Physical pixels for every coordinate (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4).
[void][MurmurCapture]::SetProcessDpiAwarenessContext([IntPtr]::new(-4))

function Write-Config([string]$Theme) {
    # Partial config: every other value takes the app's default.
    @"
[general]
theme = "$Theme"
autostart = false

[privacy]
history_enabled = false

[setup]
postponed = true
"@ | Set-Content -Encoding utf8 (Join-Path $env:APPDATA 'Murmur\config.toml')
}

function Find-Settings([int]$TimeoutSeconds = 30) {
    $Root = [Windows.Automation.AutomationElement]::RootElement
    $Condition = New-Object Windows.Automation.PropertyCondition ([Windows.Automation.AutomationElement]::NameProperty), 'Murmur Settings'
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $Deadline) {
        $Window = $Root.FindFirst([Windows.Automation.TreeScope]::Children, $Condition)
        if ($Window) { return $Window }
        Start-Sleep -Milliseconds 250
    }
    throw 'The Murmur Settings window did not appear'
}

function Invoke-Page($Window, [string]$Name) {
    $Condition = New-Object Windows.Automation.AndCondition (
        (New-Object Windows.Automation.PropertyCondition ([Windows.Automation.AutomationElement]::NameProperty), $Name),
        (New-Object Windows.Automation.PropertyCondition ([Windows.Automation.AutomationElement]::ControlTypeProperty), ([Windows.Automation.ControlType]::Button)))
    $Button = $Window.FindFirst([Windows.Automation.TreeScope]::Descendants, $Condition)
    if (-not $Button) { throw "No '$Name' page button in the accessibility tree" }
    $Button.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
    Start-Sleep -Milliseconds 700
}

function Save-Window($Window, [string]$Path) {
    $Handle = [IntPtr]$Window.Current.NativeWindowHandle
    [void][MurmurCapture]::SetForegroundWindow($Handle)
    Start-Sleep -Milliseconds 300
    $Rect = New-Object MurmurCapture+RECT
    # DWMWA_EXTENDED_FRAME_BOUNDS (9): the visible frame without the invisible resize border.
    if ([MurmurCapture]::DwmGetWindowAttribute($Handle, 9, [ref]$Rect, 16) -ne 0) {
        [void][MurmurCapture]::GetWindowRect($Handle, [ref]$Rect)
    }
    $Width = $Rect.Right - $Rect.Left; $Height = $Rect.Bottom - $Rect.Top
    $Bitmap = New-Object Drawing.Bitmap $Width, $Height
    $Graphics = [Drawing.Graphics]::FromImage($Bitmap)
    try {
        $Graphics.CopyFromScreen($Rect.Left, $Rect.Top, 0, 0, $Bitmap.Size)
        $Bitmap.Save($Path, [Drawing.Imaging.ImageFormat]::Png)
    } finally { $Graphics.Dispose(); $Bitmap.Dispose() }
    return "$Width x $Height"
}

$Work = Join-Path $env:TEMP "murmur-capture-$([guid]::NewGuid())"
$SavedAppData, $SavedLocal = $env:APPDATA, $env:LOCALAPPDATA
$env:APPDATA = Join-Path $Work 'Roaming'
$env:LOCALAPPDATA = Join-Path $Work 'Local'
New-Item -ItemType Directory -Force (Join-Path $env:APPDATA 'Murmur'), $env:LOCALAPPDATA, $Out | Out-Null
$Process = $null
$Records = @()
try {
    # Murmur is single-instance: a running copy would receive this launch instead.
    if (Get-Process murmur -ErrorAction SilentlyContinue) { throw 'Quit Murmur (tray > Quit) before capturing.' }
    Write-Config $Themes[0]
    $Process = Start-Process $App -PassThru
    $Window = Find-Settings
    $Scale = [int][math]::Round([MurmurCapture]::GetDpiForWindow([IntPtr]$Window.Current.NativeWindowHandle) / 96 * 100)
    foreach ($Theme in $Themes) {
        Write-Config $Theme
        Start-Sleep -Seconds 2   # config watcher reload
        foreach ($Page in $Pages) {
            Invoke-Page $Window $Page
            $File = Join-Path $Out ("windows-{0}-{1}-{2}.png" -f $Theme, $Page.ToLower(), $Scale)
            $Size = Save-Window $Window $File
            $Records += "| ``$(Split-Path -Leaf $File)`` | $Theme | $Page | $Scale % | $Size px |"
        }
    }
    $Os = Get-CimInstance Win32_OperatingSystem
    $WebView = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue).pv
    @(
        "# Windows Settings captures ($Scale %)", '',
        "- App: $((& $App --version | Out-String).Trim())",
        "- Windows: $($Os.Caption) $($Os.Version)",
        "- WebView2: $WebView",
        "- Captured: $(Get-Date -Format s) by scripts/parity/capture-windows.ps1 (throwaway profile)", '',
        '| File | Theme | Page | Scaling | Window |', '| --- | --- | --- | --- | --- |'
    ) + $Records | Set-Content -Encoding utf8 (Join-Path $Out "manifest-$Scale.md")
    Write-Host "Saved $($Records.Count) captures and manifest-$Scale.md to $Out. Review them for personal data before committing."
} finally {
    if ($Process -and -not $Process.HasExited) { Stop-Process -Id $Process.Id -Force }
    $env:APPDATA, $env:LOCALAPPDATA = $SavedAppData, $SavedLocal
    Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue
}
