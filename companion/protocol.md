# Murmur companion protocol, version 1

Line-delimited ASCII. One message per line, terminated by `\n`. Maximum 160 bytes per
line, including the terminator. No JSON, no allocation on the device, no binary frames.

PROTOCOL_VERSION = 1

`PROTOCOL_VERSION` is defined once here and mirrored in exactly two places:

| File | Symbol |
| --- | --- |
| `companion/flipper/src/protocol.h` | `MURMUR_PROTOCOL_VERSION` |
| `src-tauri/src/companion/protocol.rs` | `PROTOCOL_VERSION` |

`node scripts/check-protocol-version.mjs` fails the build when any of the three disagree.

## Invariant

The protocol carries **verbs and state only**. Transcript text, audio, model identifiers,
file paths and configuration values other than the button map never cross this link. A
unit test enforces that no message produced by `protocol.rs` contains a dictation string.

## Handshake

Both ends send a `HELLO` line as their first message and may send it again at any time to
renegotiate:

```
HELLO <protocol> <app_version> <role>
```

- `protocol` — integer, currently `1`
- `app_version` — the Murmur version, e.g. `0.2.2`
- `role` — `flipper`, `host`, or (later) `phone`

A receiver that sees a `protocol` it does not implement keeps the link open, refuses all
action messages, and reports the mismatch to the user. Forward compatibility: unknown
message types are ignored, never fatal.

## Flipper to host

| Line | Meaning |
| --- | --- |
| `B OK` / `B UP` / `B DOWN` / `B BACK` / `B LEFT` / `B RIGHT` | short press |
| `B OK LONG` / `B BACK LONG` | long press (>= 500 ms) |
| `P` | heartbeat |

## Host to Flipper

| Line | Meaning |
| --- | --- |
| `S IDLE` | nothing in progress |
| `S LISTEN` | recording |
| `S THINK` | transcribing |
| `S READY` | a rewrite suggestion is available |
| `S ERROR <code>` | last action failed; code is a short token, e.g. `timeout` |
| `A <verb> <result>` | acknowledgement, e.g. `A DICTATE ON`, `A ACCEPT OK`, `A ENHANCE STARTED` |
| `CFG <KEY>=<VERB> ...` | button map pushed from Settings |
| `P` | heartbeat reply |

`S` lines are sent only when the state changes, throttled to 4 Hz.

## Button map (`CFG`)

Alphabetical keys, verbs joined with single spaces:

```
CFG BACK=DISMISS DOWN=ACCEPT OK=DICTATE_TOGGLE OK_LONG=CANCEL UP=ENHANCE
```

Verbs: `DICTATE_TOGGLE`, `ENHANCE`, `ACCEPT`, `DISMISS`, `CANCEL`, `NONE`. The host is
the source of truth; the Flipper caches the last `CFG` line so the map survives a
reconnect while Murmur is not running.

## Liveness

Both ends send `P` every 5 s. A heartbeat is presence only and is **never** answered with
another `P` — replying would make both ends ping-pong forever. No traffic of any kind for
15 s means the link is dead: the Flipper shows `Murmur offline` and retries, the host closes
the transport and retries with backoff (1 s, doubling to 5 s).

## Transports

- **USB** — the Flipper app claims `usb_cdc_single`; the host opens the CDC device.
- **BLE** — the Flipper app runs the BLE serial profile; the host connects as a GATT
  client. Protocol identical.

## Change policy

Bump `PROTOCOL_VERSION` for any change that a version-1 peer would misunderstand. Adding
a new optional message type or a new verb does not require a bump; removing or renaming
does.
