# Murmur companion protocol, version 2

Line-delimited ASCII. One message per line, terminated by `\n` (a `\r\n` peer is also
accepted). Maximum 220 bytes per line, including the terminator. No JSON, no binary frames,
no allocation on the hot path.

PROTOCOL_VERSION = 2

`PROTOCOL_VERSION` is defined once here and mirrored in exactly two places:

| File | Symbol |
| --- | --- |
| `companion/flipper/src/protocol.h` | `MURMUR_PROTOCOL_VERSION` |
| `src-tauri/src/companion/protocol.rs` | `PROTOCOL_VERSION` |

`node scripts/check-protocol-version.mjs` fails the build when any of the three disagree.

## What version 2 adds

`T` (live text), `G` (the suggestion), `Q` (device battery), `L` (link quality on the device
header) and timed probes (`P <token>`). Everything in version 1 is unchanged, so a v2 host
still drives a v1 device, minus the new screens.

## Text and privacy

Version 1 refused to carry dictation content. Version 2 carries it for live on-device
feedback, under rules that keep the original promise about persistence:

- **Memory only.** Neither end writes text to a log, a file or a database. The host builds
  `T`/`G` through `protocol::text`/`protocol::suggestion`, which never trace; the device
  holds it in RAM and clears it when a turn ends, on dismiss, or when a new `S LISTEN`
  arrives.
- **Flattened.** Control characters become spaces, runs of spaces collapse, trailing space
  is trimmed, and the payload is truncated to fit one line. A transcript cannot smuggle a
  newline into the framing.
- **Replacement, not append.** Every `T` replaces the device's live buffer, so a dropped
  line heals on the next update.
- Everything else — verbs, state, counters — still comes from a closed vocabulary.

## Handshake

Both ends send a `HELLO` line as their first message and may send it again at any time to
renegotiate:

```
HELLO <protocol> <app_version> <role>
```

- `protocol` — integer, currently `2`
- `app_version` — the Murmur version, e.g. `0.3.0`
- `role` — `flipper`, `host`, or (later) `phone`

A receiver that sees a `protocol` it does not implement keeps the link open, refuses the
message types it does not know, and reports the mismatch to the user. Unknown message types
are ignored, never fatal.

## Flipper to host

| Line | Meaning |
| --- | --- |
| `B OK` / `B UP` / `B DOWN` / `B BACK` / `B LEFT` / `B RIGHT` | short press |
| `B OK LONG` / `B BACK LONG` | long press (>= 500 ms) |
| `P` | heartbeat |
| `P <token>` | echo of a timed probe, verbatim (v2) |
| `Q <percent>` | battery percentage, every 30 s (v2) |

## Host to Flipper

| Line | Meaning |
| --- | --- |
| `S IDLE` | nothing in progress |
| `S LISTEN` | recording |
| `S THINK` | transcribing |
| `S READY` | a rewrite suggestion is available |
| `S ERROR <code>` | last action failed; code is a short token, e.g. `timeout` |
| `A <verb> <result>` | acknowledgement, e.g. `A DICTATE ON`, `A ACCEPT OK` |
| `CFG <KEY>=<VERB> ...` | button map pushed from Settings |
| `T <text>` | live dictation text (v2) |
| `G <text>` | the enhancement suggestion (v2) |
| `L <latency_ms> <rssi>` | link quality for the device header (v2) |
| `P` | heartbeat |
| `P <token>` | timed probe, echoed by the device (v2) |

`S` lines are sent only when the state changes. `T` is throttled to 4 Hz and coalesced so a
fast partial cannot flood a BLE link; `G` is sent once per suggestion.

## Button map (`CFG`)

Alphabetical keys, verbs joined with single spaces:

```
CFG BACK=DISMISS DOWN=ACCEPT OK=DICTATE_TOGGLE OK_LONG=CANCEL UP=ENHANCE
```

Verbs: `DICTATE_TOGGLE`, `ENHANCE`, `ACCEPT`, `DISMISS`, `CANCEL`, `NONE`. The host is the
source of truth; the Flipper caches the last `CFG` line so the map survives a reconnect
while Murmur is not running. `BACK LONG` is reserved for exiting the app and is never sent.

## Liveness and latency

Both ends send a bare `P` every 5 s. A bare heartbeat is presence only and is **never**
answered with another `P` — replying would make both ends ping-pong forever.

A host that wants a round-trip time sends `P <digits>`. The device echoes that token once,
verbatim, and the host publishes the measured difference to the device as `L`. No traffic
of any kind for 15 s means the link is dead: the device shows `Murmur offline`, the host
closes the transport and retries with backoff (1 s, doubling to 5 s).

## Transports

- **USB** — the Flipper app claims `usb_cdc_single`; the host opens the CDC device.
- **BLE** — the Flipper app runs the BLE serial profile (`8fe5b3d5-…` service, write
  `…d6`, notify `…d7`); the host connects as a GATT central over `btleplug`. Bonding is the
  firmware's: the Flipper shows a PIN, the host enters it once and keeps the key.
- Both may run at once. A line is always answered on the transport it arrived on, and USB
  wins when both are talking, which is what makes `transport = "auto"` safe.

## Change policy

Bump `PROTOCOL_VERSION` for any change that an older peer would misinterpret. Adding an
optional message type or a new verb does not require a bump; removing or renaming does.
