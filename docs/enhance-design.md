# Enhance phase 1

Enhance is optional local rewriting after Whisper dictation. The original cleaned transcript is injected immediately; a single CPU inference worker subsequently proposes a rewrite in the overlay. Acceptance copies the proposed text to the clipboard, without pasting or replacing the original. Discard leaves the original and clipboard untouched. No transcript-bearing suggestion history is stored. New dictation invalidates an outstanding request/suggestion; a stale ID cannot be accepted. Manual rewrite applies only to the latest delivered dictation and works with automatic rewriting off.

## Boundaries

- Whisper stays in its Python sidecar. The optional `enhance-llama` Cargo feature (on by default) uses in-process `llama-cpp-2 =0.1.160` with GPU/OpenMP features disabled. Builds without it report an unavailable backend, while dictation continues.
- The model catalog is compiled in. The only entry is `unsloth/Qwen3-1.7B-GGUF` revision `d7f544eead698dbd1f15126ef60b45a1e1933222`, file `Qwen3-1.7B-Q4_K_M.gguf`, 1,107,409,472 bytes, expected SHA-256 `b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897`. Metadata came from Hugging Face; the hash has NOT been independently checked against downloaded bytes in this implementation run. The upstream model is described as Apache-2.0; verify the precise artifact's license/provenance before distribution. Models are never bundled or downloaded automatically.
- Explicit installation streams HTTPS into a temporary file under the separate Enhance data directory, checks length and checksum, and atomically promotes it. Startup, configuration load and rewrite have no network path. Local verification precedes model load. The model is mmap-backed and lazily loaded; the worker cancels cooperatively and unloads after inactivity.
- The deterministic cleanup stage always runs before injection and respects the user's filler, punctuation and capitalisation switches. Duplicate-word removal defaults on in prose only. Rewritten text is not reprocessed with those transformations on acceptance.
- Prompts treat transcript as data, protect technical spans, use Qwen's non-thinking serialization and a constrained JSON response. Invalid, expanded or changed protected data is rejected. Users inspect the result before copying; semantic equivalence is not guaranteed.
- Auto-rewrite defaults off; rewrite and accept shortcuts default unassigned. Settings suggests Ctrl+Alt+Shift+R and Ctrl+Alt+Shift+A without assigning them. Code/terminal rewriting is excluded until explicitly enabled. When the overlay is disabled, automatic rewriting stays inactive.
- The overlay is nonfocusable, draggable by a handle, sized to its content and stores a custom monitor-relative position. Only the overlay window may report its size and start a native drag. Pending snapshots additionally carry a request ID for targeted cancellation; only ready snapshots carry a suggestion ID for acceptance. Suggestions override idle hiding; injection temporarily makes the overlay click-through.

## Future work, not implemented

Phase 2 is opt-in verified-selection replacement with platform accessibility identity and selection checks. No character-count deletion, undo trick, arbitrary clipboard reading or blind paste is allowed. This phase provides only clipboard-copy delivery; no cloud inference or persistent suggestions are planned.

See [enhance-validation.md](enhance-validation.md) for the tests and unverified manual/runtime behavior.
