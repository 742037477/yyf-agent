# T04: key guard — commands cannot reach model API keys

- **Assignee:** Claude (core logic; GLM-5.3 GO is required because the author is also the final reviewer)
- **Branch:** `task/T04-key-guard`
- **Line budget:** ≤ 50 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/tools.py`, `tests/test_key_guard.py`, `tools/task_result.py`, `.github/workflows/m2k-task.yml`, `docs/WORKFLOW.md`, `tasks/README.md`
- **Status:** DONE

## Goal
Stage 1.5, first item. Before T03 lets a real model (DeepSeek) run commands on GitHub, make sure
nothing it runs can read the DeepSeek / GLM keys or other credentials: not from its environment,
not from the m2k process, not from a key file, and not by sneaking a key value into the ledger or
the model's context. Then add a workflow that runs one task card through m2k on GitHub this way.

## Required behaviour
1. `run_shell` (worker commands and the acceptance command) never passes on environment variables
   whose name looks secret (`KEY`, `TOKEN`, `SECRET`, `PASSW`, `CREDENTIAL`, `AUTH`, `COOKIE`, `PRIVATE`).
2. Any withheld value (≥ 8 chars) that still appears in command output is replaced by `[REDACTED]`
   before it reaches the ledger or the model.
3. `M2K_COMMAND_USER=<name>`: on POSIX, with m2k running as root, every command runs as that
   non-root user. Misconfiguration (not root, root target, unknown user, Windows) fails closed:
   `FAILED` with reason `START_FAILED`, the command does not run. The variable is not passed on.
4. Workflow `m2k-task`: keys only in a root-only file; m2k runs as root from a pristine code copy
   outside the workspace; commands run as `m2kw`; the ledger is outside the workspace; a pre-run
   step proves `m2kw` cannot read the keys or use sudo. On PASS, `tools/task_result.py` checks the
   ledger (chain intact, PASS bound to the frozen candidate), that only card-allowed files changed
   and still match the candidate, then the workflow pushes them to the card's branch.

## Out of scope
- OS sandbox / network isolation (stage 1.5, later card). Network stays open; there is no key to send.
- Redacting keys read from `api_key_file` (they never enter any command's reach in the workflow).

## Acceptance
```
python -m unittest tests.test_key_guard -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
Scenario (by hand, Linux root): a scripted worker running `env`, `cat /proc/$PPID/environ`, a scan of
every `/proc/*/environ`, `cat` of the root-only key file, `sudo -n` and `echo <key>` gets no key.

## Review notes (written by reviewer)

**结论：PASS（Claude，2026-10-13），用户授权合并（#3）**
- 范围：只改卡内文件；`m2k/` 新增 46 行（预算 50）。ci 4/4 绿、card-scope 绿、GLM-5.3 GO（作者即终审，本卡要求 GLM 必须 GO）。
- 偷 Key 场景本地复现全部拿不到；`tools/task_result.py` 三种情形（PASS 放行、卡外文件拒绝、判定后改动拒绝）均符合预期。
- 卡外记录（不在本卡处理）：生成 TOML 时 Key 未转义；Windows 分支缺单测；命令仍可联网（阶段 1.5 后续卡）。

## Handoff note (written by implementer, ≤ 10 lines)
- `m2k/tools.py` +46/−5: `command_env` (scrub by name), `redact`, `_as_user` (fail-closed user drop).
- Tests: 6 new (one skipped unless root, one skipped when root); full suite 72 OK on Linux 3.13,
  also run as an unprivileged user in a read-only root-owned copy (how the workflow runs it); `accept --offline` 7/7.
- Scenario run locally as root with `M2K_COMMAND_USER`: env → no key; parent `/proc/environ` → Permission denied;
  full `/proc` scan → 0 hits; key file → Permission denied; `sudo -n` → password required; `echo <key>` → `[REDACTED]`.
- Workflow not runnable before merge (it starts from main); first real use is T03.
