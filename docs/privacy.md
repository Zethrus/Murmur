# Privacy

Speech recognition runs locally: no account, telemetry, cloud transcription or
saved microphone audio. The speech engine never uses the network: it starts with
Hugging Face offline mode and telemetry disabled and only reads model folders the
app has verified.

## What is downloaded, from where, and when

Nothing is downloaded at startup. Murmur makes a network request only after you
click one of these buttons:

| Click | Downloads | From |
| --- | --- | --- |
| **Download and install** (setup), **Install**, **Update**, **Reinstall**, **Retry**, **Resume** | the components you chose | engine and GPU add-on: GitHub Releases (`github.com`, redirected to GitHub's asset hosts); speech and Enhance models: Hugging Face (`huggingface.co` and its CDN) at a pinned commit |
| **Check for updates** | the signed component list (`components-manifest.json`, its signature, Murmur's public key) | GitHub Releases (latest published release) |
| Enhance **Install** / **Download and enable** | the Enhance model | Hugging Face, pinned commit |

These requests carry no speech, transcripts, settings or identifiers beyond what
any HTTPS download carries (your IP address and a `Murmur/<version>` user agent).
Every file is checked against a manifest signed with Murmur's release key before
it is used; see [components](components.md). Dependency installation and builds
from source also need the network; "local" describes dictation, not software
distribution.

History is off by default. Enabling it stores local transcripts in `history.sqlite`;
retention pruning occurs at startup and daily. Disabling it stops new entries but
does not delete old ones; use Privacy → Clear history or uninstall with `--purge`.
Diagnostic logs rotate at 2 MiB with two backups and are not intended to contain
transcripts. Treat debug logs as sensitive when sharing. Clipboard managers,
destination applications and filesystem backups may independently retain text.

## Companion data

Enabling the Flipper companion sends live text and rewrite suggestions to the
device over USB or Bluetooth. The Flipper keeps short previews in RAM and stores
the button map on its SD card. Turn the companion off when you do not want text
sent to it.

The experimental phone listener is off by default. If enabled, it accepts paired
clients over TLS and checks that peer addresses are local. It stores pairing
tokens and its TLS identity under the app data directory. Do not share that
directory or forward the listener through a proxy. A proxy can make a remote peer
appear local. The Android app is not included in this release.

On Linux, typed dictation briefly passes through a private directory under
`XDG_RUNTIME_DIR`. The payload is removed after delivery and the directory is
removed when the helper stops. A force-killed process can leave temporary files
until the runtime directory is cleared. This is text, not a recording.

[Back to the README](../README.md)
