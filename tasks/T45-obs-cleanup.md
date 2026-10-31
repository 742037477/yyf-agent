# T45: OBS follow-ups — cached ledger id (no whole-ledger reads per receipt), one verdict, narrower errors

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T45-obs-cleanup`
- **Line budget:** ≤ 80 added lines in `m2k/` (unpacking `obs.py` counts; keep it plain, not golfed)
- **Files you may change:** `m2k/ledger.py`, `m2k/kernel.py`, `m2k/adjudicate.py`, `m2k/obs.py`, `m2k/cli.py`, `tests/test_obs.py`, `tests/test_ledger.py`, `tasks/README.md`
- **Status:** DONE (#43)

## Goal
This card cleans up after T42, before T43. The T42 review carried four notes forward. A measurement on 2026-10-17
showed the first one is a real cost.

The measurement used one scripted task, the median of 3 runs, on a ledger pre-filled with unrelated events. The
ledger is shared by every task and only grows.

| events already in the ledger | run without OBS | run with OBS | adjudicate without OBS | adjudicate with OBS |
| --- | --- | --- | --- | --- |
| 0 | 0.07 s | 0.18 s | 0 ms | 2 ms |
| 10,000 | 0.10 s | 0.64 s | 54 ms | 287 ms |
| 50,000 | 0.32 s | 3.10 s | 271 ms | 1,601 ms |

The OBS cost grows with the size of the whole ledger. Each witnessed event and each checked receipt reads every
event (`ledger.events()[0].hash`) only to learn the ledger id. With subtasks, that repeats for every child.

After this card, OBS costs about the same on a large ledger as on an empty one.

## Required behaviour
1. **`Ledger.ledger_id`**: a read-only property, the hash of the first event (`seq` 1 by order) or `None` for an
   empty ledger. It is read with one `LIMIT 1` query and cached after the first non-empty read. The first event can
   never change, and `verify_chain` already guards the chain. Every `ledger.events()[0].hash` in `m2k/` uses it
   instead. `anchor.head` may keep its own read.
2. **Kernel `_record`** and **`adjudicate`** call `ledger.events()` only with a task id. No path in a witnessed run or
   in `adjudicate` reads the whole table, except `verify_chain` itself.
3. **One verdict.** When OBS has failed, `_finish` decides UNKNOWN (with `OBS_UNAVAILABLE: …`) *before* it records
   the verdict, so the task has exactly one `verdict` event. If witnessing that verdict event itself fails, the
   kernel records `obs.failed`. It must not write a second verdict, because the Gate already refuses: the adjudicator
   finds no receipt for it.
4. **Narrower errors in `cmd_run`.** Undo T42's widening of the outer `except` to `KeyError`/`TypeError`. Instead,
   wrap only the `[obs]` setup lines. A missing key, a missing `log`, or a wrong type becomes
   `配置错误：[obs] …`, exit 2, before the ledger is opened. A programming error elsewhere must surface as a
   traceback again.
5. **`obs.py` unpacked**: one statement per line, no `;` joins, and no behaviour change.

## Acceptance
`python -m unittest tests.test_obs tests.test_ledger -v` and the full suite. New tests:
- `ledger_id` returns `None` when the ledger is empty and the first event's hash after appends, stays the same, and
  reopening the file gives the same value.
- Wrap `Ledger.events` to count calls with `task_id=None`. A full OBS task, with one subtask from `tests/test_task_tree`
  helpers, followed by `adjudicate(..., obs_public_key=...)`, makes **zero** such calls.
- On a witness death before the verdict, the task has exactly one `verdict` event, UNKNOWN, with `OBS_UNAVAILABLE`.
  On a witness death *at* the verdict, it has one verdict, an `obs.failed`, and no delivery.
- A malformed `[obs]` section in `m2k run` prints `配置错误：[obs]` and exits 2.

The reviewer will re-run the benchmark in the Goal (scratch script, not committed). At 50,000 events, "run with OBS"
must be within 0.3 s of "run without OBS".

## Out of scope
- Faster `verify_chain` (verifying only from a checkpoint) — later, when the ledger size calls for it.
- Making OBS mandatory, reconciling against the OBS log, and anchoring it (T44); release manifests (T43).

## Review notes (written by reviewer)

**PASS (Claude, 2026-10-17), merged as #43.** CI and card-scope green; GLM-5.3 GO. Linux, Python 3.13: full suite 504 OK
(7 skipped). `m2k/` +73 lines. Only `anchor.py` and `verify_chain` still read the whole ledger.
Reviewer benchmark (median of 3; OBS start-up included): at 50,000 events, run 0.458 s without OBS and 0.655 s with OBS
(difference 0.197 s ≤ 0.3 s; on main before this card, 3.18 s). Adjudicate 446 / 434 ms; the remaining growth is
`verify_chain`, which is out of scope.
Carried to T44: (1) a witness death at the verdict, or any Gate refusal, still reports PASS and exits 0 with nothing
delivered; (2) an OBS failure while a subtask verdict is being witnessed still lets the subtask merge internally
(the parent ends UNKNOWN and nothing is delivered).

## Handoff note (written by implementer, ≤ 10 lines)

实现 Required behaviour 1–5；保留并验证原有测试起点；m2k/ 新增 73/80 行。
Python 3.13.11：定向 31 项、全套 504 项通过（跳过 9）；obs.py AST 与基线等价；CI/GLM/最终审阅待完成。
基准沿用 754d82f 的仓库外脚本及预灌账本，ScriptedModel、无信任链、含旁证启动，各 3 次中位数；50k run 差从 1.8704s 降至 0.1317s ≤ 0.3s。
卡外：子任务 verdict 签署时 OBS 失败仍可内部合并，父任务 UNKNOWN、不交付；已复现，未改。

| 预灌事件 | run 无 OBS (s) | run 有 OBS (s) | adjudicate 无 OBS (ms) | adjudicate 有 OBS (ms) |
| --- | --- | --- | --- | --- |
| 0 | 0.1148 | 0.2564 | 0.37 | 1.12 |
| 10,000 | 0.1658 | 0.3040 | 46.75 | 47.01 |
| 50,000 | 0.3568 | 0.4885 | 232.80 | 236.09 |
