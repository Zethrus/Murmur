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

**Verify:** unpack the official crate and compare it with this folder:

```sh
curl -sL https://static.crates.io/crates/wry/wry-0.57.2.crate | tar xz
sha256sum wry-0.57.2.crate   # if downloaded separately
diff -ru wry-0.57.2 src-tauri/vendor/wry
```

The diff must contain only the two `.cargo*` metadata files and the hunks in
`wry-host-input.patch`.

**Remove when:** a released Wry exposes the option (watch #1850). Then drop
`[patch.crates-io]` and the direct `wry` dependency, and pass the option through the
supported API.
