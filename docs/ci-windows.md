# Windows CI and the Edison runner

Status, November 13, 2026: the self-hosted Windows manual/GPU build passed in
[run 43472148187](https://github.com/Zethrus/Murmur/actions/runs/43472148187)
at `f3c153b`. A hosted Windows build and the release workflow still need live
validation. The ordinary build cache and cold/warm measurement protocol are in
[CI build performance](ci-build-performance.md).
After default-branch activation, same-repository ordinary PR and branch manual
builds queued on Edison can use the hosted fallback described there. Manual
`gpu=true` runs, release tags/signing, and the interactive desktop runner never fall back.
Hosted Windows allocation and billing on this account are still unverified;
the fallback uses `windows-latest`, not a paid larger-runner label.

## The runner

Edison is the owner's Windows 10/11 x64 PC with an NVIDIA GPU. It runs the GitHub
Actions runner as a **Windows service** under a dedicated standard account,
`murmur-ci` (not an administrator). Labels:

```
self-hosted, Windows, X64, gpu, nvidia
```

Provisioning is described step by step in the prompt handed to the owner's agent
on Edison, and implemented by `scripts/windows/provision-runner.ps1` (elevated
PowerShell; idempotent):

```powershell
.\scripts\windows\provision-runner.ps1                       # checks, account, tools, staged model
.\scripts\windows\provision-runner.ps1 -RunnerToken <token>  # + register the runner service
.\scripts\windows\provision-runner.ps1 -VerifyOnly           # report only
```

It checks Windows (21H2+), the NVIDIA driver (R528.33+, the Windows floor for
CUDA 12.x; never upgraded by the script) and 60 GB free; creates `murmur-ci` with "Log on as a service"; installs
Git, GitHub CLI, PowerShell 7, VS 2022 Build Tools (C++ and Windows SDK), CMake,
LLVM, rustup (machine-wide in `C:\rust`), Node 22, Python 3.12, NSIS, WebView2
and 7-Zip with winget; stages `distil-small.en` offline in
`C:\murmur-release-models`; downloads the runner (SHA-256 from the release notes)
and registers it with the one-time token the owner creates at
<https://github.com/Zethrus/Murmur/settings/actions/runners/new>. It never
installs the CUDA Toolkit or cuDNN: only the driver comes from the host, the app
brings cuBLAS. It does not run the "smoke test as `murmur-ci`" step (that needs
an interactive logon with the account's password); do that by hand, or let the
first workflow run prove the toolchain.

**The service session cannot run interactive tests.** Jobs run in session 0:
no visible desktop, no global hotkeys, no foreground window, no microphone
prompt. Overlay, hotkeys, injection, tray, autostart and DPI checks run in the
logged-in session instead: automatically on the second, desktop runner (below,
"Desktop runner"), and the rest by a person with
[parity/windows-checklist.md](parity/windows-checklist.md).

## Repository settings (owner)

| Kind | Name | Value | Effect |
| --- | --- | --- | --- |
| Variable | `WINDOWS_RUNNER` | `["self-hosted","Windows","X64","gpu"]` | `windows-build.yml` runs on Edison. Unset: `windows-latest` (hosted, needs billing) |
| Variable | `ENABLE_WINDOWS` | `true` | Windows workflow and the Windows part of `release.yml` run at all |
| Variable | `WINDOWS_GPU_TEST_MODELS` | `C:\murmur-release-models` | Enables GPU validation (manual runs with `gpu=true`, and release tags) |
| Secret | `WINDOWS_CERT_PFX_BASE64`, `WINDOWS_CERT_PASSWORD` | base64 PFX and its password | Optional Authenticode signing; absent → unsigned build, labelled as such |

Fork pull requests never run on the self-hosted runner: the job runs for them
only when `WINDOWS_RUNNER` is unset (hosted). Tags never fall back to a hosted
runner. The release jobs that use Edison's GPU hard-code the labels
`self-hosted, Windows, X64, gpu, nvidia`.

## What `windows-build.yml` does

Pull requests and manual runs (`workflow_dispatch`, input `gpu`):

1. Python 3.12 (`actions/setup-python` on hosted runners; the machine-wide install on Edison, because setup-python runs the MSI and edits HKLM, which the non-admin runner user cannot), Node 22, Rust (isolated toolchain), MSVC + CMake + libclang
   (`scripts/prepare-windows-native.ps1`).
2. Freeze the engine (`scripts/windows/freeze-engine.ps1`): onedir PyInstaller
   from `engine/requirements-windows-release.txt` with `--require-hashes`, prune
   CTranslate2's unused `cudnn64_9.dll` shim, `murmur-engine.exe --help`, then the
   **cuDNN/NVRTC gate**: `scripts/check-engine-imports.py` on the frozen folder
   (string scan + PE import and delay-import tables, pefile required on Windows)
   and on the unfrozen site-packages.
3. UI build and DOM tests, the Windows-relevant script tests, engine pytest,
   `cargo test` with and without the Enhance backend.
4. Component zips first (`scripts/windows/package.ps1 -ComponentsOnly`: engine
   zip, cuBLAS-only CUDA zip from the pinned wheel, the manifest input
   `components-windows-inputs.json`, size budgets), then a Windows-only manifest
   signed with a throwaway key (never a release asset; its certificate is kept
   next to it as `murmur-release.asc`).
5. Authenticode certificate import (**release tags only**, dormant until the
   secrets exist), `tauri build` for the MSVC target (signing through
   `bundle.windows.signCommand` only on tags with an imported certificate),
   certificate removal (always), llama.cpp CPU-flag audit. Pull-request and
   manual builds never receive the certificate secrets, never hold the key in
   `Cert:\CurrentUser\My` and never produce Authenticode-signed artifacts;
   `scripts/tests/test_windows_ci.py` checks this in the workflow text.
   Pull-request and manual builds set `MURMUR_EMBED_MANIFEST` to the throwaway
   manifest, as Linux does, so the first-run setup has a catalog; the app trusts
   it only when started with `MURMUR_DEV_TRUST_ROOT` + `MURMUR_DEV_TRUST_ROOT_ACK`
   (a normal launch still reports "Release trust root missing"). Tags embed
   nothing.
6. `scripts/windows/package.ps1 -SkipComponents`: NSIS installer and portable
   zip (app only), size budgets.
7. Startup smoke test: `murmur.exe --version` and `--help` on every runner.
   On **hosted** runners only (`runner.environment != 'self-hosted'`), also a launch with
   `--background` in a throwaway profile for 10 seconds; it must not exit with an
   error, must write its config, and must not install an engine by itself. The
   self-hosted runner is a Windows service in session 0, where the tray
   (`Shell_NotifyIcon` needs a shell) and WebView2 are unavailable, so the launch
   would fail for reasons unrelated to Murmur; on Edison the interactive
   checklist in the logged-in session covers launch, tray and Settings.
8. With `gpu=true`: `scripts/test-windows-inference.py` (see below).
9. Upload the artifacts (best effort, 3 days, marked untrusted-dev-signed).
10. On Edison only: hand the build to the desktop runner (below).

Release tags (called from `release.yml`): steps 1–7 without the throwaway
manifest (on Edison, so without the launch smoke), then the **release guard**
`scripts/check-embedded-manifest.py` checks `murmur.exe` and the portable zip:
any embedded manifest must be signed by a key of the committed
`src-tauri/trust/murmur-release.asc`, and without that certificate nothing may
be embedded. Only then are the installer, portable zip and both component zips
staged under Edison's `RUNNER_TOOL_CACHE`. A separate tag-only job checks their
SHA-256 hashes and attaches them directly to the **draft** release with `gh`
(no Actions artifact storage or third-party publish action), and
the component inputs are passed to the Linux job as a workflow output. The
Linux release job runs the same guard on its rebuilt binary before its upload.

## Release flow with Windows enabled

```
prepare ─┬─ windows-build (Edison) ── windows upload (Edison) ── linux-build ── linux signing ── windows-gpu ─┐
         └─ flipper ────────────────────────────────────────────────────────────────────────────────────────────┴─ validate
```

- The Linux release job downloads the two Windows zips from the draft, and
  `make-manifest.py --windows-inputs` refuses them unless size and SHA-256 equal
  what the Windows job recorded. One signed manifest lists both platforms.
- `windows-gpu` downloads the final signed assets and runs the GPU matrix.
- `validate` runs `check-release.py assets <tag> <dir> --windows [--windows-gpu]`:
  exact asset set, budgets, portable zip = `murmur.exe` + `README.txt`, manifest
  entries for both platforms pointing at this release, and the GPU evidence.
- Nothing is ever published; the draft stays a draft.

## The GPU gate

`scripts/test-windows-inference.py` (shares `scripts/inference_common.py` with
the Linux script) extracts `murmur.exe` from the portable zip and installs the
engine, the CUDA add-on and the staged model into a throwaway `%LOCALAPPDATA%`
through the app's own verifier (`--install-component`). Then, with
`MURMUR_CUDA_DIR` pointing at the add-on (as the app passes it) and all host CUDA
removed from `PATH`/`CUDA_PATH`:

- forced CUDA: 1 cold + 10 warm runs, all four precisions × VAD on/off, device
  must stay `cuda` without fallback;
- loaded modules: `cublas64_12.dll` and `cublasLt64_12.dll` only from the add-on,
  `nvcuda.dll` from the driver, **no `cudnn*` or `nvrtc*` DLL**;
- Auto on GPU; explicit CPU and Auto with the GPU hidden stay on CPU.

It writes `GPU-VALIDATION-windows.json` with the SHA-256 of the installer,
portable zip, both Windows zips and the manifest. Decision 5 of the plan: the
GPU option stays hidden on Windows until this evidence exists for a release.
Flipping `WINDOWS_GPU_OFFERED` in `src-tauri/src/components/detect.rs` to `true`
is the only code change needed afterwards.

Static evidence gathered while writing this (not a substitute for the gate):
CTranslate2 4.6.3's `ctranslate2.dll` (win_amd64) has no cuDNN import, symbol or
string and loads `cublas64_12.dll` at run time; its wheel nevertheless ships the
266 KB cuDNN 9 dispatcher `cudnn64_9.dll`, which the freeze removes.
`cublasLt64_12.dll` 12.9.2.10 imports only `KERNEL32.dll` but names
`nvrtc64_120_0.dll` for an optional JIT path, exactly like Linux
`libcublasLt.so.12` (which never loaded NVRTC in the Linux GPU matrix).

PyInstaller caveat: CTranslate2 loads `cublas64_12.dll` at run time today, so
PyInstaller's dependency analysis does not see it. If a future CTranslate2
gains a *static* import of a CUDA DLL and a CUDA Toolkit is on the build
machine's `PATH`, PyInstaller may copy the host's `cublas64_12.dll` into
`_internal/`, silently bundling (and preferring) a host copy over the add-on.
The engine-zip budget would probably catch a ~100 MB jump, but the decisive
check is the Windows inference test: its module evidence fails unless cuBLAS
is loaded from the add-on directory. Edison has no CUDA Toolkit installed
(the provisioning script never installs one), which keeps this latent.

## Authenticode

There is no certificate. Builds are unsigned and say so in the job summary and
release notes; asset names do not change. When the owner adds
`WINDOWS_CERT_PFX_BASE64`/`WINDOWS_CERT_PASSWORD`, `scripts/windows/authenticode.ps1`
imports the PFX into the runner user's certificate store (the temporary file is
deleted at once), Tauri signs `murmur.exe` and the installer with `signtool`
(SHA-256, RFC 3161 timestamp) and verifies them, and the certificate and its key
are removed from the store in an `always()` step.

## Desktop runner (`edison-desktop`) and `windows-desktop.yml`

The automatable part of [parity/windows-checklist.md](parity/windows-checklist.md)
(items tagged **auto**) runs on a second runner in the owner's logged-in
desktop session. Not run yet: until a real run on Edison is recorded in
[parity/test-ledger.md](parity/test-ledger.md), nothing it covers counts as
verified.

> **Security warning: jobs on the desktop runner run as the owner's Windows
> account.** They run in the owner's interactive session with access to that
> account's desktop, files, browser sessions and stored credentials. This is
> acceptable only because the only workflow that targets the runner is
> dispatch-only and owner-only. Never add the `desktop` label to another
> workflow, and keep "Require approval for all outside collaborators" enabled
> (Settings → Actions → General): a fork pull request runs its own copy of the
> workflow files and could otherwise name any runner label.

The runner:

- lives in `C:\actions-runner-desktop`, labels
  `self-hosted, Windows, X64, desktop` (deliberately no `gpu`, so the build
  workflows never land on it);
- is not a service: the scheduled task **"Murmur desktop runner"** starts
  `run.cmd` hidden at the owner's logon, so jobs run in the interactive session,
  non-elevated;
- is used only by `windows-desktop.yml`: `workflow_dispatch` only,
  `if: github.actor == github.repository_owner`, concurrency group
  `windows-desktop` (one run at a time), 60-minute timeout, token permissions
  `contents: read` and `actions: read`, checkout without persisted credentials.

**Build hand-off.** Artifact storage is billing-locked, so `windows-build.yml`
(manual runs on Edison only, so a pull-request run cannot write into the folder) ends by copying the installer,
portable zip, component zips, manifest and signature, the throwaway certificate
and `components-windows-inputs.json` to `C:\murmur-ci-out\<commit>\`, writes
`SHA256SUMS` and prints every hash to its job log as
`MURMUR-HANDOFF-SHA256 <commit> <sha256> <file>`. The newest three commits are
kept. When `C:\murmur-ci-out` does not exist the step only prints a notice.
Folder ACLs (`murmur-ci` modify, owner read) are set once when Edison is
provisioned; the workflow never changes them. For a desktop run, dispatch
`windows-build.yml` on the branch first.

**Before anything runs**, the desktop job (`scripts/windows/desktop/handoff.py`)
requires the `sha` input to equal the commit it was dispatched on, copies the
files into the owner's temp folder, hashes the copies and requires them to
equal both `SHA256SUMS` and the hashes in the matching successful
`windows-build.yml` run's own log (found by `head_sha` with `gh api`, read with
`gh run view --log`). A modified hand-off folder therefore fails before any
binary from it runs. The harness dependencies (`comtypes`, `Pillow`) install
into a fresh venv with `pip --require-hashes`.

### Dispatching a run

1. Quit your own Murmur on Edison (tray → Quit) and close Notepad; leave the
   machine unlocked and untouched. The run takes the keyboard and mouse for
   20–30 minutes; any real key press or mouse click aborts it safely.
2. Make sure `windows-build.yml` has run on Edison for the commit:
   `gh workflow run windows-build.yml --ref <branch>`.
3. Dispatch the desktop run on the same commit:

   ```bash
   gh workflow run windows-desktop.yml --ref <branch> \
     -f sha=$(git rev-parse <branch>) -f only= -f repeat=1
   ```

   `only` takes checklist ids (`H1,F2`); `repeat` (1–10) runs each selected
   check that many times to find flaky checks.

The job then checks the sha, verifies the build, runs the preflight (session
unlocked, 60 s without input, no other `murmur.exe`, Notepad closed), shows a
10-second countdown banner and runs `scripts/windows/desktop/run.py`. By hand
(same folder layout): `python scripts\windows\desktop\run.py --build <verified
copy> --out <dir> [--only H1,F2] [--group focus] [--repeat 3]`.

### Read-only idle diagnostic

If the 60-second preflight reports recent input despite the owner leaving Edison
untouched, dispatch `windows-desktop.yml` on the matching commit and SHA with
`-f idle_diagnostic=true`. Leave both guard diagnostic flags false. This owner-only
mode uses the same hash-locked harness venv and writes
`C:\murmur-desktop-reports\<sha>\<run id>-<attempt>\idle-diagnostic.json`.
It skips build handoff, the normal preflight, app launch, and the input guard.
It samples for 90 seconds even when idle is below 60 seconds; it refuses a
wrong or locked session and stops if the session changes during sampling. The
normal desktop run still requires 60 seconds idle and retains all its guards.

Each roughly one-second sample contains a UTC timestamp, elapsed time, raw
`GetLastInputInfo` and `GetTickCount` ticks, wrap-safe computed idle seconds,
and whether the last-input tick changed since the previous sample. The JSON also
contains the interactive session/desktop identity and limited Python runtime
metadata. The job summary lists status, sample and change counts, and change
times. No command lines, window titles, key fields, pointer positions, or
process paths are collected. This mode only reads Windows state; it neither
sends input nor installs hooks, manipulates focus, opens a window, or closes apps.

The timestamp sequence can distinguish a recent reset at job startup from
repeated changes during the 90-second interval. `GetLastInputInfo` includes
injected input, gives no device or process attribution, and does not itself
prove that any specific physical input occurred. This diagnostic provides no
native proof of the cause of the preflight failures. Review the local artifact
before drawing a conclusion or scheduling another desktop test.

For an input-guard failure before the checks, dispatch the same workflow with
`-f guard_diagnostic=true` and the matching `sha`. This branch creates the same
hash-locked harness venv as the full suite, but skips build handoff and app launch.
It requires the owner's unlocked session with no Murmur running, then waits for
the owned diagnostic window to take focus and for 60 seconds without input before
running the isolated guard for 30 seconds. It does not close any app. To reproduce
locally in the owner-controlled, unlocked session without a build, create a fresh
venv with the same requirements:

```powershell
$Venv = Join-Path $env:TEMP "murmur-guard-$PID"
py -3.12 -m venv $Venv
$Python = Join-Path $Venv 'Scripts\python.exe'
& $Python -m pip install --disable-pip-version-check --no-input --require-hashes --only-binary=:all: `
  -r scripts\windows\desktop\requirements.txt
& $Python scripts\windows\desktop\guard_diagnostic.py --seconds 30 --out C:\murmur-desktop-reports\guard-diagnostic.json
```

The diagnostic opens a small window owned by its own process, verifies that
Windows gave that window foreground focus (allowing up to 180 seconds for the
owner to click it if automatic focus fails), and sends a tagged Unicode packet
key-down/key-up pair to it. The foreground owner is checked again immediately
before injection, and the window removes messages without calling
`TranslateMessage`. The packet is a Unicode noncharacter, not a functional
shortcut. If focus never arrives, it sends no controlled key and reports
`probe_window_unavailable`. On exit it tries to restore the previous foreground
window only while its own window is still foreground; it never steals focus back
after another window has taken it. The local `guard-diagnostic.json` reports
Python executable/version/prefix, hook and message-pump state, ctypes structure
sizes, SendInput result, callback counts, and up to eight recent **injected probe
candidates** with their tag comparison. It never records real key fields. A
missing or mismatched probe is still a failure; the diagnostic does not relax the
guard.

For the single teardown-sensitivity experiment, dispatch with
`-f guard_teardown_diagnostic=true` (leave `guard_diagnostic` false) and the
matching `sha`, or add `--teardown` to the local command above. This is a
separate opt-in path in the same hash-locked venv, with no build handoff or app
launch. It keeps one keyboard/mouse hook registration in this diagnostic process
and starts no stage after a 12-second deadline following the 60-second idle
prerequisite; final cleanup can take longer. The test sends
tagged **key-up only** Unicode `VK_PACKET` and reserved-VK controls to focused
owned window A. If both callbacks arrive, it calls `SetFocus(NULL)` on A's
owning GUI thread and repeats the controls while A remains active. Whether an
A probe is observed or missed, it
records before/after teardown snapshots, closes A, and requires both a successful
`DestroyWindow` result and termination of A's window thread before considering B.
It creates owned window B and probes again. The owned window does not call
`TranslateMessage`, and the controlled probes send no key-down. The diagnostic
does not close any user app or intentionally send while an external window is
foreground. Before B is constructed, the foreground must be null on the Default
desktop and the session last-input tick must match the tick after the last A probe.
B is created hidden; its thread repeats those checks before creation, before
showing B with `SetWindowPos(SWP_NOACTIVATE)`, and before requesting foreground.
Once B exists, only null foreground or verified ownership of B is allowed. The
same checks run
again before each B probe, and the observer checks ownership and input before
`SendInput`. An arbitrary previous foreground app is never a permitted handoff.
If B cannot take focus automatically, it stops; an owner click after hooks start
aborts when detected by the hook. Any last-input tick change during the handoff
stops B creation/activation, and any change after a miss blocks recovery. That
tick includes injected input and is only a conservative change detector, not
proof of human input. Foreground and input checks are separate reads, not an
atomic lock on the desktop; a change between the last check and native activation
or `SendInput` remains possible. A missed callback latches failure; the same
registration gets at most one controlled Unicode key-up probe on B for
recovery evidence. If the miss happens on B's initial probe, that one recovery
probe is on B after the handoff. The normal guard worker, its cleanup, and the
0.5-second probe deadline are unchanged.

`stages` records foreground HWND/TID/PID, `GetGUIThreadInfo` active/focus/status,
input desktop, callback message types (`WM_KEYDOWN`, `WM_KEYUP`,
`WM_SYSKEYDOWN`, `WM_SYSKEYUP`), SendInput result, and the hook process/thread
identity and posted/received pump heartbeat around each send and timeout. A
received heartbeat shows the message pump ran, not that the hooks ran. Compare
tags with `window_a` and
`window_b` received messages. `miss_recovered` means the same registration
delivered B's recovery callback after a latched miss; it refutes permanent
removal for that episode and does **not** pass the desktop suite. `miss_persisted`
means no recovery callback arrived. `inconclusive` means this one A/B run did
not reproduce a miss. No probe is sent after A's destruction and before B owns
foreground, so a negative A/B result cannot exclude a hook failure specific to
that no-target gap. The hook runs in the diagnostic process instead of the
product guard's isolated worker, so process-specific effects remain unresolved.
This is one bounded experiment, not a repeated sampling or release gate.

The focus-clear step follows [SetFocus(NULL)](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setfocus),
and its state is read through [GetGUIThreadInfo](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getguithreadinfo).
[SetWindowPos](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowpos)
documents `SWP_NOACTIVATE` for B's initial display.
The [KEYEVENTF_UNICODE key-up](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput)
is documented to produce a `VK_PACKET` key message without a translated
character when the window does not call `TranslateMessage`. The
[GetLastInputInfo](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo)
tick is session-specific and may not be monotonic, so any change stops the
recovery probe without labeling it real input.

The observer creates its message queue before reporting ready. Diagnostic
`heartbeat_posted` and `heartbeat_received` count private messages through that
queue; a received heartbeat shows the pump ran but does not prove either hook
is active. The diagnostic also sends one tagged, zero-displacement mouse move
with no button action. `zero_move_probe_send_result` and
`zero_move_probe_seen` can show whether the mouse hook receives injected input.
A missing mouse callback is inconclusive because the system may suppress a
zero-displacement move. Compare `probe_window.received_probe_events` with
`input_guard.hook_state.owned_probe_messages` and `owned_probe_status`:

- Tagged `VK_PACKET` down/up messages in the owned window with no matching hook
  callbacks show the key event reached the window queue while hook delivery failed.
- Both matching hook callbacks with a missing reserved-key probe show that the
  hook delivered a documented valid event immediately before the miss. The
  reserved key or timing remains suspect; this alone does not establish why.
- No window messages and no callbacks leave generation versus delivery unresolved,
  even when `SendInput` reports two events inserted.

The packet uses the documented [`KEYEVENTF_UNICODE` / `VK_PACKET` path](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput),
with U+FDD0 as a [Unicode noncharacter](https://www.unicode.org/faq/private_use.html).
The diagnostic window checks its messages using
[`PeekMessageW`](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-peekmessagew).
`pump_result: 0` with
`pump_exited_before_stop: false` is the expected cleanup `WM_QUIT`.
Compare `parent_desktop_context` with `input_guard.hook_state.desktop_context`.
The hook thread must be in the same session and window station as the parent,
and `thread_desktop_receives_input` must be `true`. A desktop named `Default`
alone is insufficient: a noninteractive window station can also have a default
desktop. These fields are captured before the hook thread stops. If the valid
packet is not seen by either the window or the hook, the cause remains
unresolved; do not run the desktop checks.

### Bounded Alt release-order comparison (H10)

H10 is opt-in and accepts only `--only H10 --repeat 1`. After the matching
Windows build and fresh owner availability, dispatch `windows-desktop.yml`
with that full `sha`, `only=H10` and `repeat=1`. It uses the normal idle/session
preflight, continuous input guard and passive startup-window handoff. It refuses
clipboard formats that the text restorer cannot preserve. It does not run as part
of the default acceptance suite.

The fixed ten-trial matrix compares four explicit Alt release sequences and a
non-Alt control with the tested chord present or absent from Murmur's disposable
configuration. F9 is released first or last; Alt is released before or after
Ctrl; Shift is always the first modifier released. Each trial gets a fresh owned
Notepad tab. Configured-chord trials request clipboard-only output, so transcript
typing cannot explain a menu transition. Configuration/readiness checks do not
prove that no other application owns the chord: the report deliberately says
"not configured in Murmur". Actual OS registration remains unverified;
configuration and a live overlay alone do not prove RegisterHotKey succeeded.

Read `H10-alt-release.json`, the per-trial app logs and settled captures. Compare
native keyboard-focus snapshots, exact traced key order and guard-probe timing.
UI Automation is deferred until the chord and output have settled. The guard
continues to send its normal probes in both contexts; this experiment does not
independently exclude a probe interaction or establish physical-key behaviour.
Sampling brackets a transition but does not establish the causal Windows message.

Earlier failures remain failures when later trials pass. Unknown foreground
ownership or unsettled delivery stops further trial input. Saving requires the
same verified owned editor; the diagnostic must not refocus or cancel a menu to
turn a failed observation into a pass. H10 final cleanup additionally requires fresh owned-editor focus before saving
and closing; a menu-focused tab is preserved as a cleanup failure. Failed
release of an owned synthetic key permanently blocks subsequent harness input,
including tab cleanup. H10 cannot clear H9, ordinary insertion checks, or any release gate.

### Reading the report

The job summary shows `desktop-report.md`: one row per checklist id with
**pass**, **FAIL** (with the reason), **skipped** (with the reason) or
**aborted** (user input or setup failure), the evidence file names, and
"best effort" on T1, T2, I3 and K1 (Windows UI details: these report skipped,
never pass, when they cannot run). With `repeat` > 1 a row that failed in some
attempts says "flaky". `desktop-report.json`, the PNG/TXT evidence and the
golden texts (the first delivery into the harness's TargetApp under the same
settings) are in `C:\murmur-desktop-reports\<commit>\<run id>-<attempt>\` on
Edison. Treat a failure first as "harness or app?": the report reason, the
screenshot and `murmur.log` excerpts decide; app bugs are reported separately.

The manual items run once per release with
`python scripts\windows\desktop\manual.py --report <that folder>`, which merges
its results into the same report.

What the harness changes outside its throwaway profile, and restores after
every check and again when the run ends (also after an abort): the clipboard
(text only), `AppsUseLightTheme`, the `HKCU\...\Run` and `StartupApproved\Run`
values, the per-user microphone consent value, and the Notepad, Terminal, VS
Code and test windows it opened. P2 runs the installer silently into a temp
folder and uninstalls it again; it is skipped when Murmur is installed for the
account (a second per-user install would take over its Start menu and uninstall
entries), so on a machine where you use Murmur, P2 is covered only by a run on a
clean account. WebView2 keeps its data under the real `%LOCALAPPDATA%` (it
ignores the profile override), and speech fixtures and the downloaded test model
are cached in `%LOCALAPPDATA%\murmur-desktop-cache` between runs (delete it to
reclaim the space).

### Stopping the desktop runner

Disable it (it stays registered and comes back on the next enable + logon):

```powershell
Disable-ScheduledTask -TaskName 'Murmur desktop runner'
# Stop the running instance now (only the desktop runner's, not the murmur-ci service):
Get-CimInstance Win32_Process -Filter "Name='Runner.Listener.exe'" |
  Where-Object { $_.ExecutablePath -like 'C:\actions-runner-desktop\*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId }
```

`Enable-ScheduledTask -TaskName 'Murmur desktop runner'` turns it back on. To
remove it for good, also delete the runner under Settings → Actions → Runners
and the folder `C:\actions-runner-desktop`.
