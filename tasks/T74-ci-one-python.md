# T74: CI runs one Python per event (Linux 3.13 on PRs, Windows 3.13 on main) plus a Python 3.11 syntax check

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T74-ci-one-python` (start from the latest main)
- **Line budget:** ≤ 0 added lines in `m2k/` (this card changes only the CI workflow)
- **Files you may change:** `.github/workflows/ci.yml`, `tasks/README.md`
- **Status:** DONE (merged #55, f0575e2)

## Goal
The repository is private, so every GitHub Actions minute counts against the owner's monthly allowance and a $10
overage limit (owner, 2026-10-19). Today the `ci` workflow runs two full test jobs on every PR push (Linux Python
3.11 and 3.13) and three after every merge that touches code (Linux 3.11, Linux 3.13, Windows 3.13; Windows minutes
cost more). The owner works on Windows with Python 3.13. Owner decision (2026-10-19): run **one** full test job per
event, and keep the promise in `pyproject.toml` / `AGENTS.md` (Python ≥ 3.11) with a cheap syntax check instead of a
second full run.

## Required behaviour (`.github/workflows/ci.yml` only)
1. **Pull requests:** exactly one `test` job, on `ubuntu-latest` with Python 3.13, with every step it has today
   (cryptography install, bubblewrap install and probe, unit tests, offline acceptance, failure annotations). Linux
   stays because the bubblewrap command-isolation tests only really run there.
2. **Push to main** (same `paths` filter as today): exactly one `test` job, on `windows-latest` with Python 3.13, with
   the same steps (the Linux-only steps stay skipped on Windows, as today).
3. **Python 3.11 syntax check** inside that same single job (no extra job, so no extra billed minute): before the
   main `setup-python` step, add a `setup-python` step with `python-version: "3.11"` (give it an `id`) and a step that
   runs `<that interpreter> -m compileall -q m2k tests tools` (use the step's `python-path` output; `shell: bash`).
   A syntax error that only 3.11 rejects (for example a 3.12-style f-string that reuses its own quote) must fail
   the job. Then the existing `setup-python` step with 3.13 runs, so `python` in later steps is 3.13.
4. Keep the check-run name format `test (<os>, <python>)` (e.g. `test (ubuntu-latest, 3.13)`): reviewers and
   scripts read it. A one-entry `matrix` with `include` chosen by `github.event_name` is fine.
5. `card-scope` is unchanged. Update the header comment of the file to say what runs when and why (owner decision
   2026-10-19, Actions minutes).
6. `tasks/README.md`: add the T74 row.

## Out of scope
- Any change to `m2k/`, tests, other workflows (`glm-review`, `m2k-task`, `main-guard`, `model-ping`, `real-accept`),
  `pyproject.toml` or `AGENTS.md`.

## Acceptance
```
python -m unittest discover -s tests -t .
```
(nothing in `m2k/` or `tests/` changes; this only confirms the branch is healthy). Plus, on the PR itself:
- the PR's checks show exactly one `test (ubuntu-latest, 3.13)` job and `card-scope`, both green, and no 3.11 test job;
- the job log shows the 3.11 `compileall` step ran and passed;
- before pushing, prove the 3.11 check bites: locally (or in a throwaway commit you do not push) put
  `x = f"{"a"}"` in a scratch file under `tools/`, run the 3.11 `compileall` command, see it fail, and remove the
  file. Say in the handoff note how you checked this.
The Windows-on-main job is checked after the merge by the reviewer (first push to main that touches code).

## After the merge (reviewer, not this card)
- Confirm the next code merge to main runs exactly one `test (windows-latest, 3.13)` job.
- Update the handoff: check-run names are now `test (ubuntu-latest, 3.13)` and `card-scope` on PRs.

## Review notes (written by reviewer)

- GLM-5.3：GO（非阻断：矩阵表达式把所有非 pull_request 事件映射到 windows-latest，以后给 ci.yml 加 workflow_dispatch 等触发器时要同步改；compileall 在 runner 上写 __pycache__，无害）。
- Codex 独立复审：PASS。741 项通过；PR 上只有 `test (ubuntu-latest, 3.13)` 与 `card-scope` 且均绿，无 3.11 测试 job、无 Windows job；日志确认 3.11.17 解释器执行 `compileall -q m2k tests tools` 并通过；Windows 上 bash 引用含反斜杠的 python-path 经 Git Bash 验证可用。
- Claude 抽查 ci.yml 矩阵（第 18 行）、解释器引用（第 29 行）、Handoff 中的 3.11 反例验证：PASS，2026-10-19 合并。
- 待办（合并后）：下一次改代码合并到 main 时确认只跑了一个 `test (windows-latest, 3.13)`。

## Handoff note (written by implementer, ≤ 10 lines)

- 基于 `54a2199`：单项矩阵按事件选 PR Ubuntu 3.13 / main Windows 3.13，保留 check-run 命名、paths、原步骤与 card-scope。
- 同一 test job 先 setup 3.11，以其 python-path 经 bash 执行 compileall，再 setup 3.13；README 已加 T74。
- 临时 conda Python 3.11.17 执行 `-m compileall -q m2k tests tools`：干净树退出 0；tools/t74_syntax_probe.py 加入卡中 f-string 后退出 1、报 SyntaxError（3.13 可解析）。
- 删除探针后同一 compileall 再次退出 0；临时 py311 环境已删除，base 保持 Python 3.13.11。
- 指定 Python 运行 `-m unittest discover -s tests -t .`：741 项，OK（skipped=11）；YAML 解析及原触发器、步骤、card-scope 保留核对通过。
- 未开 PR，Status 保持 OPEN；PR 检查与 3.11 步骤日志、合并后 Windows job 待审阅者验证。
