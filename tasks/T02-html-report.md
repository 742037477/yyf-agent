# T02: `m2k report` — a readable HTML timeline of one task

- **Assignee:** Codex
- **Branch:** `task/T02-html-report`
- **Line budget:** ≤ 200 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/report.py`, `m2k/cli.py`, `tests/test_report.py`
- **Status:** DONE

## Goal
The project owner does not read code. He verifies the framework by looking at what an agent
actually did. Add `m2k report TASK_ID` that writes one self-contained HTML file showing a task's
ledger as a timeline he can read in a browser.

## Required behaviour
1. `m2k report TASK_ID [--out PATH]` writes `PATH` (default `.m2k/reports/<task_id>.html`) and
   prints the path. Read-only: it must not append anything to the ledger.
2. Header: task id, goal, final verdict (PASS green / FAIL red / UNKNOWN amber / none grey),
   worker status, usage numbers, whether the ledger hash chain verifies.
3. Body: one row per ledger event in order: time, kind, and a short human summary
   (e.g. `proposal` → "round 3: write_file calc.py — <thought>"; `gate.denied` → gate + reason;
   `effect.result` → status + first 300 chars of output; `verdict` → verdict + reasons).
   Denials, FAILED / UNKNOWN effects and non-PASS verdicts are visually highlighted.
4. A "raw payload" toggle per row (`<details>`), showing the JSON payload.
5. Labels are in Chinese where they are UI text (表头、判定、用量 …).
6. One file, inline CSS, no JavaScript libraries, no network requests. All ledger text must be
   HTML-escaped.
7. Unknown task id → exit code 1 with a clear message.

## Out of scope
- Multi-task dashboards, charts, live refresh, any web server.

## Acceptance
```
python -m unittest tests.test_report -v
python -m unittest discover -s tests -t .
python -m m2k demo --dir .m2k/demo && python -m m2k --ledger .m2k/demo/ledger.sqlite report <the demo task id printed above>
```
Tests must cover: report written for a PASS task and a FAIL task; ledger unchanged after
reporting (same event count); `<script>` text inside a payload appears escaped; unknown task id
→ exit 1.

## Review notes (written by reviewer)

**结论：PASS（Claude，2026-10-13）**
- 范围：只改了 `m2k/report.py`、`m2k/cli.py`、`tests/test_report.py` 和本卡；`m2k/` 新增 140 行（预算 200）。
- Linux / Python 3.13 复跑：`tests.test_report` 8/8，全量 66/66，`accept --offline` 7/7。
- 卡内场景：demo 后对其任务运行 `report`，前后都是 30 条事件，哈希链完好；未知任务退出 1；`../../x` 这类任务号不会写到报告目录外。
- 7 条要求逐条对上：表头四项、逐事件中文摘要、拒绝/FAILED/UNKNOWN/非 PASS 判定高亮、`<details>` 原始载荷、全部转义、无脚本无外链。
- 卡外记录（本 PR 不处理）：`--ledger` 指向不存在的路径时，`_ledger()` 会先建一个空账本文件再报“unknown task”，`status`/`log` 同样如此；时间列是 UTC，用户在 UTC+8，读起来要自己换算。两条以后需要时另开卡。

## Handoff note (written by implementer, ≤ 10 lines)

- 对照基线：main `12996d0`，修改前全部 58 项测试通过。
- 新增中文、自包含 HTML 时间线：状态/用量/哈希链、逐事件摘要、异常高亮及原始 JSON 折叠。
- 支持默认路径与 `--out`；账本文本全部转义，报告不追加事件，未知任务退出 1，禁止覆盖账本。
- 验证：`python -m unittest tests.test_report -v`（8 项）及全部测试（66 项）均通过。
- 卡内 demo → report 通过：`demo-20261013-111511`；前后事件数均为 30，状态/用量不变，哈希链通过。
- 仅修改卡允许的三个代码/测试文件及本交接说明；未发现需修改的卡外问题。
- 本地验证在 Windows；Linux 与其他 Python 版本留待 CI，最终验收及 PR 由审阅者处理。
