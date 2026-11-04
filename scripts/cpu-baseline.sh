#!/usr/bin/env bash
# Sourced, not executed: keep Rust on x86-64/SSE2 so its runtime CPU guard is safe,
# but enable AVX2 for the pinned llama.cpp backend. SSE2-only inference measured
# a 10x+ latency penalty. Do not add Rust target-feature flags: the sys crate maps
# them back to GGML options, overriding the native contract below. Generated CMake
# flags are audited afterwards (scripts/audit-llama-cpu.py). Sourcing keeps local,
# installed and CI builds on the same definition.
baseline='-C target-cpu=x86-64'
# Applying the baseline twice is fine (the CI cache action re-applies it); any other
# value is a foreign toolchain configuration and must stop the build.
if [[ -n "${RUSTFLAGS:-}" && "${RUSTFLAGS}" != "$baseline" ]]; then
  printf 'Refusing to build with RUSTFLAGS=%s: the release requires baseline Rust codegen with an AVX2 native backend.\n' "$RUSTFLAGS" >&2
  return 1
fi
export RUSTFLAGS="$baseline"
for flag in GGML_AVX GGML_AVX2 GGML_FMA GGML_F16C GGML_BMI2 GGML_SSE42; do
  export "$flag=ON"
done
for flag in GGML_NATIVE GGML_AVX512 GGML_CPU_ALL_VARIANTS; do
  export "$flag=OFF"
done
