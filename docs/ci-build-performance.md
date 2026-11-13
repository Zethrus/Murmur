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
or junction cache paths, including the `cargo` parent before pruning its
children. Linux build scripts preserve the configured persistent pip and
PyInstaller cache paths. Hosted runners retain `setup-node` npm caching and
`Swatinem/rust-cache`; the self-hosted jobs skip those GitHub cache upload and
compression steps. Both platforms still run their linked llama.cpp CPU audit
against the actual release target, packaging checks, size budgets and signing
guards.

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

## Queued ordinary PR build fallback

This is an opt-in controller for same-repository PR builds on a configured
self-hosted runner. Set repository variable `MURMUR_BUILD_FALLBACK_ENABLED=true`
**only after these workflow files have merged to the default branch**. The
trusted controller runs from that branch on a five-minute schedule and on
`workflow_run` activity. It inspects the original build job's state. If that
job is still **queued** at or after `MURMUR_BUILD_FALLBACK_DELAY_MINUTES`
(default `10`; allowed `1..120`), it records a dispatch guard and starts one
hosted fallback workflow. The original is never cancelled by the controller.
GitHub may delay or drop scheduled events, so the threshold is eligibility,
not a start-time guarantee. `MURMUR_BUILD_FALLBACK_ENABLED=false` disables new
controller decisions; do not change it while active builds are awaiting claims.

Both build jobs wait at their start for the trusted controller to create one
immutable annotated tag ref under `refs/tags/ci-build-claims/v1/`. Its identity
includes platform, original run ID and attempt. The ref's tag message names
exactly one winning run/attempt and the original PR test-merge SHA. Only that
job proceeds to source checkout and compilation. The losing job exits
successfully without building. A second immutable dispatch tag prevents two
controller executions from dispatching twice, even if the dispatch API's
response is lost. The controller never deletes these refs automatically:
GitHub can leave self-hosted jobs queued for 24 hours, and reruns or retained
run records can still refer to them. An owner may prune old refs manually only
after the relevant runs, attempts, and desired rerun window have expired.

The controller and status publisher execute default-branch code with
`actions:write`, `contents:write`, and `statuses:write`. Build jobs have only
`actions:read` and `contents:read`; they never receive a branch-write token.
The Windows tag release upload is a separate job with `contents:write`, after
its build, so ordinary PR code does not receive that permission. The controller
checks same-repository PR ownership, open/current PR head, current test-merge
commit, byte-identical source workflow file against the default branch,
source workflow path/name, latest run/attempt, and job state. Hosted
fallback checks out the immutable original test-merge SHA rather than the
fallback workflow's default-branch SHA. No PR checkout is run by the controller.
PRs that edit these build workflow files will fail this trust check and need a
separate trusted review/validation path before the fallback can be used.
Forks, manual/GPU-requested runs, tags, signing, and desktop jobs are outside
this fallback path. The existing release workflow remains self-hosted.
With a self-hosted runner configured, fork PR build jobs are skipped and do not
receive these stable statuses; requiring the contexts will block fork merges
until a separate trusted hosted-fork policy is designed.

The stable commit status contexts are `Murmur Linux build result` and
`Murmur Windows build result`. The trusted controller publishes `pending`,
then the winning run's success/failure, against the current PR head. The
original job may later appear green as a losing no-op; use the stable status to
judge the build. No required check currently exists on `master` (branch
protection returned 404 and rulesets were empty when this change was made).
**Recommended after live validation:** require these two exact status contexts
in branch protection/rulesets, and retain any other desired independent checks.
Do not require the raw `build` job as the fallback's canonical result.

Before merging, run `python3 -m unittest scripts.tests.test_ci_build_controller
scripts.tests.test_ci_cache`. For a local read-only scan, set `GH_TOKEN`,
`GITHUB_REPOSITORY`, and `LINUX_RUNNER_CONFIGURED=true` and/or
`WINDOWS_RUNNER_CONFIGURED=true`, then run
`python3 scripts/ci-build-controller.py control --dry-run`.
The dry run reads GitHub state but writes no refs,
dispatches, or statuses. Leave the enable variable unset on the PR: the
controller cannot be triggered until its workflow file exists on the default
branch. Run ordinary PR cold/warm builds now where feasible. After merging,
set the enable variable, trigger a **manual dry run** of `Trusted build fallback
controller`, check logs, then test a queued ordinary PR on each platform with
carefully observed billing and runner availability. The controller's manual
dispatch defaults to dry run; choose `dry_run=false` only for a real scan.

A controller API error fails its run and makes no optimistic fallback decision.
A build gate times out after seven minutes without a claim, failing the job
before compilation. The hosted gate is bounded but uses hosted minutes while
waiting. The dispatch log states when hosted billing/allocation may apply; the
controller fails visibly if a recorded dispatch has no hosted run after the
threshold plus 20 minutes. A queued hosted run can still fail allocation or
reach GitHub's own queue limit; there is no automatic second fallback. The
winning build's failure remains the result. Hosted billing availability and
cost on this account are unverified. No paid larger-runner labels are used.
The controller and its jobs still depend on GitHub scheduling/API availability,
and GitHub's commit-status API has no compare-and-set operation: a concurrent
new PR event between the final freshness read and status POST can briefly
leave an older status. The next controller scan re-evaluates current runs;
branch protection should be enabled only after that behavior is observed live.
Scans cover the last seven days of runs; a longer controller outage needs a
manual investigation and rerun rather than an automatic stale publication.

GitHub API references: [scheduled workflows and delay](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
[PR test merge SHA](https://docs.github.com/en/rest/pulls/pulls),
[atomic create-ref response](https://docs.github.com/en/rest/git/refs),
[workflow dispatch](https://docs.github.com/en/rest/actions/workflows),
[workflow jobs by attempt](https://docs.github.com/en/rest/actions/workflow-jobs),
and [commit statuses](https://docs.github.com/en/rest/commits/statuses).
