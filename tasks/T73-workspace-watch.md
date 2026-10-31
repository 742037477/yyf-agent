# T73: the framework watches what an external agent changes — live in its workspace, and in owner-chosen paths outside it

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T73-workspace-watch`
- **Line budget:** ≤ 170 added lines in `m2k/` in total (tests not counted)
- **Files you may change:** `m2k/external.py`, `m2k/kernel.py`, `m2k/cli.py`, `m2k/adjudicate.py`, `m2k/live.py`,
  `m2k.example.toml`, `README.md`, `tests/test_external_agent.py`, `tests/test_watch.py` (new),
  `tests/fixtures/fake_agent.py`, `tests/test_adjudicate.py`, `tests/test_live.py`, `tests/test_startup_refusals.py`,
  `tasks/README.md`
- **Status:** DONE (merged #59, 2e57db9)

## 背景：威胁覆盖表（2026-10-27，用户确认方向）
T77（账号 ACL 隔离）已暂停、留现状不拆：现阶段不做“事前阻止”，先靠框架已有的“事后发现”。下表说明在
**当前对手**——以 <owner-account> 身份运行、无 OS 隔离、可能被 prompt 注入或跑飞的**可信厂商 Agent**（Codex、Claude Code、dsh）——
面前，已有机制管到哪一步，以及本卡补哪一格。

| # | Agent 能做的事 | 现有机制 | 效果 | T73 之后 |
| --- | --- | --- | --- | --- |
| 1 | 谎称完成；改验收或受保护文件；在工作区留同名模块劫持验收 | 冻结候选、任务方验收、受保护文件哈希、shadow 检查（T06/T10）、跨厂商审查 | 阻止交付 | 不变 |
| 2 | 运行中改框架代码、配置、Root 公钥、授权书 | 框架指纹（T40）、发行清单（T43）、Root 钉住（T65） | 中断不交付；下次启动拒绝 | 不变 |
| 3 | 改写或伪造账本、回执、锚点 | 哈希链、OBS 回执与日志对账（T42/T44）、外部锚点（T63）、只读裁决器（T39） | 发现 | 不变 |
| 4 | 整套换 Root + OBS 密钥 + 锚点（T46 a21） | Root 指纹醒目显示、远端锚点公证方变更 | 部分发现；同用户下已接受的风险 | 不变 |
| 5 | **改工作区外的其他文件**：自己的配置（如 `~/.claude/settings.json` 钩子、`~/.codex/config.toml`、全局 `AGENTS.md`）留下持久化、改用户别的项目或启动项 | **无** | 发现不了 | **B 节：所有者指定的监视清单，事后发现、任务判 FAIL**；清单外仍发现不了 |
| 6 | **读机密并外传**：OBS 私钥、测试 Root 私钥、dsh 的 DeepSeek Key、各家登录令牌 | **无**（读不留痕，网络不受控） | 发现不了 | 不变。正式 Root 私钥离线（U 盘）已结构性排除最坏情况；其余后果限于 TEST_ONLY 信任域或 API 费用 |
| 7 | 运行过程是最长 30 分钟的黑盒，超时只能记 UNKNOWN | 只有结束后的冻结 | 看不到过程 | **A 节：运行中实时记录 `workspace.changed`**（`m2k live` 能看到；超时时也留下了过程事实） |

第 6 行是“只靠事后发现”的真实缺口：在对手是可信厂商 Agent 时接受；**到第 8 阶段交付给他人或运行不可信 Agent 之前，
必须重新评估**（届时做原 T77 的威胁模型调查：选最小够用的隔离原语，攻击模拟由 DeepSeek 经 m2k 执行）。

## Goal
Today an external agent (T37) is a black box: the ledger has `effect.intent`, then nothing until `effect.result`, and
the kernel only compares the workspace before and after. Anything the agent changes outside the workspace is not
looked at, unless it is framework code or configuration. After this card: (A) while an external agent runs, the
framework itself polls the workspace and records what changed, as plain observations; (B) the owner can list paths
outside the workspace in the config; the framework snapshots them before and after every worker run, and any change
fails the task, recorded as a witnessed fact the read-only adjudicator also enforces. Nothing here prevents
anything: it turns two blind spots into ledger facts.

## Required behaviour

### A. Live workspace observation (external agents only)
1. `[models.worker]` for `kind = "cli"` takes `watch_s` (number of seconds, default `5`; `0` turns observation
   off; anything else that is not a number ≥ 0 → the same kind of config error the other `[models.worker]` keys give).
2. While the agent process runs, every `watch_s` seconds `CliAgent.run` takes a cheap snapshot of the workspace
   (relative posix path → `(size, mtime_ns)`, files only, same file list rule as `Workspace.files()`), compares it with
   the previous one and, only when something differs, appends one
   `workspace.changed` event: `{"added": [...], "changed": [...], "removed": [...], "files": <count now>}`. Each list
   is sorted and holds at most 50 paths; when a list was cut, add `"more": {"added": n, ...}` with the counts left
   out. The first snapshot is taken just before the process starts; one last comparison runs after the process has
   ended (normally, failed or timed out) and **before** `effect.result`.
3. Reading the agent's output must never block on a full pipe and the timeout must keep its meaning: e.g. run
   `communicate()` in a helper thread and poll from the calling thread. **Only the calling thread writes to the
   ledger.** `probe()` does not observe. A snapshot that fails (a file vanished while being listed, permission
   error) is skipped silently; the next one compares against the last good one.
4. `workspace.changed` is an observation, not an effect and not evidence: it is appended with `ledger.append`
   (no OBS receipt), it gets no `op_id`, the adjudicator ignores it, and the frozen candidate stays the only
   authority on what the agent left.

### B. Watched paths outside the workspace (every worker kind)
5. New optional config table `[watch]` with `paths = [...]` (strings; `~` is expanded; a relative path is relative
   to the config file's folder, like other config paths). Absent or empty → behaviour exactly as today (no new
   events, no new `task.submitted` key). Because the config is covered by the signed release manifest, adding or
   changing `[watch]` means re-signing, like any config change.
6. At startup (`m2k run`, in `cli.py`, with the other config checks), refuse with exit 2, a Chinese message and a
   `startup.refused` event with code `CONFIG` when: `paths` is not a list of non-empty strings; a path does not
   exist; a path is, contains or is inside the ledger file, the OBS key or log folder, the anchor file or the anchor
   `repo_dir` (the framework writes those during a run); or the watched tree has more than 20 000 files in total.
   At submit (`Kernel.run`), a watched path that is, contains or is inside the task workspace (or a subtask
   workspace folder) is a `KernelError`, like the existing `deliver_to` overlap check.
7. `task.submitted` records the resolved list as `"watch": [<absolute paths>]` when it is non-empty, so the ledger
   says what was watched.
8. Snapshot = absolute path → sha256 of the content for files; directories are walked recursively; symlinks and
   junctions are **not followed** (record the link itself, e.g. `"link:" + os.readlink(...)`). Take one snapshot
   right before each worker run (`Kernel._work`, so the first run, each repair and each subtask attempt) and one
   right after it.
9. If the two differ, record with `self._record` (OBS-witnessed) an `outside.changed` event:
   `{"added": [...], "changed": [...], "removed": [...]}` (absolute paths, sorted, at most 50 per list plus `"more"`
   as in A.2). If the after-snapshot cannot be taken completely (permission error, more than 20 000 files), record
   `outside.changed` with `"unverified": [<the paths that could not be read, at most 50>]` instead.
10. In the candidate loop, an `outside.changed` for this task (from any of its worker runs) works like the existing
    write-scope check: reason `files changed outside the workspace in watched paths: [...]`, test not run,
    `test.result` FAIL with reason `OUTSIDE_CHANGED`; for the unverified case test UNKNOWN with reason
    `OUTSIDE_UNVERIFIED`. Precedence among the existing codes: after `SHADOWED_MODULE`, before `OUT_OF_SCOPE`.
    No repair is offered (the existing rule already stops repairs when `test.reason` is set).
11. `adjudicate()`: any `outside.changed` event of the task → not PASS, with a problem line naming the paths.
    This must hold even when the kernel's own `test.result` and `verdict` say PASS (a forged tail).

### C. Live view and docs
12. `m2k/live.py`: `workspace.changed` → station `worker`, action `edit` (or `files` when only `removed` is
    non-empty), caption like `框架观测到工作区变化：新增 2、修改 1、删除 0`; `outside.changed` → station `worker`,
    state `bad` (`unknown` for the unverified case), caption `工作区外的监视路径被改动：N 处` (or `…无法核验` for the unverified case). No change to
    `live.html`.
13. `m2k.example.toml`: a commented `[watch]` example and `# watch_s = 5   # kind = "cli" only` line under
    `[models.worker]` (the example has no cli worker block). `README.md`: a short section “框架观测外部 Agent（T73）”: what A and B record;
    pick narrow paths (an agent's config file, not its whole home folder — Codex and Claude Code write session logs
    there on every run, which would fail every task); watching does not stop anything, it only reveals; reads and
    network uploads are still not visible (row 6 of the table above).

## Out of scope
- Any prevention: ACLs, sandboxes, isolation accounts, firewall (T77 is paused).
- Watching reads, process creation or network traffic; OS audit logs.
- Polling the workspace of the native (model-driven) worker: its every file effect is already in the ledger.
- Changing `live.html`, the OBS witness set for other events, verdict rules other than items 10–11.
- Adding `[watch]` to the owner's three demo configs (after the merge, separately, with re-signing).

## Acceptance
```
python -m unittest tests.test_watch tests.test_external_agent tests.test_adjudicate tests.test_live tests.test_startup_refusals -v
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
`tests/test_watch.py` (use the existing fake agent; add modes to `tests/fixtures/fake_agent.py` as needed, e.g. one
that writes a file, sleeps a little longer than `watch_s`, writes another and deletes one; one that writes into a
watched temp folder outside the workspace):
- A: with `watch_s` small (e.g. 0.2), the slow mode yields ≥ 2 `workspace.changed` events, all between the
  `effect.intent` and `effect.result` of `x1`, whose lists add up to what the agent did; the last change made just
  before exit is recorded; `watch_s = 0` → none; a timed-out agent still gets its final comparison before
  `effect.result` (status UNKNOWN); the 50-path cut and `"more"`; `probe()` writes nothing; bad `watch_s` values
  are refused; the verdict and delivery of the `good` mode are unchanged.
- B: an agent that writes into a watched folder → `outside.changed` (with an OBS receipt when the case has OBS),
  `test.result` FAIL `OUTSIDE_CHANGED`, nothing delivered, no `repair.started` even with `max_repairs > 0`; the
  same task without `[watch]` → PASS and no `watch` key in `task.submitted`; an unchanged watched folder → PASS;
  a watched symlink whose target changes is not followed (create links only where the OS allows; otherwise
  `skipTest`); every refusal of item 6 (exit 2, `startup.refused` code `CONFIG`) and the workspace overlap of item 6;
  a native `ScriptedModel` worker whose command writes into a watched folder is caught too (use `backend = "none"`
  as the existing tests do).
- `tests/test_adjudicate.py`: a ledger whose kernel events say PASS but that contains an `outside.changed` → not
  PASS; a ledger with only `workspace.changed` extra events → unchanged result.
- `tests/test_live.py`: the two new kinds map as in item 12.

## Review notes (written by reviewer)

2026-10-27 Claude: items 10–11 cover the task and all its descendant tasks (every subtask and every reassigned epoch), via the parent links in task.submitted.

- 2026-10-27 Claude: PASS on 8d6e7c7. Codex review (gpt-6-astra) round 1 sent back one point: an `outside.changed` in a failed subtask attempt was ignored once the reassigned epoch passed, so the parent and the adjudicator delivered while the outside change remained; fixed with a shared `outside_changes()` over the `task.submitted` parent links, used by the kernel and the adjudicator, plus 4 regression tests. GLM-5.3: round 1 UNKNOWN (empty reply), round 2 GO. CI and card-scope green. `m2k/` +156 (≤ 170).
- Known limits (recorded, not fixed): watched paths are compared only before and after each worker run, so a change reverted before the agent exits, or written by a detached process after it exits, is not seen; reads and network uploads stay invisible (row 6 of the table). GLM non-blocking notes kept for a later small-fixes card: hash only regular files in `watch_snapshot` (a FIFO would block); a malformed `[obs]` surfaces as a `[watch]` config error; `outside_changes()` scans every task in the ledger on each call.

## Handoff note (written by implementer, ≤ 10 lines)

Implemented CLI workspace polling, bounded change lists, caller-thread observations, and final comparisons on exit/failure/timeout.
Added owner watch-path startup/submit checks and content snapshots around every native/CLI worker run, repair, and subtask attempt.
Outside changes are OBS-witnessed and block acceptance/repair/delivery and read-only adjudication across all descendant epochs; live mappings and docs updated.
Validation: focused acceptance 93 run, 91 passed, 2 skipped; full suite 853 run, 838 passed, 15 skipped (Python 3.13.11 on Windows).
Acceptance: offline scenarios 28/28; T46 CHECK OK (21 tests, 17 defence mutations, scenarios and embedded full suite passed).
Scope: 156 added lines under m2k/ versus origin/main; card_check and git diff --check passed.
Windows symlink creation lacks privilege (2 new skips); a real Windows junction was verified without following its target.
CI/card-scope on GitHub, GLM-5.3 GO, and final reviewer approval remain pending; the card stays OPEN. No PR opened.
Re-review regression: 7d98edf missed failed/reassigned and nested descendants; four new tests and existing scoped-ledger-read checks pass.
