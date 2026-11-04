# Companion phase 2 — Bluetooth, live text, device feedback

Protocol **v2**. Ships as Murmur 0.3.0. Phase 1 (USB only) shipped in 0.2.3.

## Decisions taken

| Question | Decision |
| --- | --- |
| Transport priority | `auto` — USB when the cable is in, BLE when it is not |
| Pairing | Firmware-managed bonding; the Flipper shows the PIN, the host enters it once and remembers it |
| Live text on the device | Full streaming text; never logged, never stored, never written to disk on either end |
| Host BLE stack | `btleplug` (Linux + Windows) |
| Extras | Vibrate on every state change, suggestion on screen with Down to accept, minimal header with link, battery and latency |

## Wire protocol v2

Two new host→device lines and one device→host line. Everything else is unchanged.

```
T <text>        live dictation text, one transmission per update
G <text>        the enhancement suggestion once it is ready
Q <battery>     device battery percentage, every 30 s
```

Rules:

- `T` and `G` carry UTF-8, no control characters, hard-truncated at the line limit. The
  device treats each `T` as a **replacement** of the live buffer, not an append, so a
  dropped line self-heals on the next update.
- Text is never persisted: the host does not log it, the device holds it in RAM only and
  drops it on dismiss or timeout.
- A device that receives `T` without negotiating v2 ignores it.

`MAX_LINE` rises from 160 to 220 bytes to fit a wrapped text chunk plus the tag.

## Device side (Flipper)

- `ble.c/h` — start `ble_profile_serial`, register the serial profile callbacks, accept
  RX into the same ring as USB, advertise while unpaired. Bonding is the firmware's; the
  app only reports link state.
- `link.c` — two receive rings (USB, BLE) with one shared line parser each, so a line is
  always answered on the transport it arrived on. `auto` semantics fall out of this: the
  host that says `HELLO` first owns the link, and USB wins if both are talking.
- `murmur_companion.c` — new screen layout:
  - header: `MURMUR`, transport glyph (`U`/`B`), battery `%`, link latency in ms
  - live text: wrapped, last four lines, auto-scrolled while listening
  - suggestion screen: appears on `G`, scrolls with Up/Left, `DOWN` accepts, `BACK` dismisses
  - vibrate on phase transitions, not on every tick
- Buttons, map cache and Back-long-to-exit are unchanged.

## Host side (Rust)

- `companion/link/ble.rs` — `btleplug` central on its own current-thread tokio runtime,
  scan by the Nordic UART service UUID, connect, subscribe to TX notify, write RX. Same
  `Link` trait, so the supervisor does not branch on transport.
- `link::open` — `auto` tries USB then BLE; `ble` goes straight to BLE and reports a
  pairing hint when the device is advertising but refuses the connection.
- `companion/mod.rs` — sends `T` from the streaming transcript hook and `G` from the
  enhance result hook, throttled to 4 Hz and coalesced, so a fast partial cannot flood a
  20-byte-per-second link. Adds `rssi`, `battery` and `latency_ms` to `Status`.
- Latency is measured by timing the existing `P` heartbeat round trip, so no new message
  and no extra traffic.
- Privacy: transcript text goes through `Companion::publish_text`, which never touches
  `tracing`. A unit test asserts the log macro is not reachable from that path.

## Settings UI

Page 06 gains: transport `auto | USB | Bluetooth`, a BLE device row (name, address, bond
state), and a read-only quality line (RSSI, battery, latency). The button map is unchanged.

## Tests

- Protocol v2 encode/decode, including a text line at exactly the limit and one over it.
- `auto` resolution: USB present wins, USB absent falls through to BLE, both absent errors.
- Throttle: 20 transcript updates in 100 ms produce at most one `T`.
- Text is not logged (asserts on the tracing layer).
- Device: `ufbt` build against API 87.1, then a hardware pass with the cable out.

## Risks

- BLE throughput is ~1–2 KB/s after overhead; the 4 Hz coalesced update is well inside it.
- Pairing UX is the failure mode users will hit first: the page must say *"pairing"* and
  *"bond missing"* distinctly instead of showing a generic offline state.
- A bonded host that reconnects while the FAP is not running will retry until the cable or
  the app appears; the supervisor's backoff already covers this.
