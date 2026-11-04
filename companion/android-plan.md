# Murmur companion for Android — plan

Status: **approved direction, not started.** Phase 3. No Android code is written until the
increments below are accepted.

Supersedes the earlier control-only sketch: the phone is now a **remote microphone**, the four
**control actions**, and the partner that lets the **Flipper drive the phone's microphone**.

## Decisions taken

| Question | Decision |
| --- | --- |
| Where recognition happens | **The host.** The phone streams raw PCM and Murmur transcribes with the existing engine, so enhance, overlay, live text and injection behave exactly as they do for the built-in microphone. |
| First milestone | **Remote mic plus the four buttons**: dictate, enhance, accept, dismiss. |
| Transport | **LAN WebSocket over WiFi**, mDNS discovery, token pairing. |
| Reachability | **LAN only.** Never the public internet, and enforced in code rather than by convention. |
| Distribution | **Sideload APK** attached to the GitHub release. No store account. |

The Flipper cannot be a microphone: buzzer only (100–2,500 Hz), 256 KB RAM, no audio input path,
no I2S. It stays a control surface — but it can now *use the phone's* microphone, which is the
point of this phase.

## Topology

**The phone is a standalone remote.** It talks to Murmur directly and needs no Flipper, no
pairing with any other device, and no companion hardware: install the APK, pair once with Murmur
over the LAN, and the microphone and all four buttons work. The Flipper is optional and, when
present, adds nothing the phone requires.

Two independent links to one host, and the host coordinates them. There is no phone↔Flipper
pairing, no BLE between them, and no phone-as-relay: the rejected option C needed two pairing UX
and two hops for nothing.

```
Flipper ── BLE/USB ──┐
                     ├──► Murmur host ──► STT ──► enhance ──► overlay/injection
Phone ──── wss/LAN ──┘        │
                              └── live text + state back to both
```

**Flipper button → phone microphone.** Pressing OK on the Flipper sends `B` as it does today; the
host starts a dictation turn and, if the audio source is the phone, sends `MIC START <stream>`.
The phone streams; transcription and live text flow back to the Flipper exactly as with the local
microphone. The Flipper never needs to know the audio came from the phone.

**Audio source policy**, in `[companion]`: `source = "auto" | "local" | "phone"`.
`auto` uses the phone when one is paired and connected, otherwise the local microphone. The
Settings card shows which source is live per turn.

## Host (Murmur, Rust)

- New `[companion.network]` block: `enabled = false`, `bind = "127.0.0.1"`, `port = 8787`,
  `mdns = true`, `require_tls = true`.
- Listener is opt-in and authenticated; there is no unauthenticated mode.
- **LAN-only, enforced at accept time.** Any peer whose source address is not private —
  RFC1918, IPv6 ULA (`fc00::/7`) or link-local — is rejected before authentication, logged as a
  refused connection, and counted. So even if someone forwards the port or the host is dual-homed,
  the socket refuses to serve a public peer. No UPnP, no NAT traversal, no relay: the app never
  opens a path to the internet, and the docs say so plainly.
- **Transport security: TLS 1.3, `wss://` only.** Murmur generates a self-signed certificate on
  first enable and shows its SHA-256 fingerprint in the pairing QR. The phone pins that
  fingerprint, so a self-signed cert is enough and no CA is involved; a changed fingerprint is a
  hard stop with an explicit re-pair, never a prompt to "trust anyway". Plaintext `ws://` is not
  offered at all.
- Pairing: six-digit code plus QR containing host, port, fingerprint and one-time code. The phone
  sends `HELLO 5 <app-version> phone CAP mic` then `AUTH <code>` over the pinned TLS session; the
  host replies `AUTH OK <token>`. Tokens are per device, stored, constant-time compared,
  rate-limited (5 attempts then lockout), and individually revocable in Settings.
- mDNS `_murmur._tcp` with TXT `proto`, `app`, `mic=1` — advertised on the local network only.
  Manual `host:port` entry remains the escape hatch where mDNS is blocked.

## Protocol additions

The text verbs stay `B`/`S`/`A`/`CFG`. The network link adds three messages and nothing else:

- `MIC START <stream-id>` — host asks the phone to stream (sent at turn start).
- `MIC STOP <stream-id>` — turn over, phone stops capturing.
- `HELLO ... CAP mic` — capability negotiation.

These are additive and only ever sent to a link that advertised `mic` in `HELLO`. The Flipper
never advertises it and never receives them, **so its protocol stays at 4 and no `.fap` change is
needed.** `protocol.md` gains a section documenting the network extensions and the audio framing;
`scripts/check-protocol-version.mjs` keeps the three host/device mirrors in step as today.

## Audio framing

- PCM16, mono, 16 kHz, 20 ms frames (640-byte payload).
- Header: magic `MRMA`, version, flags, stream id, sequence, sample rate, channels.
- **Backpressure drops the oldest frame, never grows a queue.** A phone on a weak signal must not
  be able to make Murmur allocate without bound.
- Frames are never written to disk, and transcript text never reaches a log — the invariant the
  Flipper link already enforces. Audio in memory only, for the duration of the turn.
- `RemoteAudioSource` feeds the same capture → STT path as `cpal`, so nothing downstream changes.

## Android app (Kotlin, Compose)

- One activity: connection state, a large push-to-talk surface, the four control buttons, and the
  host list from mDNS with manual entry.
- `AudioRecord` at 16 kHz mono, no resampling in Kotlin, no software AEC in v1.
- TLS with pinned fingerprint, token in memory plus encrypted storage.
- Foreground service for microphone use so the buttons and the mic work with the phone in a
  pocket — one low-priority notification channel, no spam.
- Failures shown, not swallowed: no host, refused LAN source, unauthorised token, fingerprint
  mismatch, link dropped mid-dictation.

## Increments

1. **Host network layer, no audio** — config block, TLS listener with the LAN guard, pairing and
   tokens, the four verbs, state pushes. Verified with a CLI client before any Android code.
   mDNS discovery is deliberately deferred to increment 3 so the first increment has no new
   discovery surface; the manual `host:port` path exists from the start.
2. **Audio ingest and coordination** — framing, backpressure, `RemoteAudioSource`,
   `MIC START/STOP`, source policy. Verified by pressing OK on the Flipper with a CLI client
   streaming a WAV as the phone's stand-in.
3. **APK, control only** — connect, pair, four buttons, state display. Useful on its own.
4. **APK, remote mic** — push-to-talk streaming, live text mirrored from the host, Flipper-driven
   turns.
5. **Release** — `murmur-companion-<version>.apk` attached by CI behind `ENABLE_ANDROID`,
   docs updated.

Each increment lands as its own PR, review findings fixed before merge, as in Phases 1–2.

## Build

- SDK already present: `/home/zethrus/Android/Sdk`, build-tools 34/35/36, platforms `android-35`
  and `android-37.0`, NDK, `adb`.
- System Java is **25**, which AGP may refuse; pin a JDK 21 toolchain in `gradle.properties`
  instead of depending on the system JDK.
- CI builds the APK on the self-hosted runner behind `ENABLE_ANDROID` and attaches it to the
  release. One local keystore, no secrets in the repo. `minSdk 26`.

## Out of scope

Play Store, BLE audio from the phone, Flipper as an audio device, on-device Android speech
recognition, iOS, and any path that exposes Murmur beyond the LAN.

## Risks

- **WiFi latency and jitter** on the audio path; the 20 ms framing and drop-oldest rule bound the
  damage, and a stalled link ends the turn rather than recording garbage.
- **mDNS is blocked or flaky on some routers**; manual entry is the documented fallback.
- **Self-signed rotation** invalidates pairing; re-pairing must be an explicit, visible action.
- **Phone sleeping** kills the microphone without a foreground service, which is why one exists.
