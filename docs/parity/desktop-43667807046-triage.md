# Focused Windows desktop report 43667807046

At `926178a4ffda57e95897c9a2de4b2d27f58b023a`, H1, H8, I1, I4 and I5 all failed on
Windows 10.0.26200. The report and screenshots are in `/tmp/murmur-desktop-43667807046` on the
test host.

- H8 without Alt kept UI Automation focus on Notepad's editor. Its screenshot visibly shows
  `The ps` followed by spaces and a 26-character status. The harness saved its disposable
  document (`saved_file_updated: true`) and compared the file with the exact 45-character
  same-utterance history output; it differs at character 4, exactly as the UI Automation readback
  does. This is actual editor corruption, not just a TextPattern readback artifact.
- H8 with Alt kept editor focus through F9 up and Shift up. Immediately after Alt up, focus moved
  to Notepad's File menu, then to a Title list item. Its saved file was not updated, so its
  saved-file comparison is not evidence of what was typed. H1 and I1 also recorded File menu
  focus after the hotkey; I4 and I5 recorded empty Notepad documents.

The Windows enigo 0.2.1 `text` implementation submits the whole transcript in one `SendInput`
call. The screenshot and saved file prove corruption, but do not prove the call size is its only
cause. Its source also has a separate exact-Unicode defect: for a supplementary character, the
low surrogate's key-up event uses the high surrogate's scan code.

The Windows typing path now submits one complete character at a time with a 12 ms processing
interval. Each UTF-16 code unit has a matching down/up pair; surrogate pairs are submitted in a
single call. Before each character it rechecks the selected foreground window identity and
delivery permissions. Paste is submitted as one chord after that same check. A changed target or
delivery error holds the full transcript in memory with a warning that partial text may be present;
it does not record successful delivery in history. Windows menu mode is treated as a blocked
destination instead of typing menu accelerators. No Escape, synthetic modifier release, forced
refocus or clipboard fallback is used to conceal an Alt release transition.

The next native gate is H8, H1, I1, I4 and I5 on this commit, followed by the full desktop suite.
In particular, Windows 11 Notepad may use custom menu UI whose state is not exposed through
`GetGUIThreadInfo`; the Alt path must be observed natively before it can be called repaired. The
12 ms interval likewise needs the saved-file equality assertion on Edison. Release remains closed
until those exact insertion and focus checks pass.
