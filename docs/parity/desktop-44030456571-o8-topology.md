# O8 topology and keyboard-oracle run 44030456571

Exact build 44028249077 of `b837738e83fdb97debe800bb22b7b8446a74fbc5` (Wry 0.57.1,
WebView2 157.0.4341.46 as reported by the loaded environment). Guarded
`--only O8 --repeat 1`; preflight, guard and cleanup completed without an abort. An
earlier dispatch, 44030312382, was refused at preflight because O8 must run alone;
nothing was launched.

## Results

| Case | Held movement | Same-root focus change | Typed after release |
| --- | --- | --- | --- |
| Cold, hidden Settings foreground | Failed | None | Not attempted (no visible target) |
| Owned Notepad foreground | Passed | None | `o8k1` landed exactly |
| Visible Settings, search field focused | Failed | None | `o8k2` landed exactly in the field |
| Same Notepad foreground | Passed | None | `o8k3` landed exactly |

The restart restored the last successful position with a 0 px difference. The
activation traces were valid, as in run 43996483597. Keyboard focus was preserved in
every case, including both failed Settings-thread gestures: the remaining defect is
lost mouse delivery and drag entry, not keyboard focus loss.

## Topology (identical in all four gestures; complete, twice-read, no errors)

| Window | Process / thread | Class | Ex-style |
| --- | --- | --- | --- |
| Overlay root | Murmur UI thread | `Tauri Window` | `0x08040118`: `WS_EX_NOACTIVATE`, `WS_EX_APPWINDOW`, `WS_EX_TOPMOST` |
| Controller host (app record matches) | Murmur UI thread | `WRY_WEBVIEW` | none |
| WebView2 in-process host | Murmur UI thread | `Chrome_WidgetWin_0` | none |
| **Renderer's immediate parent** | **msedgewebview2 browser process** | `Chrome_WidgetWin_1` | `WS_EX_NOREDIRECTIONBITMAP` only |
| Hit renderer | msedgewebview2 browser process | `Chrome_RenderWidgetHostHWND` | `WS_EX_TRANSPARENT` |

Classification: **foreign immediate parent**. The controller host is owned and on the
hit chain, but it is two levels above the renderer, and a foreign-owned window lies in
between.

## Decision

Under plan section 3, Batch C, this selects the second row. An owned-host style change
cannot reach the renderer's immediate parent without modifying a foreign window, which
the plan forbids. The next production candidate therefore needs a minimal, isolated
`AllowHostInputProcessing` reproducer with release/capture acceptance tests. Do not
add `WS_EX_NOACTIVATE` to `WRY_WEBVIEW` or `Chrome_WidgetWin_0`, and do not try more
hand-back variants.

The Wry 0.57.2 run (plan step 2) is still pending. It addresses a later movement stage
and is not expected to change the failed pre-drag cases.

## Run 2: Wry 0.57.2 (44032865194)

Exact build 44030918182 of `8ee45d10cd470ae9c6e3ccc40d7251c2fbc9a539` (adds Wry 0.57.2).
Guarded `--only O8 --repeat 1` completed without an abort. The outcome was identical:
cold and Settings gestures did not move while held, with no drag log line; both Notepad
gestures moved, with seven drag log lines each. There were no same-root focus changes,
all three typed tokens landed exactly, the restart error was 0 px, and topology was
unchanged (foreign immediate parent).

As expected, Wry 0.57.2 neither fixes the pre-drag failure nor regresses the passing
cases. Keep it as an independent dependency update, still subject to Linux regression
evidence, and do not count it as an O2 repair.
