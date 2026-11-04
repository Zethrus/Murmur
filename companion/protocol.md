# Murmur companion protocol, version 4

Line-delimited ASCII. One message per line, terminated by `\n` (a `\r\n` peer is also
accepted). Maximum 220 bytes per line, including the terminator. No JSON, no binary frames,
no allocation on the hot path.

PROTOCOL_VERSION = 4

`PROTOCOL_VERSION` is defined once here and mirrored in exactly two places:

| File | Symbol |
| --- | --- |
| `companion/flipper/src/protocol.h` | `MURMUR_PROTOCOL_VERSION` |
| `src-tauri/src/companion/protocol.rs` | `PROTOCOL_VERSION` |

`node scripts/check-protocol-version.mjs` fails the build when any of the three disagree.

## What version 4 adds

`D DISCARD` confirms the device's discard prompt independently of the button map.
`D ACCEPT <index>` identifies the selected RAM history preview (0 is newest). The host
cannot accept an old dictation from this preview because it has no host dictation ID and
only 40 characters; it re-sends the current suggestion if one exists, or replies
`A ACCEPT UNAVAILABLE`. It never accepts a different suggestion by accident.

## What version 3 added

`PTT <0|1>` announces push-to-talk mode, and `B OK PRESS` / `B OK RELEASE` report
the two edges of the OK key while that mode is enabled. Version 2 added `T`, `G`, `Q`,
`L`, and timed probes. Peers with different protocol versions report a mismatch.

## What version 2 added

`T` (live text), `G` (the suggestion), `Q` (device battery), `L` (link quality on the device
header) and timed probes (`P <token>`). Everything in version 1 is unchanged, so a v2 host
retains the original button and state messages.

## Text and privacy

Version 1 refused to carry dictation content. Version 2 carries it for live on-device
feedback, under rules that keep the original promise about persistence:

- **Memory only.** Neither end writes text to a log, a file or a database. The host builds
  `T`/`G` through `protocol::text`/`protocol::suggestion`, which never trace; the device
  holds live text in RAM and keeps only the first 40 characters of the last three finished
  dictations in a RAM ring. The live view retains a nonempty final transcript through `S IDLE`
  and `S READY`, and clears when a new turn enters `S LISTEN`. A direct `S LISTEN` to `S IDLE`
  transition aborts the turn and clears its preview.
- **Flattened.** Control characters become spaces, runs of spaces collapse, trailing space
  is trimmed, and the payload is truncated to fit one line. A transcript cannot smuggle a
  newline into the framing.
- **Replacement, not append.** Every `T` replaces the device's live buffer, so a dropped
  line heals on the next update. A bare `T` (or `T `) clears the live buffer and removes
  its provisional RAM history entry when the final transcript is empty. Both forms fit
  within the 220-byte line limit.
- Everything else — verbs, state, counters — still comes from a closed vocabulary.

## Handshake

Both ends send a `HELLO` line as their first message and may send it again at any time to
renegotiate:

```
HELLO <protocol> <app_version> <role>
```

- `protocol` — integer, currently `4`
- `app_version` — the Murmur version, e.g. `0.3.0`
- `role` — `flipper`, `host`, or (later) `phone`

A receiver that sees a `protocol` it does not implement keeps the link open, refuses the
message types it does not know, and reports the mismatch to the user. Unknown message types
are ignored, never fatal.

## Flipper to host

| Line | Meaning |
| --- | --- |
| `B OK` / `B UP` / `B DOWN` / `B BACK` / `B LEFT` / `B RIGHT` | short press |
| `B OK PRESS` / `B OK RELEASE` | OK down/up in push-to-talk mode (v3) |
| `B OK LONG` / `B BACK LONG` | long press (>= 500 ms) |
| `D DISCARD` | confirmed discard, independent of the button map (v4) |
| `D ACCEPT <index>` | select a RAM history preview; 0 is newest (v4) |
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
| `A ACCEPT UNAVAILABLE` | selected history entry cannot be accepted; no current suggestion to show (v4) |
| `CFG <KEY>=<VERB> ...` | button map pushed from Settings |
| `PTT <0\|1>` | push-to-talk mode announcement (v3) |
| `T <text>` | live dictation text (v2); bare `T` or `T ` clears an empty final |
| `G <text>` | the enhancement suggestion (v2) |
| `L <latency_ms> <rssi>` | link quality for the device header (v2) |
| `P` | heartbeat |
| `P <token>` | timed probe, echoed by the device (v2) |

`S` lines are sent only when the state changes. `T` is throttled to 4 Hz and coalesced so a
fast partial cannot flood a BLE link; `G` is sent once per suggestion.

## Push-to-talk

The host sends `PTT 0` or `PTT 1` alongside `CFG` during negotiation and again when
the mode changes. In mode 1, OK down sends `B OK PRESS` to start dictation and OK up
sends `B OK RELEASE` to stop it. The host handles these edges independently of the
button map. All other buttons retain their existing lines. In mode 0, OK sends the
original `B OK` short press and follows its configured mapping.

## Button map (`CFG`)

Alphabetical keys, verbs joined with single spaces:

```
CFG BACK=DISMISS DOWN=ACCEPT OK=DICTATE_TOGGLE OK_LONG=CANCEL UP=ENHANCE
```

Verbs: `DICTATE_TOGGLE`, `ENHANCE`, `ACCEPT`, `DISMISS`, `CANCEL`, `NONE`. The host is the
source of truth; the Flipper caches the last `CFG` line so the map survives a reconnect
while Murmur is not running. Outside listening, `BACK LONG` exits the app and is never
sent. During listening, `BACK LONG` opens a discard prompt; confirming sends `D DISCARD`
to cancel regardless of the mapped OK-long action. A real `B OK LONG` still follows its map.

## Liveness and latency

Both ends send a bare `P` every 5 s. A bare heartbeat is presence only and is **never**
answered with another `P` — replying would make both ends ping-pong forever.

A host that wants a round-trip time sends `P <digits>`. The device echoes that token once,
verbatim, and the host publishes the measured difference to the device as `L`. No traffic
of any kind for 15 s means the link is dead: the device shows `Murmur offline`, the host
closes the transport and retries with backoff (1 s, doubling to 5 s).

## Transports

- **USB** — the Flipper app claims `usb_cdc_single`; the host opens the CDC device.
- **BLE** — the Flipper app runs the BLE serial profile (`8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000`
  service, write `19ed82ae-ed21-4c9d-4145-228e62fe0000`, indicate
  `19ed82ae-ed21-4c9d-4145-228e61fe0000`); the host connects as a GATT central over `btleplug`. Bonding is the
  firmware's: the Flipper shows a PIN, the host enters it once and keeps the key.
- Both may run at once. A reply is queued for the transport on which its request arrived;
  unsolicited lines use the active transport.

## Phone network extension (host increment 1)

The opt-in phone listener uses WebSocket **over TLS 1.3** (`wss://`) on a local address.
Each WebSocket text frame contains one protocol line without a newline byte; its text plus
the implied newline must fit the same 220-byte limit. This extension keeps protocol 4.
The Flipper link and its messages are unchanged.

The host first sends `HELLO 4 <app-version> host`. The phone replies
`HELLO 4 <app-version> phone CAP mic`, then `AUTH <six-digit-code>` for pairing or
`AUTH <token>` on reconnect. Pairing returns `AUTH OK <token>`; reconnect returns
`AUTH OK`. A failed attempt returns `AUTH DENY <reason>`. Codes expire after five
minutes and can be used once. The host stores only a hash of each token.

After authentication, `B dictate_toggle`, `S enhance`, and `A accept|dismiss|cancel`
invoke the existing host companion verbs. Flipper-style `B <button>` messages also use
the configured button map. A successful action returns `ACK <verb> <result>`.
The host sends `STATE listening|transcribing|enhancing|suggestion|idle` when that phase
changes. A `STATE` frame never contains transcript or suggestion text.

Binary WebSocket frames are reserved for the remote microphone in increment 2. This
increment closes a session that sends one with an explicit `unsupported audio binary
frame` reason. It neither accepts nor streams audio.

## Change policy

Bump `PROTOCOL_VERSION` for any change that an older peer would misinterpret, including
changes to a button's press semantics. Adding an optional message type or a new verb
does not require a bump; removing or renaming does.
