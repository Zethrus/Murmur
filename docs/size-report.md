# Download size report (Linux and Windows)

Units: MB = 10⁶ bytes, MiB = 2²⁰ bytes. "Download" is the file a user fetches;
"unpacked" is what an AppImage expands to; "installed" is the `.deb`
`Installed-Size`. Every number below is measured unless it is labelled
**estimate**.

## Before: v0.4.7 release

Sizes from the GitHub release API; unpacked size from `check-release.py sizes`
on the downloaded AppImage; `.deb` installed size from its control file
(fetched with an HTTP range request).

| Artifact | Download | Unpacked / installed |
| --- | ---: | ---: |
| `Murmur_0.4.7_amd64.AppImage` | 1,708.2 MB (1,629.1 MiB) | 2,783 MB (2,654.0 MiB), gzip SquashFS |
| `Murmur_0.4.7_amd64.deb` | 1,704.3 MB (1,625.4 MiB) | 2,789.9 MB (2,660.6 MiB) under `/opt/murmur` |
| `murmur-linux-x86_64.tar.gz` | 1,856.1 MB (1,770.2 MiB) | 1,861.3 MB: AppImage + a second 153.1 MB copy of the engine |

What the 0.4.7 AppImage contained, unpacked (ranked):

| # | Component | Unpacked | zstd -19 | Needed? |
| --- | --- | ---: | ---: | --- |
| 1 | cuDNN 9 (8 libraries) | 1,329 MB | ≈530 MB **estimate** | No: never loaded (see below) |
| 2 | cuBLAS + cuBLASLt | 854 MB | 468 MB | Only for GPU inference |
| 3 | NVRTC (two copies) | 227 MB | 67 MB | No |
| 4 | WebKitGTK, GTK, ICU and other client libraries | 263 MB | ≈75 MB **estimate** | Yes, in the AppImage |
| 5 | `murmur-engine` (PyInstaller onefile, already compressed) | 153 MB | — | Yes; but ≈128 MB of it was PyAV/FFmpeg and 11 MB hf_xet, both unused |
| 6 | `murmur` binary, not stripped | 41 MB (≈10 MB symbols) | — | Yes, stripped |
| 7 | Same `.so` copied 2–3× instead of symlinked | 20 MB | — | Duplicates |
| 8 | Engine copied again into the tar.gz | +153 MB | — | Duplicate |

### Method

- AppImage payload offset = end of the runtime's ELF section-header table
  (`e_shoff + e_shentsize × e_shnum`); `unsquashfs -o <offset>` lists or
  extracts the SquashFS without executing the AppImage
  (`check-release.py appimage_offset`).
- Per-component sizes: `du --apparent-size` on the extracted tree.
- zstd figures: `zstd -19` per file. Rows marked **estimate** are approximate
  figures from the baseline analysis, not file-by-file measurements. A trial
  `mksquashfs` re-pack of the 0.4.7 AppDir without CUDA and engine gave
  109.8 MB (gzip) and 89.9 MB (zstd).
- GPU check behind "never loaded": the released engine on an RTX 4060 (driver
  580.178.04) with only `libcublas.so.12` and `libcublasLt.so.12` on
  `LD_LIBRARY_PATH` transcribed correctly with `int8_float16`, `float16`,
  `float32` and `int8`, VAD on and off. CTranslate2 4.6.3's Linux wheel has no
  cuDNN or NVRTC references; the Whisper encoder's Conv1D layers run as
  im2col + cuBLAS GEMM. This holds for that CTranslate2 version only, so the
  build now checks it (`scripts/check-engine-imports.py`).

## After: components (PR-B)

Built on einstein-probe (Ubuntu 24.04.5) with `scripts/build-linux.sh` on
2026-11-07 (UTC); numbers from `python3 scripts/check-release.py sizes`, the
`.deb` control file and the generated manifest. Versions still read 0.4.7.

The packages contain only the app. The engine and the GPU add-on are signed
component zips downloaded on request; models come from Hugging Face.

| Artifact | 0.4.7 | PR-A (interim) | **PR-B** | Unpacked / installed (PR-B) | Budget |
| --- | ---: | ---: | ---: | ---: | ---: |
| AppImage | 1,708.2 MB | 679.0 MB | **90.8 MB** (86.6 MiB) | 296.4 MB unpacked (282.7 MiB) | 125 MiB |
| `.deb` | 1,704.3 MB | 533.6 MB | **10.5 MB** (10.1 MiB) | 39.5 MB installed (37.7 MiB) + system packages | 125 MiB |
| tar.gz | 1,856.1 MB | 678.1 MB | **90.1 MB** (85.9 MiB) | the AppImage only | 130 MiB |
| Engine component zip | — | — | **65.4 MB** (62.4 MiB) | 297.5 MB installed (283.7 MiB) | 130 MiB |
| CUDA add-on zip (cuBLAS only) | — | — | **456.4 MB** (435.3 MiB) | 854.4 MB installed (814.8 MiB) | 520 MiB |
| Manifest + signature + certificate | — | — | 24.5 KB | — | 1 MiB each |

Speech models (from the manifest; download = installed): tiny.en 78.1 MB,
base.en 147.8 MB, small.en 486.1 MB, distil-small.en 335.5 MB, distil-large-v3
1,516.5 MB, large-v3 3,090.8 MB, large-v3-turbo 1,621.7 MB. Enhance model
1,107.4 MB.

### Totals per configuration

App + engine + `distil-small.en`; GPU adds the CUDA add-on. "On disk" counts the
AppImage file itself (it is not unpacked) or the installed `.deb` files, plus the
installed components; `.deb` system packages are not counted.

| Configuration | 0.4.7 download | PR-B download | Change | 0.4.7 on disk | PR-B on disk |
| --- | ---: | ---: | ---: | ---: | ---: |
| AppImage, CPU | 2,043.7 MB | **491.8 MB** | −75.9 % | 2,043.7 MB | 723.9 MB |
| AppImage, NVIDIA GPU | 2,043.7 MB | **948.2 MB** | −53.6 % | 2,043.7 MB | 1,578.2 MB |
| `.deb`, CPU | 2,039.8 MB | **411.5 MB** | −79.8 % | 3,125.4 MB | 672.6 MB |
| `.deb`, NVIDIA GPU | 2,039.8 MB | **868.0 MB** | −57.4 % | 3,125.4 MB | 1,527.0 MB |
| First download only (app) | 1,708.2 MB | **90.8 MB** AppImage / **10.5 MB** `.deb` | −94.7 % / −99.4 % | — | — |

0.4.7 needed the same model download on top of the package; its onefile engine
also unpacked about 300 MB into `TMPDIR` on every start, which is not counted
above.

### How the components were packed

- Deterministic zips with LZMA (`scripts/bundle-linux-runtime.py components`).
  The CUDA add-on with deflate was 554 MiB (over its budget); LZMA gives
  435.3 MiB. The engine zip went from 96.3 MiB (deflate) to 62.4 MiB.
- Installing from the local zips with the release binary (`--install-component`,
  verify + extract + smoke test): engine 5.9 s, CUDA add-on 20.8 s on this machine.
- The engine zip leaves out PyInstaller's five convenience symlinks into
  `ctranslate2.libs/` and `numpy.libs/` (the component manager refuses links); the
  frozen engine was checked to start, transcribe on the CPU, and run the full GPU
  matrix without them.
- GPU validation with the *installed* components on the RTX 4060
  (`scripts/test-linux-inference.py`, `distil-small.en`, 3 s fixture; evidence
  `docs/parity/evidence/gpu-validation-components-distil-small-en-rtx4060.json`):
  all four precisions with VAD on and off gave the same transcript with only the
  add-on's cuBLAS on the loader path; libraries loaded: `libcublas.so.12` and
  `libcublasLt.so.12` from `components/cuda/<version>/` and the host
  `libcuda.so.580.178.04`; no cuDNN or NVRTC. Warm median 65.0 ms (PR-A, packaged
  runtime: 64.0 ms), cold first inference 1.03 s, engine launch through first
  reply 1.23 s; CPU warm median 1.38 s. (An earlier run of the same script on a
  previous build, with other builds running, measured 74.3 ms.)

## After: PR-A (interim)

Built on einstein-probe (Ubuntu 24.04.5) with `scripts/build-linux.sh` on
2026-11-06; numbers from `python3 scripts/check-release.py sizes` and `du`.
Version strings still read 0.4.7 because this PR does not bump versions.

| Artifact | Before (0.4.7) | After | Change | Unpacked / installed after | Budget |
| --- | ---: | ---: | ---: | ---: | ---: |
| AppImage | 1,708.2 MB | **679.0 MB** (647.5 MiB) | −60.3 % | 1,444.9 MB unpacked (1,378.0 MiB), was 2,783 MB | 700 MiB |
| `.deb` | 1,704.3 MB | **533.6 MB** (508.8 MiB) | −68.7 % | 1,187.9 MB installed (1,132.9 MiB) plus system packages, was 2,789.9 MB | 550 MiB |
| tar.gz | 1,856.1 MB | **678.1 MB** (646.7 MiB) | −63.5 % | the AppImage only (679.0 MB) | 710 MiB |
| Engine (onedir, inside both packages) | 153.1 MB onefile, unpacked to `TMPDIR` on every start | 297.5 MB on disk (283.7 MiB), ≈79.3 MB of the AppImage | no per-start unpack | — | — |

The `.deb` installed size counts only Murmur's files. Its dependencies
(WebKitGTK, GTK, xdotool, x11-utils, AppIndicator, ALSA, …) come from the
distribution and are usually already installed on a desktop.

What makes up the new AppImage (each part re-packed alone with the same
settings, zstd level 19, 1 MiB blocks):

| Component | Unpacked | In the AppImage |
| --- | ---: | ---: |
| cuBLAS + cuBLASLt (`usr/lib/murmur/cuda`) | 854.3 MB | 510.8 MB |
| Engine (`usr/lib/murmur/engine`) | 297.5 MB | 79.3 MB |
| App, WebKitGTK/GTK and other libraries, typing tools, icons | 293.1 MB | 88.9 MB |

What changed:

- cuDNN and NVRTC are no longer shipped (−1,556 MB unpacked).
- Engine: onedir instead of onefile; PyAV/FFmpeg and hf_xet left out.
- `murmur` stripped: 31.2 MB instead of 41.4 MB.
- Duplicate library copies replaced by symlinks.
- SquashFS: zstd level 19 with 1 MiB blocks instead of gzip. Measured on the
  same AppDir: zstd/128 KiB blocks 703.7 MiB, zstd/1 MiB 646.6 MiB, xz/1 MiB
  614.2 MiB. zstd was kept because GPU start-up reads the 750 MB cuBLASLt
  through FUSE and zstd decompresses much faster than xz.
- `.deb`: no `/opt/murmur` copy of the AppImage runtime; system dependencies;
  xz-compressed.
- tar.gz: only the AppImage; the second engine copy is gone.

Performance on the same machine (`scripts/test-linux-inference.py`,
`distil-small.en`, 3 s fixture, automatic `int8_float16`): warm median 64.0 ms
(earlier onefile build, as recorded in `docs/building.md` before this change:
65.0 ms), engine launch through first reply 1.27 s (earlier: 2.59 s). All four GPU precisions, with VAD on and off, ran with only
the shipped cuBLAS directory on the loader path; no cuDNN or NVRTC library was
loaded. The same matrix passed with `large-v3-turbo`, and `large-v3` ran at
`int8_float16`, `float16` and `int8` (its `float32` weights do not fit the test
GPU's 8 GB). `libcublasLt.so.12` contains the string `libnvrtc`; none of these
runs loaded it. Evidence: `docs/parity/evidence/`.

### Interim budget versus the plan (PR-A)

The plan proposed interim budgets of 400 MiB (AppImage, `.deb`) and 410 MiB
(tar.gz). cuBLAS alone is 510.8 MB (487 MiB) compressed, so those budgets cannot
be met while it ships inside the packages. The enforced budgets are the
measured sizes plus about 8 % headroom: 700 / 550 / 710 MiB. PR-B moves the
engine and cuBLAS into optional downloads and lowers them to 125 / 125 / 130
MiB; the "App, WebKitGTK…" row above (88.9 MB) is what remains.

### What stays large, and why

- **cuBLAS (≈511 MB compressed)**: needed for GPU inference. PR-B makes it an
  optional download for NVIDIA users only.
- **WebKitGTK and GTK in the AppImage (≈250 MB unpacked)**: an AppImage must
  carry its web view; the `.deb` uses the system copy instead.
- **llama.cpp inside `murmur`**: Enhance runs in-process on the CPU, so the
  31 MB binary includes it.
- **Engine (≈297 MB unpacked)**: CTranslate2 (≈136 MB), onnxruntime for Silero
  VAD (≈59 MB), OpenBLAS in NumPy (≈37 MB), tokenizers and Python itself.

## Windows (PR-C): measured on Edison

Measured by `check-release.py sizes` in the green Edison run (2026-11-08, run
42502808128, version 0.4.7). Unpacked = sum of zip entries (the installer is
reported as its own size).

| Artifact | Download | Unpacked | Budget |
| --- | ---: | ---: | ---: |
| `Murmur_<ver>_x64-setup.exe` (NSIS, per-user, embedded WebView2 bootstrapper) | 10.6 MiB | — | 35 MiB |
| `murmur-windows-x86_64.zip` (portable `murmur.exe` + README) | 11.2 MiB | 29.2 MiB | 35 MiB |
| `murmur-engine-windows-x86_64-<ver>.zip` | 42.8 MiB | 178.6 MiB | 130 MiB |
| `murmur-cuda-windows-x86_64-<ver>.zip` (cuBLAS 12.9.2.10 only) | 429.9 MiB | 735.5 MiB | 520 MiB |

Removed from what the Windows CTranslate2 wheel would otherwise bring along:
`cudnn64_9.dll` (266 KB dispatcher shim; `ctranslate2.dll` never references
cuDNN) and, from the cuBLAS wheel, `nvblas64_12.dll`, headers and import
libraries. Typical Windows CPU setup: installer 11.1 MB + engine 44.9 MB + `distil-small.en`
335.5 MB ≈ 392 MB; GPU adds 450.8 MB.
