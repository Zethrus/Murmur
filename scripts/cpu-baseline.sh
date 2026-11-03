#!/usr/bin/env bash
# Sourced, not executed: export the x86-64/SSE2 baseline for the pinned llama.cpp
# backend. The pinned sys crate leaves AVX2 on even with GGML_NATIVE=OFF, so the
# features are disabled at the compiler too, and the generated CMake flags are
# audited afterwards (scripts/audit-llama-cpu.py). Sourcing keeps release builds
# and CI on one definition instead of two drifting copies.
if [[ -n "${RUSTFLAGS:-}" ]]; then
  printf '%s\n' 'Release RUSTFLAGS must be empty: the build enforces the x86-64/SSE2 baseline.' >&2
  return 1
fi
export RUSTFLAGS='-C target-cpu=x86-64 -C target-feature=-avx,-avx2,-fma,-f16c,-bmi2,-sse4.2'
for flag in GGML_NATIVE GGML_AVX GGML_AVX2 GGML_FMA GGML_F16C GGML_BMI2 GGML_SSE42 GGML_CPU_ALL_VARIANTS; do
  export "$flag=OFF"
done
