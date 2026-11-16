# Automatic Notepad fixture lifecycle (November 16, 2026)

## Native evidence before this extension

The focused H1/H8 retention change (`88db7e8` plus `3039ac1`) passed both the single run
`43889221636` (`/tmp/murmur-desktop-43889221636`) and repeat run `43891157358`
(`/tmp/murmur-desktop-43891157358`): four exact assertion results, 91 healthy guard
probes, and no abort or cleanup failure. The broader selection H1,H8,I1,I4,I5,F13
at `43893335319` (`/tmp/murmur-desktop-43893335319`) disabled that narrow retention.
H1 passed, then H8 and the later checks aborted at probe 17 after automatic H1
window close. Sixteen probes were healthy; the log recorded one `SendInput`, 118
keyboard callbacks without further callbacks at timeout, a received probe-17 heartbeat,
and no real input. This comparison
supports testing the Notepad lifecycle boundary. It does **not** establish the
Windows root cause or justify changing the guard or app.

An earlier H1-first attempt (`/tmp/murmur-desktop-43861801542-attempt2`) also
passed H1 before probe 17 failed during H8 setup, after H1's window close.
That trace likewise did not establish a cause.

## Bounded extension

Every automatic `Context.notepad()` fixture now stays in a run-owned registry,
separate from `Restorer`. Each check's `Restorer` mark still unwinds clipboard,
configuration, registry, mouse, and other state before the next check. This applies
with a broad selection and repeats; `TargetApp`, Terminal, and VS Code cleanup
remain per check. F3's explicit `TargetApp.close()` in its replacement scenario
remains immediate. No guard deadline, startup lease, or product behavior changed.

Each Notepad call gets a new work-directory/run, attempt, and call identity.
Creation refuses an existing fixture path rather than clearing its text. A new tab
may reuse only a live Notepad HWND and PID registered by this run, including any
earlier owned window when Windows uses more than one. Explicit H8 same-window reuse
is checked against that same registry. A newly created window must expose the
new document title and a Notepad PID. Failed creation fails the check and aborts
later checks; an unverified spawn has no HWND eligible for cleanup.

At run end, the existing owned-tab cleanup navigates to each exact fixture title,
saves it, and requires a clean title marker, file, foreground HWND, and Notepad
PID before `Ctrl+W`. A missing tab, pending save, or changed owner reports a
cleanup failure and fails the run without closing an unknown tab. Search allows
64 restored tabs plus this run's retained fixture count; the total
retained set is bounded by selected checks and repeats.

## Caller audit and validation

H1 and I1 read a fresh document after dictation. I4 checks paste and restores
its clipboard snapshot immediately; I5 does the same while checking the
replacement. I6 requires its fresh document to stay empty and compares the
clipboard-only output. O3 and O4 type into a fresh document for overlay focus
checks; O4 also changes overlay placement, which still restores per check. None
requires the previous Notepad window to be absent. H8 intentionally creates
three distinct tabs and may clear only its verified no-Alt tab for the same-tab
direct sender comparison; its history, UI Automation, menu, and saved-byte
oracles remain unchanged. The `run.NOTEPAD_CHECKS` preflight also includes O4.

Local harness tests cover broad selection with repeats and distinct fixture
names, immediate clipboard and other state restoration, reuse across two owned
windows, foreign or changed-owner rejection, F3's immediate explicit close,
failure-abort handling, and final save/ownership failure without unsafe close.
The 192-test desktop harness suite passes locally. New native broader-selection
and full-suite runs are still required; the owner coordinates the desktop before
those runs. The PR and previous Windows checks were green at the old head only.
No tag or release gate is opened by this change.
