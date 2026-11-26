# H9 Windows focus containment

The H9 native trace showed an Alt shortcut leaving RichEditD2DPT for an
InputSiteWindowClass File menu while the foreground HWND stayed unchanged and
Win32 menu flags stayed false. The old window-only comparison therefore allowed
delivery to the wrong keyboard recipient.

Windows focus identity now includes the verified `GUITHREADINFO.hwndFocus` HWND,
its owning thread, PID and process start time. A focused child may belong to a
separate embedded renderer process; that identity is checked independently. If
the child is missing, outside the foreground window, invalid, elevated, or
unverifiable, delivery holds the transcript. The default `hold` mode also holds
when the child changes within the same foreground window. The type path checks
that identity before every character.

On Windows, accepted dictation and code shortcut actions carry the focus read from their
callback to the controller. Stop, cancel and rebind do not store an activation
snapshot. A deliberate **Insert here** retry reads its own current target. The
configured `insert` mode still opts into the current verified target; `refocus`
still activates the original top-level window, then requires the original child
identity before delivery. Top-level activation alone cannot restore child focus.
Linux keeps its existing controller-thread capture and X11 identity behavior.
The Windows reader rechecks foreground/thread/PID, child focus, menu flags and
child ownership after gathering process metadata. These checks remain best effort,
not an atomic guarantee between reading the destination and sending input.

Native run 45282636518 on `1da9804f7550e8b2f7f0456244321b3784f7d3b8`
confirmed containment for its three Alt trials: clipboard-only, typing and paste
all held 44-character transcripts when keyboard focus changed to File. The
editors remained empty, typing emitted no Unicode packets, and paste emitted no
Ctrl+V. No Alt trial produced an output history row. This is containment evidence
for this run, not an Alt-delivery pass or proof for every activation timing.

Direct Unicode and all three non-Alt paths passed their exact assertions. The
guard observed 221 successful probes without a miss; final cleanup retired all
seven owned tabs without a cleanup failure. Evidence is in the run's
`H9-notepad-delivery.json`, app logs and `desktop-report.json`:
https://github.com/Zethrus/Murmur/actions/runs/45282636518

The clipboard-only Alt trial retained editor HWND 2495130 through the snapshot
after Alt-up, then reported HWND 2360254 after Ctrl-up, approximately 7 ms later.
The settled UIA read identified File. These samples bracket the observed change;
they do not establish which release or queued message caused it. The non-Alt
control retained its editor HWND throughout. Guard probes remained active in
both cases, so the comparison does not independently exclude a probe interaction.

The approved next comparison varies Murmur registration and explicit release
order on fresh owned documents, with native focus stamps and exact event traces.
It must distinguish "not configured in Murmur" from proven OS registration and
must retain failed observations. Ordinary H9 and release assertions stay intact.
No menu-cancellation or synthetic masking repair is justified by this timing
evidence alone. A held transcript remains an interruption, not successful delivery.

Parent review restricted callback capture to Windows and added the final native
identity recheck. Independent review found no additional behavior blocker; its
Windows expression compile finding was corrected before a successful GNU Windows
cross-check. Local validation passes 527 Python script tests, 13 focus-policy
tests, and the full no-default-features Rust suite. The executor also reported
the full default-feature Rust suite passing before those scoped parent changes.
Lifecycle tests establish action/helper ownership, not native cancel/rebind queue
timing. The bounded native result above does not replace those lifecycle checks,
ordinary insertion acceptance, or full-suite and manual release validation.
