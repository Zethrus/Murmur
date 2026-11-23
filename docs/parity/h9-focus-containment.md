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

This is designed to contain the observed same-window menu transition in default
hold mode; native effectiveness remains unverified. It does not repair Alt
menu activation or prove exact delivery. H9 remains unresolved until native
hardware validation confirms the Alt paths and text/clipboard result. A held
transcript is an interruption, not a passing exact-delivery result.

Parent review restricted callback capture to Windows and added the final native
identity recheck. Independent review found no additional behavior blocker; its
Windows expression compile finding was corrected before a successful GNU Windows
cross-check. Local validation passes 527 Python script tests, 13 focus-policy
tests, and the full no-default-features Rust suite. The executor also reported
the full default-feature Rust suite passing before those scoped parent changes.
Lifecycle tests establish action/helper ownership, not native cancel/rebind queue
timing. All Windows hardware behavior remains to be checked on the resulting build.
