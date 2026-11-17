# O8 after the overlay input repair: run 44053826670

Exact build 44051727949 of `8dabbf44`. Guarded `--only O8 --repeat 1`: **all four
observations passed**. O8 reports diagnostic status by design and does not clear O2.

| Case | Held movement | Same-root focus change | Activation vetoes | Typed after release |
| --- | --- | --- | --- | --- |
| Cold, hidden Settings foreground | Yes | None | 3 | Not applicable |
| Owned Notepad foreground | Yes | None | 3 | `o8k1` exact |
| Visible Settings, search field focused | Yes | None | 3 | `o8k2` exact |
| Same Notepad foreground | Yes | None | 3 | `o8k1o8k3` exact |

The restart restored the last successful position with a 0 px difference. No overlay
`WM_ACTIVATE` reached the hand-back.

## The repair, in order

| Run | Change | Result |
| --- | --- | --- |
| 44030456571 | Topology record | The renderer's immediate parent belongs to the WebView2 browser process. An owned-host style change is ruled out |
| 44041874181 | O9 reproducer | The baseline reproduces Murmur. `AllowHostInputProcessing` on the overlay restores mouse delivery, drag start and clean release |
| 44045203286 | Vendored Wry 0.57.2 + overlay-only host input (`d5d224b5`) | All four drags move. The Settings field loses focus |
| 44048345662 | UI-thread CBT veto of overlay activation (`cd2f4ba5`) | 0 activations, 3 vetoes per gesture. The field still "loses focus" |
| 44051172677 | Diagnostics (`eb6ad1a`) | The field element is stale: Settings rebuilt its page when the drag saved its position |
| 44053826670 | Settings defers external-config rebuilds while a text field is focused (`8dabbf44`) | All four cases pass |

The Wry patch is documented in `src-tauri/vendor/README.md`. The veto is documented in
`src-tauri/src/overlay/noactivate.rs`. Both are Windows-only. The Settings change applies on
every platform, and its UI test covers the deferred rebuild.

## Still required

Ordinary O2 and the rest of the 48-check suite run next on this exact build. After that:
manual gates, Linux regression for Wry 0.57.2 and the Settings change, final-head CI, and
the release gates in the unblocking plan.
