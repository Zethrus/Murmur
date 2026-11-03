# Enhance validation

Automated checks on the local Linux checkout:

- `export TMPDIR=$PWD/.cache/tmp; cargo test --manifest-path src-tauri/Cargo.toml`: 81 passed.
- `npm run build`: TypeScript, design-token lint and Vite passed.
- `node scripts/check-tokens.mjs`: 25 files, zero violations.
- `npm run test:ui`: 29 passed.
- `npm run test:tokens`: 20 self-tests passed.
- `/usr/bin/python3 -m pytest engine/tests -q`: blocked because system Python lacks pytest; `engine/.venv/bin/python -m pytest engine/tests -q`: 42 passed.
- `cargo test --manifest-path src-tauri/Cargo.toml --no-default-features`: 81 passed; exercises the unavailable-backend path.
- `npx tauri build --no-bundle`: passed; release binary at `src-tauri/target/release/murmur`. This is a compile check, not a launch test.

The Rust suite uses fake backends and byte-stream fixtures; it does not attest to actual Qwen output, the full model download, or live window activation. The pinned SHA-256 came from metadata and remains unverified against the model bytes. No model was downloaded during development. No measured warm/cold inference, quality corpus, resource baseline, or Windows native runtime acceptance matrix exists yet. Manual X11 focus/drag, clipboard, monitor/DPI and offline-network checks have not been completed; Windows build/runtime is unverified on Windows hardware. Do not represent this as a release-quality or performance pass.

To complete the release gate, explicitly install and verify the GGUF, test inference offline with representative synthetic dictations and human review, exercise click/drag and focus with real target applications on Linux and Windows, then measure latency/cancellation/RSS and validate packaging on clean Windows hardware. The in-process llama backend cannot guarantee hard preemption of a hung native call.
