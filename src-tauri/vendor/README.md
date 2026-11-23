# Vendored crates

## `wry/`: Wry 0.57.2 with one opt-in WebView2 option

**Source:** the crates.io release `wry 0.57.2` (`.crate` SHA-256
`a490119196d02a51d0871df7b9119a1f3e46d75b7e90053a801454d5c6b3491c`, which is the
checksum previously locked in `Cargo.lock`). The registry metadata files
`.cargo-ok` and `.cargo_vcs_info.json` are omitted. Everything else is byte-for-byte
upstream except the change recorded in `wry-host-input.patch`.

**Patch:** `wry::set_host_input_processing_ids(&["overlay"])` lists webview ids whose
WebView2 controller is created with
`ICoreWebView2ControllerOptions4::AllowHostInputProcessing(true)`. The setter body is
taken from the upstream proposal
[tauri-apps/wry#1850](https://github.com/tauri-apps/wry/pull/1850). Tauri passes the
window label as the Wry webview id, and Tauri exposes no builder hook for this option,
hence the id list. The list can be set once; if it is never set, Wry behaves exactly as
upstream. A runtime without the interface logs a warning and creates the controller
unchanged, as upstream proposes. Non-Windows code is unchanged.

**Why:** Murmur's overlay lost the mouse press whenever another Murmur window on the same
UI thread (Settings, including the hidden Settings window just after launch) was the
foreground window. The overlay renderer's immediate parent belongs to the WebView2
browser process
([run 44030456571](../../docs/parity/desktop-44030456571-o8-topology.md)). The isolated
reproducer showed that host input processing restores mouse delivery, drag start and
clean release
([run 44041874181](../../docs/parity/desktop-44041874181-o9-host-input.md)).

**Verify:** both platform build workflows verify the archive's SHA-256, apply the
recorded patch in a temporary directory, and compare every vendor file byte-for-byte:

```sh
python3 scripts/verify-vendored-wry.py
# Offline, with a previously downloaded archive (the same hash is still required):
python3 scripts/verify-vendored-wry.py --archive /path/to/wry-0.57.2.crate
```

Only the two `.cargo*` metadata files are omitted. Unrecorded changes, missing or
extra files, archive hash mismatches, and patches that do not apply fail verification.
Git attributes preserve upstream bytes on Windows as well as Linux. Verification
does not modify the vendor tree. Unit tests use small local archives and need no network.

**Remove when:** a released Wry exposes the option (watch #1850) and Tauri exposes it
through a supported integration. Then drop `[patch.crates-io]`, the direct `wry`
dependency and this verifier, and pass the option through that API. Validate the
migration independently; the released Wry focus fix alone does not replace host
input processing.
