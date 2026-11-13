# CI build cache and measurement

The ordinary Linux and Windows builds use Rust **1.100.0**. The successful Linux
run [43462609681](https://github.com/Zethrus/Murmur/actions/runs/43462609681) and
Windows manual/GPU run
[43472148187](https://github.com/Zethrus/Murmur/actions/runs/43472148187) both
reported `rustc 1.100.0 (0ef1bc386 2026-11-09)`. CI checks this exact version
after toolchain setup. Do not advance it without a passing build on both hosts.

On self-hosted runners, `scripts/ci-cache.py prepare` stores the Cargo target,
Cargo registry/git downloads, pip wheels, npm downloads, and PyInstaller cache
under `RUNNER_TOOL_CACHE/murmur-build-cache`. The path is scoped by repository,
runner name, OS, architecture and Rust version, and is outside `GITHUB_WORKSPACE`.
The checked-out `.cache/artifacts` folders and fresh engine venv remain in the
checkout; normal source cleanup cannot erase the compiler cache. The build and
release jobs on each platform share a job concurrency group so only one writer
uses that platform's runner-local cache at a time. The group can replace an
older **pending** build under GitHub's default concurrency semantics; inspect
cancelled runs when collecting measurements.

Before each self-hosted build, a target over 30 GiB is discarded, download
caches over 8 GiB are discarded separately, and a 40 GiB total namespace limit
prunes downloads before the target. Caches for other Rust versions unused for
14 days are removed within the same repository/runner/platform namespace.
`cargo/bin` is kept when its downloads are pruned. Cleanup fails on symlinked
cache paths. Hosted runners retain `setup-node` npm caching and
`Swatinem/rust-cache`; the self-hosted jobs skip those GitHub cache upload and
compression steps. Both platforms still run their linked llama.cpp CPU audit
against the actual release target, packaging checks, size budgets and signing
guards.

## Cold and two warm builds per platform

The parent reviewer will run live CI after this branch is merged. No speedup
has been measured for these changes yet. Use ordinary trusted PR or manual
builds at the **same commit**, runner, toolchain and lockfiles. Keep the runner
free of other heavyweight jobs. Do not set Windows `gpu=true` for this ordinary
build comparison; GPU validation is a separate gate. If a run is cancelled or
the host changes, repeat the sequence.

1. Record runner name, OS/arch, commit SHA, Rust version, available RAM and free
   disk. For the **cold** run, remove only that runner's namespace through the
   runner's normal maintenance procedure; do not delete other namespaces. The
   first run after Rust pinning is cold without manual removal because its path
   is versioned. The next two runs are **warm 1** and **warm 2**, without cache
   removal.
2. For each run, record run ID/status, queue wait, `Build frozen engine`/`Freeze
   the engine`, Rust tests, components, app build and package/verify step
   durations from the Actions job steps. The workflow summary records elapsed
   time, target/pip/npm/Cargo cache sizes, free disk and available RAM at start
   and finish. This is a snapshot of available memory, not peak process RSS.
   Check the logs for any target or download cache pruning and for hosted cache
   restore/save outcomes.
3. Run the full existing test, audit, size, signing and release checks. Compare
   warm 1 and warm 2 to cold for each stage and the whole job; report both raw
   times and percentage changes. Report cache size and disk headroom, failures,
   and any rerun separately. Never infer a saving from this protocol alone.

Baseline context from runs before this change:

| Platform / run | Rust tests | Components | App | Other |
| --- | ---: | ---: | ---: | --- |
| Linux [43462609681](https://github.com/Zethrus/Murmur/actions/runs/43462609681), `f3c153b` | 595 s | 435 s | 640 s | Package verify 168 s; GPU 70 s |
| Windows [42683062495](https://github.com/Zethrus/Murmur/actions/runs/42683062495) | 775 s | 344 s | 725 s | Rust cache post 369 s with gzip tar |

Those runs had different conditions and are context only, not the cold/warm
comparison. The latest successful Windows manual/GPU run is 43472148187 at
`f3c153b`.

## Queue fallback decision still required

A 10-minute `timeout-minutes` setting cannot cap queue time: GitHub applies it
after a runner starts the job, and self-hosted jobs can remain queued for up to
24 hours. A scheduled controller can inspect the workflow-job API every five
minutes and dispatch a hosted ordinary build after a queued self-hosted build
has aged 10 minutes. GitHub may delay scheduled workflows, so 10 minutes is the
eligibility threshold, not an exact start deadline. If the controller cannot
reach the API, it must make no fallback decision and report the error on its
next run. Hosted billing/allocation failure must be visible as a failed run.

Two viable follow-on choices require an explicit CI status policy:

- **Separate hosted run with an atomic claim ref:** both jobs attempt to create
  the same run-specific Git ref before any build step. Only the winner builds;
  the loser exits without compiling. This prevents duplicate builds in a
  queue/start race without cancelling a running build. The hosted run is a
  separate check run, while the original self-hosted job may remain queued or
  later succeed as a loser. The required PR check must be moved to a trusted
  aggregator that reports the winner's result. Claim refs need delayed cleanup
  after GitHub's 24-hour queue limit.
- **Cancel queued run, then dispatch hosted:** simpler status wiring, but the
  queued state can change between the API read and cancellation. The documented
  GitHub Actions API has no atomic “cancel only if still queued” operation, so
  this can cancel a build that just started. It does not meet the
  no-running-build-cancellation rule.

The first choice is the safe direction, but it changes branch protection/check
identity and introduces a privileged cross-run controller. It has not been
enabled in these workflows. GPU tests, release tags, and the interactive desktop
runner must stay exclusively self-hosted.
