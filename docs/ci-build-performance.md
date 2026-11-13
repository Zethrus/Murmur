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
checkout; normal source cleanup cannot erase the compiler cache. Each cache namespace is tied to one registered runner name. A GitHub runner
agent runs one job at a time, so writes to a namespace are serialized by that
agent. Do not register two agents with the same name and shared tool-cache path.
There is no workflow/job concurrency group that can replace an older pending
build or queued release. Separate runner names get separate cache namespaces.

Before each self-hosted build, a target over 30 GiB is discarded, download
caches over 8 GiB are discarded separately, and a 40 GiB total namespace limit
prunes downloads before the target. Caches for other Rust versions unused for
14 days are removed within the same repository/runner/platform namespace.
`cargo/bin` is kept when its downloads are pruned. Cleanup fails on symlinked
or junction cache paths, including the `cargo` parent before pruning its
children. Linux build scripts preserve the configured persistent pip and
PyInstaller cache paths. Hosted runners retain `setup-node` npm caching and
`Swatinem/rust-cache`; the self-hosted jobs skip those GitHub cache upload and
compression steps. Both platforms still run their linked llama.cpp CPU audit
against the actual release target, packaging checks, size budgets and signing
guards.
The junction regression is mocked in the Linux script suite; a native Windows
junction run on Edison remains to be validated live.

## Cold and two warm builds per platform

The parent reviewer should run normal cold/warm PR builds before merging where
runner availability and billing permit. No speedup has been measured for these
changes yet. Use ordinary trusted PR or manual builds at the **same commit**,
runner, toolchain and lockfiles. Keep the runner
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

## Queued ordinary build fallback

This opt-in GitHub Actions controller covers same-repository PR builds and
ordinary branch `workflow_dispatch` builds on a configured self-hosted runner.
Windows manual runs with `gpu=true`, tag runs, signing jobs, forks on a
self-hosted runner, and desktop jobs are excluded. Set repository variable
`MURMUR_BUILD_FALLBACK_ENABLED=true` only after the workflows have merged to
the default branch. `MURMUR_BUILD_FALLBACK_DELAY_MINUTES` defaults to `10`
(allowed `1..120`). The delay is measured from the current attempt's build
job `created_at`, or its attempt `run_started_at` when the job timestamp is
unavailable. A rerun of an old run gets the full queue threshold.

A `workflow_run` event asks the trusted default-branch controller to inspect
that source or hosted run. A ten-minute schedule reconciles only active source
and hosted runs, without a seven-day creation filter; it can find a newly
rerun old run. If the original build job is still queued after the threshold,
the controller reserves a dispatch attempt and requests a hosted build. It
reconciles visible hosted runs before retrying an uncertain `503`/timeout,
waits at least three minutes between reservations, and stops after three
attempts. The original build is never cancelled. An uncertain accepted request
can create more than one hosted *job* after a retry, but an atomic claim ref
allows only one job to proceed to checkout and compilation. Extra queued or
started hosted jobs can still consume hosted allocation minutes.

The controller claims the first eligible running build job (self-hosted or
hosted) through an immutable annotated tag ref keyed by platform, original
run, and attempt. The claim records the exact original SHA and one winning
run/attempt. The build gate reads that ref with a read-only token, rechecks the
latest source run, attempt, and SHA immediately before compilation, and exits
without compiling if it lost. The claim and dispatch reservations are retained
so a long queue or rerun cannot reopen the same attempt. A completed hosted
job without a claim, 30-minute hosted allocation stall, or exhausted dispatch
retries publishes an `error` result instead of leaving an endless `pending`
status. A later self-hosted claim can supersede that status if the job starts.

The privileged controller checks same-repository PR ownership, open/current
PR head, current test-merge SHA, merge parents, and workflow path. It requires
the trusted run title for fallback eligibility.
It checks source workflow blob equality with the default branch **only for
hosted fallback eligibility**. A same-repository PR editing its build workflow
still runs the ordinary build and tests; it cannot use fallback. The
controller checks an ordinary build job's conclusion before publishing green,
so a skipped job cannot masquerade as a passing build. If an edited workflow
changes the run title, its ordinary job still runs, but no stable status is
published because the controller cannot verify the run's tested SHA from that
title. The controller never checks out or executes PR code. Ordinary build
jobs have `contents:read` and `actions:read`; only the default-branch
controller has write permissions for fallback coordination. Trusted tag-only
release jobs separately hold release write permissions.
The release caller has `contents:write` and `actions:read`, while reusable
build jobs explicitly downgrade to read. Signed Linux release work is in the
trusted tag-only `release.yml` job. Windows tag assets stage on the sole
configured Windows release runner, are hash-checked, then upload directly to
the draft from a separate tag-only write-token job. That path does not require
Actions artifact storage; ordinary throwaway-signed artifact upload remains
best effort. The Windows staging contract requires that `WINDOWS_RUNNER`
select one physical runner with a persistent `RUNNER_TOOL_CACHE` between jobs.
The uploader uses the build job's recorded attempt number, so rerunning only
the upload job does not look for assets under the uploader's newer attempt.
Flipper release compilation and direct upload run in a tag-only `release.yml`
job; the standalone Flipper build workflow has a read-only token.

The stable PR commit status contexts are `Murmur Linux build result` and
`Murmur Windows build result`. Manual builds use separate `Murmur Linux manual
build result` and `Murmur Windows manual build result` contexts so they do not
overwrite PR status at the same SHA. The winning run's conclusion controls the
status; a losing original job may appear green after skipping compilation.
No required check currently exists on `master` (branch protection returned 404
and rulesets were empty when this change was made). After live validation,
require the two PR contexts, along with any other independent checks. Do not
require the raw `build` job as the fallback's canonical result.

The controller is still a hosted Actions job. A healthy self-hosted build must
wait for the trusted controller's event-triggered claim; no measured claim
latency is available yet. The read-only gate waits at most seven minutes and
fails visibly before compilation if Actions scheduling or the API cannot
supply a claim. There is no safe fail-open path with read-only build tokens:
bypassing the atomic claim could duplicate compilation. Ten-minute scheduled
reconciliation means up to **144 controller job starts per day** while enabled,
plus event-triggered starts. At a hypothetical 10–30 seconds of runner time per
scheduled start, that is 24–72 hosted runner minutes per day before any billing
rounding; it is an estimate, not a measurement. The scan asks for active runs
rather than repeatedly scanning seven days of completed runs. GitHub may delay
or drop scheduled events, so the ten-minute queue threshold is eligibility,
not a fallback start-time guarantee. An outage longer than gate timeout can
fail a healthy self-hosted build; more than 100 simultaneous active runs needs
manual investigation. Status POST has no compare-and-set, so a PR update
between final freshness read and POST can show a stale status. Since scheduled
scans do not revisit completed runs, a dropped follow-up event can leave it
until a new run or manual correction. Hosted billing and allocation on this
account remain unverified.

Before merging, run `python3 -m unittest discover -s scripts/tests -p
'test_*.py'`. For a local read-only scan, set `GH_TOKEN`,
`GITHUB_REPOSITORY`, and the relevant `LINUX_RUNNER_CONFIGURED=true` or
`WINDOWS_RUNNER_CONFIGURED=true`, then run `python3 scripts/ci-build-controller.py
control --dry-run`. Leave the enable variable unset on the PR. After merge,
set it, trigger a manual controller dry run, and then observe a queued ordinary
PR and branch manual build on each platform. The parent reviewer should record
cold and two warm build measurements separately; none are claimed here.

GitHub references: [reusable workflow token permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations),
[workflow-run API and status filter](https://docs.github.com/en/rest/actions/workflow-runs),
[jobs by attempt](https://docs.github.com/en/rest/actions/workflow-jobs),
[workflow dispatch](https://docs.github.com/en/rest/actions/workflows),
[atomic ref creation](https://docs.github.com/en/rest/git/refs), and
[commit statuses](https://docs.github.com/en/rest/commits/statuses).
