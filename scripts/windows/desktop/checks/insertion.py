"""I1-I6: Notepad, editor, Windows Terminal, Prefer paste, non-ASCII, clipboard only."""
import re
import secrets

import fixtures
import targets
import win32
from checks.common import CODE_HOTKEY, require_empty
from context import Fail, Skip


def i1_notepad(ctx):
    expected = ctx.golden("A")
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"injection": {"method": "type"}})
    pad = ctx.notepad()
    pad.focus()
    ctx.dictate("A")
    text = pad.wait_text()
    ctx.settle()
    ctx.screenshot(pad.hwnd, "notepad.png")
    ctx.expect(expected, text, "Notepad")


def i2_editor(ctx):
    if targets.VSCode.executable():
        # VS Code is a code app (postproc.code_apps), so auto style is code: compare with the
        # code-style golden text made through the code hotkey.
        expected = ctx.golden("B", hotkey=CODE_HOTKEY, style="code")
        ctx.murmur.ensure(ctx.fixtures["B"].path)
        editor = targets.VSCode(ctx.work, ctx.check_id)
        editor.start()
        ctx.restorer.push("close VS Code", editor.close)
        editor.focus()
        ctx.dictate("B")
        ctx.sleep(3.0)
        ctx.settle()
        text = editor.text()
        ctx.screenshot(editor.hwnd, "vscode.png")
        ctx.expect(expected, text, "VS Code")
        return
    expected = ctx.golden("B")
    ctx.note("VS Code is not installed: TargetApp in the editor role stands in")
    ctx.murmur.ensure(ctx.fixtures["B"].path)
    app = ctx.target("editor", role="editor")
    app.focus()
    ctx.dictate("B")
    text = app.wait_text()
    ctx.settle()
    ctx.expect(expected, text, "TargetApp (editor role)")


def i3_terminal(ctx):
    if not targets.Terminal.available():
        raise Skip("Windows Terminal (wt.exe) is not installed")
    ctx.murmur.ensure(ctx.fixtures["A"].path)
    terminal = targets.Terminal().start()
    ctx.restorer.push("close Windows Terminal", terminal.close)
    before = terminal.buffer()
    if before is None:
        raise Skip("this Windows Terminal version does not expose its buffer through UI Automation")
    prompts = len(re.findall(r"(?m)^PS [^>\n]*>", before))
    terminal.focus()
    ctx.dictate("A")
    ctx.sleep(3.0)
    ctx.settle()
    after = terminal.buffer() or ""
    ctx.screenshot(terminal.hwnd, "terminal.png")
    lines = [line for line in after.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    win32.tap("esc")  # PowerShell clears the line: nothing is ever run
    if len(re.findall(r"(?m)^PS [^>\n]*>", after)) != prompts:
        raise Fail("A new prompt appeared: the dictation was executed")
    if not last.startswith("PS ") or "quick brown fox" not in last.lower():
        raise Fail(f"Sentence A is not on the prompt line: {last[-120:]!r}")


def i4_paste(ctx):
    expected = ctx.golden("LONG")
    ctx.murmur.ensure(ctx.fixtures["LONG"].path, {"injection": {"prefer_paste": True}})
    ctx.clipboard_guard()
    sentinel = f"murmur-sentinel-{secrets.token_hex(4)}"
    win32.set_clipboard_text(sentinel)
    pad = ctx.notepad()
    pad.focus()
    ctx.dictate("LONG")
    text = pad.wait_text()
    ctx.settle()
    ctx.sleep(2.0)
    clipboard = win32.clipboard_text()
    ctx.screenshot(pad.hwnd, "notepad.png")
    ctx.expect(expected, text, "Notepad (paste)")
    if clipboard != sentinel:
        raise Fail(f"Clipboard not restored after the paste: {clipboard!r}")


def i5_unicode(ctx):
    spoken, written = fixtures.REPLACEMENT
    config = {"postproc": {"replacements": {spoken: written}}}
    expected = ctx.golden("MARKER", variant="replacement", config=config)
    if written.lower() not in expected.lower():
        raise Fail(f"The replacement rule did not apply (model heard {expected!r}); harness fixture problem")
    ctx.murmur.ensure(ctx.fixtures["MARKER"].path, config)
    ctx.clipboard_guard()
    pad = ctx.notepad()
    pad.focus()
    ctx.dictate("MARKER")
    text = pad.wait_text()
    ctx.settle()
    ctx.screenshot(pad.hwnd, "notepad.png")
    ctx.expect(expected, text, "Notepad")


def i6_clipboard_only(ctx):
    expected = ctx.golden("A")
    # inject::choose_path matches exclude_apps as a substring of the focused app's name.
    ctx.murmur.ensure(ctx.fixtures["A"].path, {"injection": {"exclude_apps": ["notepad"]}})
    ctx.clipboard_guard()
    win32.set_clipboard_text("before")
    pad = ctx.notepad()
    pad.focus()
    ctx.dictate("A")
    ctx.sleep(3.0)
    ctx.settle()
    require_empty(pad)
    clipboard = win32.clipboard_text() or ""
    if clipboard.strip() != expected.strip():
        raise Fail(f"Clipboard holds {clipboard!r}, expected the transcript {expected.strip()!r}")
    if clipboard != expected:
        ctx.note(f"clipboard text {clipboard!r} differs from the typed form only in whitespace")
