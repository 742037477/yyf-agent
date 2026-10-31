# T08: commands run isolated, without network by default

- **Assignee:** Claude (core logic and OS-level isolation; GLM-5.3 GO is required because the author is also the final reviewer)
- **Branch:** `task/T08-command-sandbox`
- **Line budget:** ≤ 165 added lines in `m2k/` (tests not counted). Raised from 150 by the author before review:
  fail-closed start detection (`--info-fd`) and three accept scenarios needed the room; nothing speculative.
- **Files you may change:** `m2k/sandbox.py`, `m2k/tools.py`, `m2k/worker.py`, `m2k/verify.py`, `m2k/kernel.py`, `m2k/contract.py`, `m2k/cli.py`, `m2k/accept.py`, `tests/test_sandbox.py`, `tests/test_anchor.py`, `tests/test_ledger_location.py` (only: their CLI configs opt into `[sandbox] backend = "none"`, since `auto` now refuses on Windows), `.github/workflows/ci.yml`, `.github/workflows/m2k-task.yml`, `.github/workflows/real-accept.yml`, `m2k.example.toml`, `README.md`, `docs/WORKFLOW.md`
- **Status:** DONE

## Goal
Stage 1.5, anti-privilege-grab. T04 keeps keys out of a command's reach, but a command can still read
any file the user can read, write outside the workspace, reach the ledger by absolute path, and send
data anywhere over the network. After this card every worker command and the acceptance command run in
an isolated environment: the workspace is the only writable place, only system runtime folders are
readable, there is no network unless the task contract grants it, and no other process is visible.
Where no isolation backend exists, `m2k run` refuses to start unless the operator explicitly opts out,
and the opt-out is written to the ledger.

Owner's computer, checked 2026-10-13: no Docker Desktop, no WSL distribution installed. So the only
backend in this card is Linux bubblewrap (`bwrap`), used on GitHub runners and any Linux machine.
On Windows and macOS there is no backend: real-model runs go to GitHub (`m2k-task`), or the owner opts
out locally with `[sandbox] backend = "none"`. A WSL2 backend is a later card, only if WSL2 gets installed.

## Required behaviour
1. `m2k/sandbox.py`: a backend has `name`, `available() -> (bool, reason)` and
   `argv(command, cwd, network) -> list[str]` (run with `shell=False`).
   - `none`: today's behaviour (`shell=True` in `cwd`).
   - `bwrap`: `bwrap --die-with-parent --new-session --unshare-all [--share-net]`, read-only binds of
     `/usr`, `/etc` and the usr-merge links (`/bin`, `/lib`, `/lib64`, `/sbin` when present), read-only
     bind of the Python installation (`sys.base_prefix`, `sys.prefix`), `--proc /proc --dev /dev
     --tmpfs /tmp`, read-write bind of the workspace only, `--chdir` workspace, `HOME=/tmp`, then
     `/bin/sh -c <command>`. Nothing else is mounted: no home folders, `/root`, `/srv`, `/opt`
     (except the Python prefix), the ledger, `deliver_to`, the seed folder or the m2k code.
     `available()` runs a one-second probe (`bwrap ... true`) and reports why it failed.
2. Config `[sandbox] backend = "auto" | "bwrap" | "none"`, default `auto`.
   `auto` = `bwrap` if available, otherwise `m2k run` exits 2 before submitting, printing (Chinese) that
   this computer has no isolation backend and naming the two options (run on GitHub, or set
   `backend = "none"` to accept the risk). `bwrap` that is unavailable → same refusal.
   `none` → runs, prints a one-line warning, and the ledger says so.
3. Contract `[task] network = false` by default; `true` only when the task owner grants it.
4. Every `run_shell` call (worker commands and the acceptance command) goes through the chosen
   backend with the contract's `network` value. The T04 key guard (scrubbed env, redaction,
   `M2K_COMMAND_USER`) still applies; with a user drop, `bwrap` itself runs as that user.
   Fail closed: if the backend fails to start at run time the effect is FAILED `START_FAILED` (worker)
   or test UNKNOWN; never fall back to running unisolated.
5. `Kernel(..., sandbox=<backend>)`, default `none`, so existing tests and scripted runs are unchanged.
   `task.submitted` records `"sandbox": name, "network": bool`; `m2k status` shows both.
6. Offline scenarios in `m2k accept` (backend picked with `auto`; Linux CI installs bubblewrap):
   - `命令读取工作区外的文件`: a canary file next to the bench workspace; a command prints it.
   - `命令往工作区外写文件`: a command writes into the canary folder.
   - `命令联网外传`: a command opens `socket.create_connection(("1.1.1.1", 53), 3)`.
   Expected for all three: `拦下（Linux：隔离环境内做不到；无隔离后端：命令被拒绝执行）`, and the
   check is on the facts (canary text absent from the ledger, canary folder unchanged, connection failed
   or never attempted), not on the backend name.
7. Workflows: `ci.yml` (ubuntu) installs `bubblewrap` and, only if the runner's AppArmor blocks
   unprivileged user namespaces, relaxes that for the job. `m2k-task.yml` installs it, writes
   `[sandbox] backend = "bwrap"` into the generated config, and the pre-run proof step shows that a
   command run as `m2kw` inside the sandbox cannot connect out, cannot see `/root` or `/srv` outside the
   workspace, and cannot list other processes. `real-accept.yml` gets the same install.
8. Docs: `m2k.example.toml` gains the `[sandbox]` section with comments; `README.md` "当前边界" says what
   is isolated where; `docs/WORKFLOW.md` local DeepSeek runs: needs Linux, GitHub, or `backend = "none"`.

## Out of scope
- Windows / macOS / WSL / Docker backends; CPU and memory limits; seccomp filters.
- Per-command network grants (network is per task).

## Acceptance
```
python -m unittest tests.test_sandbox -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
Tests: argv shape for `bwrap` (network on/off, binds); `auto` with no backend refuses; `none` records
itself; on Linux with bwrap (skip otherwise): canary unreadable, write outside fails, `/root` and the
ledger folder invisible, connection fails, workspace writable, `{python}` works; backend failing at run
time → `START_FAILED`, command not run unisolated. CI green on Windows and Linux, offline acceptance all ✅.
By hand on GitHub: dispatch `m2k-task` once with a small throwaway contract and confirm the proof step.

## Review notes (written by reviewer)

**Round 1 (PR #12): PASS (Claude, 2026-10-13, after GLM-5.3 GO via the streamed review of T13), merged as #12.**
GLM review failed 3x before T13 (connection reset on a long review). By-hand `m2k-task` dispatch on the branch
(run 38403530799): bubblewrap install and the m2kw proof step OK. Windows 3.11 fix: no info pipe without os.set_blocking.
GLM follow-ups (non-blocking): guard a Python prefix of `/`; scalar `sandbox =` in config; `available()` on `none`.

## Handoff note (written by implementer, ≤ 10 lines)

- `m2k/sandbox.py` (none / unavailable / bwrap + `choose`); `run_shell` routes worker and acceptance commands through the
  backend; a bwrap that never reports on `--info-fd` gives START_FAILED, never an unisolated run. Unknown backend fails closed.
- `[sandbox] backend` in config (`auto` refuses with exit 2 when no backend); `[task] network` (default false);
  `task.submitted` and `m2k status` record sandbox + network. Accept: 3 isolation scenarios judged on facts (16/16).
- Workflows install bubblewrap (relaxing AppArmor userns only if the probe fails); m2k-task uses `bwrap` and proves
  as m2kw: no network, no /root keys, no /opt code, /srv shows only the workspace, host processes hidden.
- Verified here with a locally built bwrap 0.8.0: tests.test_sandbox 15/16 (network control skipped: no outbound
  net here), full suite OK, proof step OK as m2kw; removing --unshare-all / hiding /tmp / the info-fd check breaks tests.
- Could not run: real network control test (needs CI) and a full m2k-task dispatch (next, by hand on GitHub).
