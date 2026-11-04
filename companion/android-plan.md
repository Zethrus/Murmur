# Murmur companion for Android — plan

Status: **not started. Planning only.** Phase 3. Nothing in Phase 1 or 2 may depend on it,
and no Android code is written until this document is agreed.

## Goal

The same three actions the Flipper companion offers, from a phone: start and stop
dictation, trigger an enhance, accept or dismiss the suggestion. Configuration lives in
Murmur Settings, in the same `[companion]` section the Flipper uses.

## Constraint that shapes the design

The wire protocol (`companion/protocol.md`, version 1) is transport-agnostic on purpose.
The phone is a second link implementation, not a second feature. If Android needs a new
message, that is a protocol change and the Flipper gets it too.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| **A. LAN WebSocket to Murmur** | full state feedback, same protocol, works on any WiFi network | new listening socket in Murmur, discovery and pairing to design, must be opt-in and authenticated |
| B. Phone as BLE HID keyboard | no Murmur changes at all, works with any app | one-way, no state, cannot accept a suggestion, conflicts with normal keyboard use |
| C. Phone relays to the Flipper over BLE | no host changes | phone must be paired to the Flipper *and* reachable by Murmur; two hops, two pairing UX, most moving parts |

**Recommendation: A**, with B kept as a "no setup" fallback for people who only want a
dictate button.

## Sketch (option A)

- Murmur gains a `[companion.network]` block: `enabled`, `bind` (loopback by default),
  `port`, `mdns` on/off. Off unless the user turns it on. Never bound to `0.0.0.0`
  without an explicit warning and a token.
- Discovery: mDNS service `_murmur._tcp`, TXT record carries protocol version and app
  version. Manual host:port entry as the escape hatch.
- Pairing: Murmur shows a 6-digit code and a QR; the phone sends `HELLO 1 <ver> phone`
  followed by `AUTH <code>`, the host answers `AUTH OK <token>`; the token is stored and
  reused. Tokens are revocable per device in Settings.
- Same `B`/`S`/`A`/`CFG` messages, same verbs. Phone UI is a large three-button surface
  plus a live state indicator, which is all the protocol can express.
- Android implementation: Kotlin, Compose, foreground service so the buttons work while
  the phone is in a pocket, no persistent notification spam (one low-priority channel).

## Security requirements before any of this ships

1. Loopback bind by default; a non-loopback bind requires a token to exist.
2. The token is compared in constant time and rate-limited (5 attempts, then a lockout).
3. No transcript text ever crosses the socket — the protocol invariant already enforces
   this, and the host-side test suite must prove it for the network transport too.
4. TLS is optional on a trusted LAN in phase 3. If it is added, it is added for both
   transports at once, not only for the phone.

## Milestones

- **A1** `[companion.network]` config + listener + auth handshake, no UI. Tested with a
  scripted client.
- **A2** Settings card: enable, bind address, pairing code, paired devices, revoke.
- **A3** Android app: connect, pair, three actions, state display.
- **A4** mDNS discovery and QR pairing.
- **A5** Release wiring: the APK is built and attached by CI the same way the `.fap` is.

## Open questions for the user, when we get there

1. Is phone-over-WiFi actually wanted, or is "phone as a keyboard" enough? Option A is
   roughly a week of work; option B is a day and no Murmur changes.
2. Should the phone be able to see the *text* of a suggestion before accepting? That
   would break the protocol invariant and needs a deliberate decision, not a default.
3. Distribution: sideloaded APK from GitHub releases, or Play Store? Play Store brings
   signing, review and a permanent key that must not live in this repo.
