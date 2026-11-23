# Overlay input hardening, November 23, 2026

Approved follow-up from 0.5.1 / `ffa6222f78d7a5220c56db921f7b1de9a0893cf5`.
PR26 is merged. This work does not reopen its earlier diagnostic experiments.

## Baseline and audit

- Wry 0.57.2 already includes the released focus correction. The two-file vendor
  patch adds overlay-only `AllowHostInputProcessing` before controller creation.
  Settings is not opted in. Unsupported/unknown outcomes are logged explicitly;
  they do not count as a successful capability check.
- The existing WH_CBT hook is installed for the UI thread, vetoes only the exact
  overlay root, and chains other codes/targets. Root destruction clears the target
  and attempts unhooking; repeated installs reuse the hook. Installation occurs
  through the existing main-thread overlay visibility path. This follow-up changes
  no hook, activation fallback, native drag, foreign-window style, or input guard.
- The outcome registry stores controller-creation observations. There is no new
  recreation path in this change; lifecycle claims beyond the recorded hardware
  runs require a fresh native check. No speculative registry/hook redesign is made.
- The vendor content verifier reconstructed all 83 files from the pinned official
  archive plus recorded patch, with no differences. Both platform CI workflows now
  execute it. The verifier is read-only with respect to the checkout and supports
  an offline archive with the same mandatory SHA-256.
- Upstream Wry #1850 remained open at review. Remove vendoring only after a released
  compatible API is available through Tauri, with separate migration validation.

## Demonstrated shared Settings defects

1. A pending local save completed after focus moved to a different text field and
   rebuilt that field, bypassing the external-config focus protection.
2. A queued focus-out refresh could rebuild while an IME composition had not ended.

Both new regressions failed before the fix and pass afterward. Save completion now
uses the same editing-aware refresh path as external config. Composition retains
the current element until completion; deferred refreshes are coalesced and cancelled
on disposal. Explicit navigation still renders its destination and clears old state.
Tests cover field identity, draft value/selection, newest pending configuration,
composition ordering, navigation, and disposal. The same frontend runs on both OSes;
no style, layout, token, feature, or platform-specific UI fork was introduced.

## Local evidence

- 75 UI tests passed, including the editing regressions and the review follow-up below.
- 500 script tests passed, including nine vendor pin/integrity tests.
- `npm run build`: TypeScript, token lint, protocol check and Vite build passed.
- Actual upstream archive verification: 83 files matched.
- Full Rust/MSVC and packaging validation are assigned to exact-head CI. Local
  Rust is 1.92.0; CI pins 1.100.0. No Rust behavior changed in this follow-up.

These are local test results, not native Windows, visual, or release acceptance.

## PR review and first CI follow-up

The review reproduced a stale-closure regression: retained Companion handlers sent
the entire old network/button table, so changing Port could revert an earlier Bind
address edit. Companion saves now send only the changed nested property, matching
the backend's recursive table-merge contract. Page contexts expose the latest config
to delayed actions; the excluded-app picker and live network address use that value.
Five UI regressions cover sequential/queued edits, unrelated external updates,
rejected saves and exclusion-list preservation. Four failed before the correction;
all five and the full 75-test UI suite passed afterward. The UI harness now mirrors
the backend's nested-table merge (arrays and replacement dictionaries still replace).

Linux CI 45170789317 reached Rust tests and failed the certificate rollback fixture:
its new subkey was created relative to today's date, after the fixed November 1
manifest issue time. Only that test's key timeline is now anchored to its existing
`NOW` constant. The legitimate-C2 acceptance, malicious-C1 rejection, cached-state
preservation and offline revocation assertions remain intact. Production trust code
is unchanged. The corrected Rust test still requires CI execution.

Windows CI 45172206326 stopped when applying the recorded Wry patch in the verifier.
A local Git reproduction showed that a reused CRLF checkout retains its old bytes
when new attributes arrive. Windows CI now restores only the tracked vendor inputs
from its checked-out commit before running verification. A regression covers that
transition; checksum, patch application and byte comparison remain strict. Native
confirmation of the Windows checkout correction remains pending.

## Remaining acceptance sequence

| Gate | Required evidence | State |
| --- | --- | --- |
| Windows/Linux CI | Both checks on final head; vendor verification and existing Rust/build suites | Pending |
| Edison build handoff | Manual Windows build on exact head, verified asset hashes | Pending |
| Focused Windows | O8 four contexts with exact Settings/Notepad typing, ordinary O2/O3/O4/O7, F11 and delivery regressions | Pending availability/build |
| Release edges | Rapid press/release, release outside overlay, repeat drag, restart/recreation and no stuck movement; unchanged focus | Pending native coverage |
| Full desktop | All 48 checks, guard healthy, no hidden skips/aborts, verified owned-fixture cleanup | Pending |
| Shared Settings | Native editing/selection and composition while config changes on Windows and Linux | Pending |
| Visual/accessibility | Matched light/dark pages and overlay states; high/mixed DPI, Narrator, reduced motion | Pending owner/hardware |
| Other manual parity | Secure/elevated targets, AV, microphone privacy/unplug and checklist items not automated | Pending owner/hardware |
| Release candidate | Exact-build installer/portable and Linux packages, CPU/GPU, manifest/signature verification | Pending |

Before desktop automation, obtain current Edison availability (unlocked, Deskflow
exited, input devices disconnected). Build time does not reserve the desktop. Keep
physical Edison build workloads sequential. Preserve all guard deadlines and failure
latches; no blind retry loops, user-app closure, or changes to protected desktop state.
Notify the owner when input automation stops.

Review failures from their reports before editing. Roll back a new regression with
the relevant small follow-up commit, not by removing the already-proven input route.
Unsigned Windows Authenticode is authorized; it does not waive release signing or
owner approval for the protected signing environment. No new tag or release is made
by this follow-up until its required gates are satisfied.
