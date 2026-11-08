Murmur for Windows (portable)
=============================

This zip holds only the app, murmur.exe. Keep it anywhere you can write to, then run it.

On first run Murmur opens its setup. It downloads the speech engine, a speech model and,
when offered, the NVIDIA GPU add-on. Every download is checked against a list signed by the
Murmur release key before anything is installed. Nothing is downloaded before you confirm
the sizes in the setup summary.

Files Murmur writes:
  Settings:                    %APPDATA%\Murmur\config.toml
  Engine, models, log, history %LOCALAPPDATA%\Murmur\

Requirements: Windows 10 1809 or later (x64) and the Microsoft Edge WebView2 Runtime, which
Windows 11 and current Windows 10 already include. The installer (Murmur_*_x64-setup.exe)
installs WebView2 if it is missing; that step needs an internet connection.

This build is not code-signed (no Authenticode certificate yet). Windows SmartScreen may warn
"Windows protected your PC"; choose "More info" and "Run anyway" only if you downloaded the
file from https://github.com/Zethrus/Murmur/releases and its SHA-256 matches SHA256SUMS.

Known Windows limitations (administrator windows, microphone privacy, SmartScreen) are in
docs/troubleshooting.md: https://github.com/Zethrus/Murmur/blob/master/docs/troubleshooting.md
