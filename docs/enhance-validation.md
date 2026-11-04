# Enhance validation

Enhance ships **off by default** (`enhance.auto_rewrite = false`). With it off, dictation behaves as it did in v0.1.2.

## What has been run

Automated checks on the local Linux checkout (Ubuntu 24.04, X11):

- `cargo test`: 106 passed, 2 ignored (real-model and hardware-gated).
- `cargo test --no-default-features`: 106 passed, 1 ignored. Three consecutive runs, no flakes.
- `npm run build`: TypeScript, design-token lint and Vite passed.
- `node scripts/check-tokens.mjs`: 25 files, zero violations.
- `npm run test:ui`: 33 passed.
- `engine/.venv/bin/python -m pytest engine/tests -q`: 42 passed.
- `scripts/build-linux.sh`: deb and AppImage built; the CPU-baseline audit reports the linked llama.cpp cache clean across nine compiler flag files.

Differential regression: `postproc::tests::enhance_off_matches_release_corpus` compares 2,688 deliveries against the v0.1.2 corpus with zero differences. Independently confirmed end-to-end: the same dictation through the real v0.1.2 binary and this build produced identical text.

## Real model, real hardware

Qwen3-1.7B Q4_K_M (1,107,409,472 B, SHA-256 `b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897`) was downloaded through the pinned catalog entry, installed, and exercised through the installed application on CPU:

- Measurements from this machine (AMD Ryzen 7 7735HS, 8 cores / 16 logical CPUs), using 8 threads and six short dictation rewrite cases in `enhance::tests::real_model_rewrite_quality`: the SSE2 build took 26.6–32.9 s per rewrite; the sixth case exhausted the test's shared 180 s budget with `EnhanceError::Timeout`. The same code built with default GGML flags (AVX2 enabled) took 2.0–2.7 s for the same six cases. These measurements compare native compiler flags; they do not include the timeout, thread or prefill changes.
- Builds now keep Rust at x86-64/SSE2 for the runtime guard and enable AVX2, FMA, F16C, BMI2 and SSE4.2 in llama.cpp. Native tuning, AVX-512 and all-variant builds are disabled and the generated compiler flags are audited. The old SSE2-only native backend incurred a measured 10x+ inference penalty.
- Shipped-build check on the same machine: `cargo test --release real_model_rewrite_quality -- --ignored` against the release artifact that `scripts/audit-llama-cpu.py` audited completes the same six cases in 2.3–2.9 s with 8 threads (the default on this CPU after `worker::effective_threads` switched to physical cores). Timeout moved to 60 s and prefill batches to 256 in the same change; neither is covered by that measurement.
- Accept copies to the clipboard; the original typed text is never replaced.
- Exceeding the deadline surfaces "Enhance stopped: generation exceeded the configured time limit. Original dictation is unchanged." rather than failing silently.

## Known limits

- **The 1.7B model is a weak editor.** On already-clean dictation it usually answers "unchanged"; pushed to expand, it has invented requirements in testing. Expect punctuation, casing and filler-word cleanup, not a semantic rewrite. Qwen3-4B is the obvious upgrade at roughly 2–3× the latency.
- Rewrite quality is not measured by any corpus; only synthetic dictations reviewed by a human.
- Windows overlay interaction and native rewriting are unverified on Windows hardware.
- Minimum-CPU hardware execution is unverified; Enhance requires AVX2 + FMA + F16C (for example, Intel Haswell from 2013 or newer). Rust remains on the SSE2 baseline and the runtime guard reports Enhance unavailable on CPUs missing those features before native inference can run.
- The in-process llama backend cannot hard-preempt a hung native call; cancellation is observed between tokens.
- Four independent review passes (8 → 6 → 4 → 3 findings) were applied before release; the remaining review notes concern edge cases of the Enhance-on path, not the default behaviour.
