# T72: Same-model review as an explicit, recorded opt-in (`allow_same_model = true`), reviewed in a fresh session

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T72-same-model-review` (start from the latest main)
- **Line budget:** ≤ 60 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/cli.py`, `m2k/kernel.py`, `m2k/report.py`, `m2k.example.toml`, `README.md`, `tests/test_author_reviewer.py`, `tests/test_trust_cli.py`, `tests/test_report.py`, `tasks/README.md`
- **Status:** DONE (merged #53, 8b4743e)

## Goal
T26 made `m2k run` refuse to start when the worker and the reviewer are the same model ("an author cannot review
itself"). Not everyone has access to two vendors' models, so the owner decided (2026-10-19) to **allow the same model
as reviewer, but only as an explicit opt-in** in the config:

```toml
[models.reviewer]
allow_same_model = true   # same-model review: a fresh session that never sees the worker's conversation
```

The config is covered by the Root-signed release manifest (T43), so an agent cannot switch this on by itself; turning
it on is an owner change that needs `trust sign-release`. Same-model review is weaker than cross-vendor review (the
same model shares the worker's blind spots), so every such run is **recorded and shown**, never silent. It is allowed
in **both** trust domains (owner decision).

## Required behaviour
1. **Startup** (`cmd_run`, the T26 check). Without the flag, or with `allow_same_model = false`, behaviour is exactly
   T26's (the same refusal text, exit 2, nothing submitted). With `allow_same_model = true` and the same model (same
   rule as T26: equal `model` strings case-insensitively, or equal worker/reviewer names), the run starts and prints
   one yellow line to stderr before the run:
   `注意：Worker 和审查者是同一个模型（<name>），按配置 allow_same_model 以新会话独立审查（同源审查，已记入账本）。`
   A non-boolean `allow_same_model` is a configuration error (clear message, exit 2, no traceback). With different
   models the flag changes nothing and prints nothing.
2. **Recorded in the ledger** (`kernel.py`). The `task.submitted` payload gains `"review": "same-model"` or
   `"review": "cross-model"` for every task (top-level and subtasks), so the adjudicator, reports and `m2k live` can
   show it. Do not change any verdict, Gate or adjudication rule because of it.
3. **Fresh session.** The reviewer must not see the worker's conversation — this is already a design rule (AGENTS.md);
   add a test that, in same-model mode, the reviewer's request messages contain none of the worker's messages
   (use `ScriptedModel` and inspect what the reviewer received).
4. **Visible.** `m2k status` / `m2k report` show the review mode (`同源审查` or `跨模型审查`); keep it to one line or
   one field.
5. **Docs.** `m2k.example.toml`: the commented line above under `[models.reviewer]`, with a one-line warning that
   cross-vendor review is stronger. `README.md`: two or three sentences on when to use it and that it is recorded.

## Out of scope
- Any change to how the reviewer is called, to review prompts, or to the GO/NO_GO rules.
- External CLI agents as reviewers; changing any demo config or manifest.

## Acceptance
```
python -m unittest tests.test_author_reviewer tests.test_report -v
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
New tests show: same model without the flag → refused exactly as before; with the flag → starts, prints the yellow
line, `task.submitted` has `"review": "same-model"`, the run can PASS; different models → `"cross-model"` and no
warning; a string value for the flag → configuration error exit 2; the reviewer never receives the worker's messages;
status/report show the mode. Existing T26 and T46 tests pass unchanged.

## After the merge (reviewer, not this card)
- No demo config uses the flag, so no manifests are re-signed.
- The animation page (`m2k live` later) shows lamp 10 "审查独立" in yellow with "同源审查" when `review` is `same-model`.

## Review notes (written by reviewer)

- 2026-10-19 Claude: PASS on 3e80df7. Codex independent review PASS (no blocking or non-blocking points), GLM-5.3 GO, CI and card-scope green. Checked `m2k/cli.py` (T26 refusal unchanged without the flag; non-boolean flag is a config error, exit 2; `review_mode` passed to the kernel) and `m2k/kernel.py` (`task.submitted.review` for parent and subtasks; no verdict, Gate or adjudication change). `m2k/` +21 lines.
- GLM non-blocking notes kept for a later cleanup card: the flag type is checked only in `run` (not `ping`/`accept`); the same-model rule exists in the CLI and as a kernel fallback; the yellow line always carries ANSI codes.
- The non-boolean check runs before the contract is loaded, so it cannot be recorded with a task id (T71 keeps it stderr-only).

## Handoff note (written by implementer, ≤ 10 lines)

- 基于 origin/main `31463bd` 从头重做，未复用已取消实现。
- 实现显式布尔开关、原样默认拒绝和启动黄字；不改审查提示词、Gate 或判定规则。
- 父子任务记账 review；status/report 显示中文模式，旧任务保持“未记录”。
- README/示例补充说明；ping/联网 accept 过滤开关，避免模型构造参数错误。
- 新增 17 项测试，原测试用例正文不变；覆盖会话隔离、两信任域和重签保护（生产远端锚点传输模拟）。
- Python 3.13：定向 28/28、全套 708（697 通过、11 跳过）、离线 28/28、T46 CHECK OK。
- 首轮离线发现空 Worker 延后赋值的兼容回归；main 对照正常，改为提交时推导模式后通过。
- m2k/ 新增 21 行，无卡外修改；Status 保持 OPEN，待 CI、GLM GO 和最终审阅；未开 PR。
