# Idle drag foreground diagnostic after desktop attempt 43949887005

The Windows run used `c12bd001f2fc97079316f15f73e124c274be5683` and the
exact build from Actions run 43945536043. Its desktop report records nine
passes, one failure (O2), 196 healthy guard probes, five owned Notepad tabs
closed, and successful restoration. O4 and F11 passed. O4 checks the position
after release, which is a weaker drag oracle than O2's held-position and
restart assertions.

O2's pointer started at `(960, 984)` on the 52 × 40 icon, at `(26, 20)` in
both the overlay and hit child's native client coordinates. The physically
held mouse moved 300 × 200 pixels, while the overlay rectangle stayed
`(934, 964, 986, 1004)`. The hit child rooted at the overlay. The historical
stage-6 DOM sample also reported a 52 × 40 icon and centered pill hit, but
there was no stage-1 mouse-down, native drag entry, or new drag log during
the gesture. The same-process subclass trace cannot observe the separate
WebView2 renderer boundary. This evidence does not establish a native
ignore-cursor defect or a no-activate conflict. Settings lifecycle remains a
moderate hypothesis; cold readiness remains unresolved.

The opt-in `O8` desktop-harness diagnostic compares one cold gesture with
three gestures after foregrounding, in order, one run-owned Notepad window,
run-owned Murmur Settings, and the same Notepad window. It creates Notepad
only after the cold gesture. All four use O2's icon affordance, 300 × 200
travel, 25 held steps, and 100-pixel held-movement threshold. Direction
prefers alternating signs, but each actual start is checked against its monitor's
verified work area. A different sign is chosen if needed to fit the entire
300 × 200 trajectory and translated overlay; if none fits, the diagnostic
stops without injecting the gesture. Travel is never clipped or shortened.
The work area, preferred direction and actual endpoint are recorded. Murmur's main PID and icon config
stay fixed across the four gestures. The harness records foreground root/PID,
Settings visibility and the launch hide-wait outcome, native overlay and hit
styles and enabled state, UIA/native geometry, held samples and per-gesture
log deltas. A guard trip, ownership loss or unsafe foreground stops the
comparison. A movement failure remains latched even if later cases move.
Only one owned Notepad fixture is made, and the normal final tab cleanup
retains responsibility for closing it.

Run `--only O8 --repeat 1` on a **new exact-SHA build** of this harness commit.
The previous `c12bd001…` build cannot execute O8 under the strict same-SHA
handoff. Four passing observations are inconclusive and do not clear O2.
The final restart checks the last successful position within 2 px only when
later failed trials have not changed that position. O8 does not replace O2's
ordinary gate or authorize a product repair. The full 48-check run, manual
DPI, Narrator and visual checks, signature and GPU gates remain closed.
The release-signing environment still requires owner approval. The user has
authorized an unsigned Authenticode installer; no primary key retrieval is permitted.

Review corrections add guard/ownership checks after Notepad creation and UIA
discovery and before each activation/click, including activation's fallback
path. Settings' existing HWND and original live PID are verified before the
singleton handoff can activate it, and again afterward. Native observation
APIs now declare pointer-width arguments centrally. Launch-specific hide
evidence is reset on every launch. Regression coverage includes cold/partial
movement, refusal before out-of-bounds input, guard trips during creation,
discovery and activation, Settings ownership loss before handoff, 64-bit
handle conversion and sequential launches. All 227 harness tests pass locally;
these checks are not native Windows validation.
