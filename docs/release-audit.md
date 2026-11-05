# Public release audit

November 5, 2026. Release candidate: 0.4.1. Scope: Linux x86_64 and Flipper Zero.
Android and Windows are deferred as described in [the roadmap](roadmap.md).

## Fixed in this candidate

| Finding | Change |
| --- | --- |
| Fork pull requests could run Flipper builds on the configured self-hosted runner | Added a fork guard and disabled persisted checkout credentials |
| Linux could publish without a successful Flipper build | Draft-first release with both jobs required and an exact asset check |
| A stale helper PID could target an unrelated Linux process | Own the helper child directly; never read a PID file to signal a process |
| Helper descendants could outlive shutdown | Stop the owned process group before reaping its leader |
| Temporary transcript files had predictable shared paths | Use a private random runtime directory and remove the payload after delivery |
| Direct typing escaped text as though it were shell input | Pass literal text as an argument after `--` |
| Debian packages omitted typing and window-detection tools | Declare `xdotool` and `x11-utils` |
| Source installation could touch an engine directory before checking symlinks | Validate the destination before venv creation |
| Flipper builds could pass Python bytecode to the linker | Restrict application sources to C files |
| Repository lacked a license and current platform guidance | Add GPL version 3, rewrite the README, and separate current support from plans |

## Checks performed locally

- TypeScript compilation, production frontend build, and token lint passed.
- All 33 UI tests and 20 token-lint self-tests passed.
- All 43 Python engine tests passed with network and inference mocked.
- All 22 installer, CPU-audit, and release-validator tests passed.
- Rust suites passed with and without Enhance: 171 tests per run. Real-model
  tests were ignored because they require a separately installed model.
- After the final helper lifecycle change, all three helper regression tests passed.
- The Flipper app built against API 88.2. The C protocol test passed with warnings
  treated as errors.
- Actionlint, ShellCheck, shell syntax, release version checks, protocol checks,
  Markdown file links, and whitespace checks passed.
- The README banner was rendered and visually inspected.
- The existing v0.4.0 Linux and Flipper downloads were downloaded and checked
  against all six published SHA-256 hashes. The frozen engine started, and the
  portable executable pair matched the standalone asset hashes.

These checks do not establish that 0.4.1 is published. The release workflow still
has to build and publish the candidate from its final source tag.

## Remaining validation limits

This audit did not perform a fresh hardware microphone test, a clean-account
package installation, physical Flipper USB/Bluetooth checks, or a minimum-CPU
Enhance run. Native Wayland input remains unverified. Windows and Android are
excluded from this release. Use [the manual checklist](parity/README.md) to record
hardware results against the actual release tag.

## Dependency follow-up

The npm audit and the Python requirements audit reported no known vulnerabilities. Cargo audit reported no vulnerability
entries, but did report unsoundness warnings for transitive `glib 0.18.5`
(RUSTSEC-2024-0429) and `memmap2 0.8.0` (RUSTSEC-2026-0186), a yanked
`async-executor 1.14.0`, and unmaintained dependencies. The review did not find
application calls to the affected glib iterator or memory-map range APIs. These
warnings still need follow-up through their upstream dependencies.

Speech model downloads currently follow upstream repository revisions. Pinning
those revisions would make model installs reproducible. GitHub Actions, uFBT,
the Flipper release SDK channel, and some Python build tooling also use mutable
versions. The lockfiles do not make the entire release build reproducible.

The audit found no committed credential or private-key patterns. That scan is
not a proof that every historical commit is free of sensitive data.
