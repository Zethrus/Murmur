# Windows overlay repair and Murmur 0.5.0 release plan

Planning baseline: PR26, `f2534712fc3180be0bd08edd234e3d2e5ba77588`.
This is a proposed implementation and validation plan, not release approval.
No application changes, dependency updates, native tests, or workflow dispatches
were made to produce it. Work continues without OMX workflows.

## Recommendation

Fix input delivery before activation, rather than restoring focus after the
overlay has activated. First establish the actual WebView2 host topology and
runtime. Separately evaluate the released Wry 0.57.2 focus correction, which is
absent from our locked 0.57.1. Select one repair path using that evidence, prove
it in the bounded foreground comparison, then progress to ordinary acceptance
checks and the complete release gates.

Do not treat either an upstream issue or a dependency upgrade as proof that
Murmur's particular failure is solved. In particular, our failed gestures never
reach `start_overlay_drag`, whereas the Wry fix concerns entry into native
moving/sizing. These may be two different focus problems.

## 1. Evidence and diagnosis

### Direct native evidence

The [exact-head O8 run](https://github.com/Zethrus/Murmur/actions/runs/43996483597)
completed all four gestures. Its artifact contains valid, armed, uncapped,
paired activation traces with no parser errors:

| Context | Movement while held | Observed native path |
| --- | --- | --- |
| Cold launch, Settings hidden but still foreground | Failed | Overlay click activation; synchronous hand-back to Settings; no DOM mouse-down or native drag entry |
| Owned Notepad foreground | Passed | Activation peer is null; default forwarding; local Murmur focus enters WebView2 while external foreground remains Notepad |
| Visible Settings foreground | Failed | Same hand-back branch and absent drag entry as the cold case |
| Same Notepad foreground again | Passed | Same successful path; full requested travel |

The guard observed 26 healthy probes, cleanup completed, and restart restored
the successful position with zero-pixel origin error. Correct in-bounds UIA
StatusBar selection was established before these gestures; the earlier
same-named ToolTip collision is not an explanation for these failures.

The report identifies Murmur PID 12792 and a hit window of class
`Chrome_RenderWidgetHostHWND` in PID 13896. It does **not** identify the hit
window's immediate parent. Trace parent IDs represent callback nesting, not
the native window hierarchy. Focus peers also do not prove parentage.

The older [full-suite run](https://github.com/Zethrus/Murmur/actions/runs/43906690485)
recorded WebView2 `157.0.4341.46` through the registry and three 1920×1080 displays
at 100%. That is historical environment evidence, not proof of the runtime
loaded by the latest process. Its results were 44 pass, 3 fail, 1 skip, with
additional final-cleanup failures. Later focused passes do not replace a final
full-suite result.

### Source-supported interpretation

High confidence: the failure is associated with activation when Settings and
the overlay share Murmur's UI thread. The latest trace proves the hand-back
executes, and the existing `bfe131f` return-zero candidate did not cure it.

Moderate confidence: preventing the initial activation while allowing the
original mouse message is a better repair direction than another hand-back
variant. Causation inside the renderer remains unproved. Windows distinguishes
`MA_NOACTIVATE` from responses that discard the mouse message, and default
active `WM_ACTIVATE` processing assigns keyboard focus. See Microsoft's
[mouse-activation contract](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-mouseactivate)
and [activation contract](https://learn.microsoft.com/en-us/windows/win32/inputdev/wm-activate).

Lower confidence: renderer readiness or a changing cursor-handling style could
contribute. Preserve those observations, but do not change delays, geometry,
movement thresholds, or the native movement loop without new evidence.

### New upstream findings

1. **Released Wry correction:** [PR1910](https://github.com/tauri-apps/wry/pull/1910)
   removes `WM_ENTERSIZEMOVE` from the branch that calls WebView2 `MoveFocus`.
   It shipped in [Wry 0.57.2](https://github.com/tauri-apps/wry/releases/tag/wry-v0.57.2).
   Murmur locks Wry 0.57.1, whose handler still includes it. This is a concrete
   drag-focus risk, but not a demonstrated explanation for our pre-drag failure.
   The 0.57.1→0.57.2 tag diff also changes GTK code, Windows drag/drop and child
   teardown; the update is not a Windows-only one-line change.
2. **Closely related upstream report:** discussion on
   [Tauri10767](https://github.com/tauri-apps/tauri/issues/10767) describes the
   same-process foreground distinction and an experimental combination of host
   input routing and early activation prevention. This is another application's
   report, not Murmur evidence or a recommendation to copy its hooks.
3. **Supported input-routing API:** Microsoft documents
   [`ICoreWebView2ControllerOptions4::AllowHostInputProcessing`](https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2controlleroptions4).
   It is a creation option for windowed controllers and defaults to false.
   Our pinned Wry creates controller options but does not expose this setting.
   [Wry1850](https://github.com/tauri-apps/wry/pull/1850) was open when checked.
4. **Material regression risk:** [WebView2Feedback5701](https://github.com/MicrosoftEdge/WebView2Feedback/issues/5701)
   reports lost release/capture behavior when host input processing is enabled.
   This requires dedicated acceptance tests before considering that route.

## 2. Preserve the contract throughout

- Retain the 60-second idle preflight, session/desktop checks, real-input
  abort, 0.5-second guard deadline, permanent failure latch, and verified
  run-owned cleanup. A failed run is not automatically restarted.
- No focus repair with synthetic Alt/Esc, `AttachThreadInput`, foreign-window
  subclassing/style mutation, or keyboard replay. No unknown application closes.
- Preserve the actual keyboard target, including its focused editor/control and
  selection, rather than checking only the final foreground HWND.
- Keep the same full, work-area-bounded 300×200 gesture, held-movement threshold,
  exact text/history checks and ≤2 px restart-origin tolerance. Retain failed
  observations even if a later case passes.
- Keep Windows-specific behavior isolated. Linux focus, movement, release-gated
  delivery, and visuals need regression evidence after any dependency update.
- Coordinate Edison availability before native work. It is currently free for
  normal use. Every testing session ends with an explicit stopped notification.

## 3. Implementation batches and decision gates

### Batch A — capture the controlling boundary once

**Scope:** a bounded, QA-only topology/runtime observation, plus test-oracle
corrections described below. Do not combine it with a production activation
change.

Use existing `WebviewWindow::with_webview` access on the UI thread to read the
overlay controller's `ParentWindow` and environment's `BrowserVersionString`.
Pinned Tauri 2.12.5 exposes both controller and environment. Microsoft's
[controller](https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2controller)
and [environment](https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2environment)
contracts distinguish the supplied host window from the actual loaded runtime.
Do not infer either from an installed-runtime registry entry alone.

Capture, without activating or modifying any window:

- Overlay root, controller host, hit renderer, Settings root and focused child.
- Immediate parent chain to the overlay root: HWND, parent HWND, owner HWND
  separately, PID, TID, class, styles, visibility, enabled state and bounds.
- Murmur process creation identity, active/focus/foreground HWNDs, desktop,
  app SHA/artifact hashes, actual WebView2 version and Wry/Tao/Tauri lock versions.
- Snapshot completeness and errors, not just successful fields. Suggested limits:
  16 ancestors and 128 descendants; cycles, overflow, inaccessible properties,
  destroyed/reused HWNDs or changed owners make that observation inconclusive.

Read native fields before and after enumeration and reject changing identity.
Use pointer-width declarations. Do not synchronously query arbitrary window
text or run unbounded UIA enumeration. Foreign HWND metadata may be read, never
modified. Emit only handles/identifiers/classes/geometry/version information,
not user titles or text. If a queued UI-thread operation times out, invalidate
its generation so it cannot become evidence for a later gesture.

The first diagnostic should use the existing artifact if a standalone read-only
inspection can safely attach to a verified run-owned process. If app-side
controller access or a fresh launch is required, use one reviewed instrumentation
commit and the normal exact-SHA build/handoff. Never weaken artifact provenance
or represent an old binary as a new-head validation.

**Exit:** identify the renderer's immediate parent and the controller host, with
ownership and runtime attached. No additional drag is necessary just to collect
topology. Missing or foreign ownership is a decision result, not a reason to
broaden a style patch.

**Tests before dispatch:** null/cyclic/changing chains; PID/TID mismatch;
destroyed/reused handles; pointer-width values; unavailable controller/runtime;
overflow; late UI-thread replies; disabled-QA path; and zero activation/input or
style writes from collection.

### Batch B — separately evaluate the released Wry correction

Prepare a small dependency-only candidate using `cargo update -p wry --precise
0.57.2` in `src-tauri`, if the pinned dependency constraints permit it. Review
the resulting lock diff and upstream changes; do not run a broad dependency
update. If resolution pulls unrelated major changes, stop and choose a reviewed
backport strategy rather than silently upgrading the stack.

Keep this commit independent from topology instrumentation and any new
activation policy. Do not edit the Cargo registry cache. Prefer a published,
compatible upstream release to a permanent private fork.

Verify resolved source no longer calls `MoveFocus` for `WM_ENTERSIZEMOVE` while
retaining normal `WM_SETFOCUS` handling and move notifications. Native tests must
cover Settings dropdown placement, keyboard/IME behavior and Linux interaction
as well as dragging. Source inspection and compilation cannot prove focus safety.

**Exit:** either a compatible, reviewed dependency candidate with regression
checks, or a documented reason to defer it. Even if O8 remains unchanged, this
does not falsify the pre-activation hypothesis: it addresses a later movement
stage. Do not advertise it as the complete O2 repair.

### Batch C — choose exactly one activation repair path

| Observation | Implementation direction | Stop condition |
| --- | --- | --- |
| The renderer's controlling immediate parent is verified Murmur-owned on the UI thread, and the runtime/source evidence supports the style decision there | Trial the minimum nonactivating style/handler at that exact host only, keeping the existing fallback | Reject ambiguous/foreign/thread-mismatched or changed identity; stop if the controlling boundary cannot be established |
| The controlling parent is foreign-owned, or no safe owned style boundary exists | Evaluate supported host input processing in a minimal, isolated Windows reproducer | No foreign mutation; no product patch until routing, mouse release and focus tests pass |
| Neither route has defensible evidence | Produce a minimal upstream reproduction and a separate hosting-design decision | Stop production experiments; no more activation-return variants or repeated full-build loops |

**Owned-host implementation requirements:** select from the verified controller
and topology relationship, not class name alone; preserve all unrelated style
bits; apply/read back on the owning UI thread; handle asynchronous child creation
and destruction; be idempotent across shows; never redirect input to Settings or
another app. Keep busy click-through behavior intact. Record failure explicitly
and do not silently claim the policy was installed. Remove only state owned by
this implementation during teardown, after revalidating identity.

The upstream Chromium parent-style check is a lead, not a stable WebView2 API
guarantee. The controller's host need not be the renderer's immediate parent.
Do not apply `WS_EX_NOACTIVATE` to every descendant or assume that its documented
top-level semantics automatically solve child-window behavior.

**Host-input fallback requirements:** isolate it to the overlay controller at
creation; Settings remains on its existing input policy. `with_webview` after
creation is suitable for observation, not for retroactively changing creation
options. The integration may require a narrowly pinned Wry/Tauri-runtime change;
audit that seam before modifying dependencies. Unsupported runtime/interface
must be an explicit result, not silently treated as enabled.

Use a small two-window reproducer with the same windowed WebView2 hosting,
thread relationship, `focusable:false`, HTML drag target and native dragging.
Exclude engine/model/audio/component packaging. Run fixed baseline and candidate
configurations in the same coordinated session; no arbitrary parameter search.
The reproducer diagnoses behavior and cannot clear Murmur's acceptance checks.

Only if the host-input experiment proves a later programmatic activation remains,
consider a separately reviewed **thread-local**, exact-overlay early activation
veto. The upstream combination is experimental: do not add a CBT hook by default.
Any such design must pass all unrelated/negative hook codes onward, use no global
hook or injection, clean up reliably, preserve keyboard and mouse delivery, and
leave the desktop safety observer untouched. A need to relax those constraints
rejects the candidate.

Do not forward activation and restore focus afterward as a shortcut. Do not hide
Settings, focus Notepad, or prewarm the overlay to make cold O2 pass.

## 4. Repair the evidence gaps before judging a candidate

The current O8 records keyboard focus during held samples but primarily asserts
foreground ownership. Add a distinct keyboard-destination assertion: changing
the focused child with the same root must fail. Add synthetic typing **after**
release into a preselected Settings text field and into the external editor,
without clicking/refocusing either target. Compare the expected field/editor
contents exactly. Use a disposable Settings profile; restore any modified
preference. Captured samples alone cannot rule out a transient change between
samples; use the bounded owned-window trace and post-gesture behavior together,
and label trace gaps as inconclusive.

Keep UIA reads out of active transcript injection. Settings field discovery is
completed before a gesture; observation during it uses native state. Preserve
history-before-Notepad-readback ordering that fixed earlier harness interference.

Ordinary O2 still uses `overlay_drag_point()`, which selects the first same-named
UIA element. O8 has the stronger selector because a native ToolTip collision was
observed. Audit O2 before its acceptance run. If it shares that defect, reuse
verified ownership/type/containment/uniqueness selection without weakening O2's
movement or persistence oracle. Keep that harness correction in a separate
commit; a corrected click target is not a product repair.

Also close the existing evidence-quality gaps together: retain every bounded
candidate observation rather than overwriting earlier polls; report alternate
names on the owned subtree without using them as a permissive click fallback;
use fresh per-gesture evidence where freshness is claimed. Mount-time stage 6
does not prove current viewport convergence. Test partial-drag exceptions and
last-moment hit-root rejection, including key/button cleanup.

## 5. Validation ladder

Advance only after reading the actual evidence. Keep one active heavy Edison
job and one desktop run at a time. Stop on guard trips, ownership loss, invalid
evidence or cleanup failure; do not turn an abort into a failed-check retry loop.

| Gate | Work | Required result |
| --- | --- | --- |
| Local regression | Focus/dispatch/ownership/lifecycle tests; O8 parser and geometry; UI drag/button tests; changed Rust feature sets; Windows target compile; frontend build; diff check | New regressions fail before and pass after; unchanged dispatch, Linux and privacy contracts |
| Exact build | Manual Windows build and validated desktop handoff for the proposed commit | Binary, installer, harness and artifact provenance match; tests and packaging pass |
| Four-case experiment | O8 alone, repeat 1; same process and fixed icon config | All four gestures move while held, preserve the intended keyboard target, show DOM/native entry, and persist within 2 px; no guard/cleanup failures |
| Product acceptance | Cold ordinary O2; O3/O4/O7 and F11; Settings field and external editor typing | Ordinary O2 passes without a warm-up; actions and drag preserve focus; delivery waits for release |
| Repeatability | Two independent cold launches, then a focused sequence with repeated idle/recording drags and hide/show/restart | No intermittent first-click loss, stuck mouse state or cached evidence; per-attempt artifacts retained |
| Delivery regression | H1/H8/I1/I4/I5/F13 and F1–F8/F12; include actual Enhance Accept/Discard coverage | Exact same-utterance output, clipboard restoration, safe held-text handling, no retrigger or transcript logging |
| Full automation | All 48 catalog acceptance IDs including H8, excluding diagnostic-only O8 | No failed/aborted items; explain any skipped item and complete its acceptance separately; all owned cleanup succeeds |
| Cross-platform/manual | Current-head Linux interaction; matched screenshots; Windows manual checklist | Evidence tied to actual candidate, not old Linux baseline or prior-head focused results |

O8 intentionally reports diagnostic status even when all observations succeed.
Do not turn it into a substitute for O2 or change its outcome convention just to
make CI green; assess its four observations, then run ordinary acceptance.

Add explicit loss-of-release coverage if host input processing is trialled:
release inside/outside, hold without movement, rapid click-drag-release, leaving
the work area, repeated drags, busy delivery, and hide/destroy during a gesture.
Retain native button-state completion and timeout cleanup. A movement pass with
stolen focus or a latched button is a failed candidate.

The full selection is:

```text
P1,P2,P3,P4,S1,S3,S4,O1,O2,O3,O4,O6,O7,H1,H2,H3,H4,H5,H6,H7,H8,I1,I2,I3,I4,I5,I6,F1,F2,F3,F4,F5,F6,F7,F8,F11,F12,F13,T1,T2,T3,A1,A2,M1,M3,K1,C1,C3
```

If a product fix changes after any native gate, invalidate the relevant result.
A different merge/release build needs its own recorded identity and validation;
do not silently transfer exact-SHA claims.

## 6. Remaining release gates beyond dragging

1. Reconcile [test-ledger.md](test-ledger.md), [matrix.md](matrix.md), the focus
   contract and checklist with actual runs. Some text still says no native work
   has occurred or assumes no owned WebView children. Correct those statements
   precisely; historical failures remain visible. Visual parity is not established
   by shared frontend code or unit tests.
2. Complete S2 high DPI, O5 mixed DPI, F9 secure desktop, F10 elevated target,
   A1 sign-in, M2 device privacy, M4 unplugged microphone, C2 antivirus/reinstall,
   K2 Narrator, K3 reduced motion, H6's manual capture-field portion, and matched
   Linux/Windows visuals and behavior. Coordinate disruptive/hardware actions
   explicitly. Do not automate security prompts around their safeguards.
3. Restore final-head Windows and Linux PR checks sequentially; review the final
   diff and manual Windows handoff. Linux attempt 2 for `f253471` was verified
   successful during planning; that does not validate a future dependency change.
4. After parity and pre-release gates, merge the authorized PR. Verify the final
   commit/tree and build identity. Do not tag merely because compilation passes.
5. Before triggering release, verify `ENABLE_WINDOWS`, runner/GPU/model readiness,
   Windows GPU offering policy, and owner access to the protected signing
   environment without retrieving secrets. The workflow conditionally skips
   Windows GPU validation when its model variable is absent; this task requires
   actual GPU evidence, so a skipped workflow job cannot satisfy that gate.
6. Build Linux and Windows draft assets from the final release identity. The
   Linux signing job requires owner environment approval and the configured
   signing subkey; never retrieve the primary key or bypass the approval.
   Validate the combined manifest, component hashes, OpenPGP signature, Linux
   and Windows GPU evidence against final assets, and required Flipper artifact.
7. Install/smoke-test the actual draft installer/portable Linux and Windows
   assets, verify checksums, and finish the release checklist. Authenticode may
   remain unsigned as authorized; document that accurately. This is separate
   from mandatory component-manifest signing.
8. Publish only the validated draft, then download the public assets and verify
   them. No build artifact from an intermediate diagnostic commit is a release.

## 7. Scope, rollback and deliverables

Prefer reviewable commits for (A) evidence/oracle corrections, (B) the compatible
dependency update, (C) the one selected native repair, and (D) regression evidence
and release documentation. Use explicit revert commits if a candidate fails;
do not hard-reset the shared branch or discard unrelated work. Keep the current
hand-back fallback until a replacement passes focus acceptance; separately decide
whether the unsuccessful `bfe131f` change remains justified as defensive behavior.

No broad renderer-style propagation, unpinned dependency update, global hook,
lowered movement threshold, forced focus, hidden failure, or indefinite diagnostic
loop belongs in this plan. If a correctly observed candidate fails, classify it
by the changed message stage and either reject it or authorize one specifically
motivated next branch. A new round must answer a new question.

Concrete deliverables are: topology/runtime record; dependency-diff assessment;
reviewed candidate or explicit rejection; exact-head focused and full reports;
manual/visual evidence ledger; final asset/signature/GPU verification; and a
downloadable, accurately labelled release. There is no defensible fixed release
ETA until the first candidate preserves both mouse delivery and keyboard focus.

## 8. Implementation status (local, no native evidence yet)

Committed locally on `fix/windows-desktop-startup`, not pushed, no Edison run:

| Commit subject | Batch | Change |
| --- | --- | --- |
| select O2/O4 icon drag target by role and bounds | A (harness) | O2/O4 icon gestures use O8's role/ownership/containment/uniqueness selector; rejected candidates are recorded. F11's pill-view handle lookup is unchanged. |
| assert O8 keyboard target and record renderer topology | A (oracle) | O8 latches same-root keyboard-focus changes, types a unique token after release into the preselected Notepad editor or Settings history-search field without refocusing, and requires exact text. A bounded, twice-read, read-only topology snapshot classifies the renderer's immediate parent as owned/foreign per gesture; O7 writes `webview-topology.json` in full runs. |
| log overlay WebView2 host and runtime in QA builds | A (QA app) | QA builds log one text-free `QA overlay webview host` record: controller `ParentWindow`, its owner PID/TID and parent, and the loaded `BrowserVersionString`. |
| update wry 0.57.1 -> 0.57.2 (last commit) | B | Wry 0.57.1 → 0.57.2 only; resolver-only `windows-sys` edge churn excluded, lock accepted by `--locked`. |

Source reading during implementation: Wry hosts the controller in an in-process
`WRY_WEBVIEW` child (`WS_CHILD | WS_CLIPCHILDREN`, no `WS_EX_NOACTIVATE`) of the
overlay. The run 43996483597 trace shows its `WM_SETFOCUS` (hwnd `0x350abe`) only in
the Notepad cases. Whether the renderer's *immediate* parent is that owned host or a
foreign `Chrome_WidgetWin_*` window is exactly what the new snapshot answers; Batch C
must not be chosen before it does.

An independent review then tightened the O8 input path (separate commit): the
Settings field is clicked at its validated centre only when `WindowFromPoint` resolves
to the verified Settings root; Settings typing also re-proves UIA focus on that exact
field, because its native focus window is shared by the whole page; a null focus never
authorizes typing; unreadable or stale app host records are not classified; and O7's
topology evidence cannot replace O7's own result. The Settings case now starts from a
real click into that field, so its results are not directly comparable with earlier
Settings observations.

Local gates passed: 470 script tests; 305 Rust tests on Linux with Wry 0.57.2; Windows
(`x86_64-pc-windows-gnu`, no default features) check and clippy, with no new warnings
in changed files. Frontend unchanged.

Next native order, one Edison job at a time: (1) exact build of the commit just before
the Wry update (instrumentation and hardening, Wry 0.57.1), then O7 and
`--only O8 --repeat 1` for topology plus the stronger oracle; (2) exact build of the
Wry commit and the same O8 run to isolate the Wry change; (3) read both, then choose one
Batch C row. Ordinary O2, the 48-check suite and Linux regression follow.

## 9. Outcome, 2026-11-17

Batch C selected the host-input route. Evidence chain:
[topology](desktop-44030456571-o8-topology.md) → [O9 reproducer](desktop-44041874181-o9-host-input.md)
→ [O8 after the repair](desktop-44053826670-o8-fixed.md). The product change has three
parts:

- **Overlay host input:** vendored Wry 0.57.2 with an overlay-only
  `AllowHostInputProcessing` patch (`src-tauri/vendor/README.md`).
- **Activation veto:** a UI-thread `WH_CBT` veto of the overlay's programmatic activation.
- **Settings rebuild:** Settings defers external-config rebuilds while a text field is
  focused.

Harness corrections along the way: O9 now uses the startup lease and the build's root
path. Final cleanup gains a hit-tested editor click, and S4 fits Settings into the work
area before its drag.

**Results on `faf54085`:** 48/48 automated checks passed and O8 passed. Final-head Linux
and Windows PR checks, the manual checklist, and the release gates in section 6 remain.

