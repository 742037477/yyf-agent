# T86: 审计层收尾——用完预算不丢已写的工作、审查失败重审一次并留原话、连接层错误按 retry_delays 重试

- **Assignee:** Codex（GLM-5.3 必审 + Codex 独立复审）
- **Branch:** `task/T86-audit-tail`
- **Line budget:** `m2k/` 下新增 ≤ 60 行（测试不计；Claude 估约 45 行）
- **Files you may change:** `m2k/kernel.py`、`m2k/adjudicate.py`、`m2k/models.py`、`m2k/worker.py`、`m2k/ledger.py`、
  `m2k/verify.py`、`tests/test_audit_tail.py`（新建）、`tests/test_busy_retry.py`、`tests/test_owner.py`、
  `tests/test_review_injection.py`、`tasks/T86-audit-layer-tail.md`、`tasks/README.md`
- **Status:** DONE (merged #64, cd42ddd)

## 背景（用户 2026-10-30 拍板：m2k 收尾、之后停止开发）
T85 定案为选项 2：m2k 回到“审计 / 可核验层”，不再做规模化。本卡只修 R0 / T82 实测暴露的三处毛病，不加新功能：
1. **T84**：③ 有 12 块代码已经写对，却因为轮数用完被判失败、丢弃。目前 `BUDGET_EXHAUSTED` 时候选虽然冻结、也跑了验收，
   但无论验收结果如何都不审查、不交付（顶层直接转“待授权”，子任务判 FAIL）。
2. **T82**：3 次误伤都是审查环节故障（GLM 回复不是单个 JSON、HTTP 500 重试后仍失败），一次失败就挡住了正确交付；账本里
   不留审查原话，事后无法诊断。
3. **T84 / T85**：`retry_delays` 只管 `ProviderBusy`；URLError、SSL EOF、RemoteDisconnected、IncompleteRead、超时会立刻变成
   `MODEL_ERROR`（T85 ③\* 出现 6 次）。

写卡时 Claude 已在 26e2fda 上做过原型实测（不提交）：第 1 条只让 `test_owner.py` 一处断言失败；第 2 条让
`test_review_injection.py` 里只给一次坏回复的 12 个子测试失败；第 3 条会改变 `test_busy_retry.py` 里 `"network"` 一例。
全套 980 个测试中其余全部照常通过。

## Goal
工人因预算（轮数、模型调用、token）用完而停下时，候选若在冻结后通过验收、独立审查也给 GO，就和正常 `finish` 一样判 PASS
并可交付；否则行为与现在完全相同。审查回复不是单个 JSON 对象或审查调用出错时，在预算允许的前提下用新 nonce 重审一次，
失败那次的原话（截断）写进账本。适配器把连接层错误和 busy 一样按 `retry_delays` 重发，并如实记下“丢失的那几次请求用量未知”。

## Required behaviour

### A. 预算用完的候选照常核验（`kernel.py`、`adjudicate.py`）
1. 条件（全部满足才算“可照常核验”）：任务没有 `subtasks`、没有 `plan`；`worker.status is BUDGET_EXHAUSTED`；没有信任链问题
   （`broken` 为空）；本轮 `test.verdict is PASS` 且 `test.reason` 为空；`model_budget_denial(contract, usage, "reviewer")` 为空
   （审查还有预算）。顶层任务与子任务都适用。
2. 满足时：照常调用 `_audit`，`verdict = combine(test.verdict, review.verdict)`；`reasons` 第一行写
   `worker stopped: BUDGET_EXHAUSTED (<原因>); candidate checked as usual`，后面照常是 test / review 行。不进修复轮（预算已用完）。
3. 顶层、无子任务、无规划的任务：框架的 `owner.request`（`kind: budget`、`by: framework`）**只在最终 verdict 不是 PASS 时**记录，
   记录内容、`awaiting`、`reasons` 里的 `awaiting owner: budget: …` 行与 verdict = UNKNOWN 都和现在一样，只是记录时机从冻结前
   挪到验收 / 审查之后。PASS 时不记 `owner.request`，照常进入交付闸。
4. 不满足第 1 条时（验收不过、审查没预算、有信任问题等）：结果与现在相同——不调审查，子任务 FAIL，顶层转待授权；唯一差别是第 3 条说的
   顶层 `owner.request` 记录位置（内容不变）。
5. 其他停止状态（`STALLED`、`MODEL_ERROR`、`TRUST_BROKEN`、`AWAITING_OWNER` 等）一律不变：不审查、不交付。
6. 裁决器：`worker.stopped` 的 status 为 `FINISHED` 或 `BUDGET_EXHAUSTED` 都可判 PASS（其余检查——冻结候选、验收、审查 GO、
   nonce、凭证、`owner.request` 不存在——全部照旧）。其他状态仍是 `the worker did not finish (...)`。
7. 原生 Worker 与外部 Agent 都适用（外部 Agent 的 `BUDGET_EXHAUSTED` 同样处理）。

### B. 审查失败自动重审一次，原话入账（`kernel.py` `_audit`，必要时 `verify.py`）
1. 可重审的失败只有两种：审查调用抛 `ModelError`（`reviewer error: …`，含 busy / 连接错误重试用尽）；回复不是单个 JSON 对象
   （`reviewer reply was not exactly one JSON object`）。**nonce 不符、`invalid decision`、预算不足（`review not run`）都不重审。**
   用 `AuditResult` 上的明确标记区分（例如 `verify.py` 里加 `retry: bool = False` 字段），不要靠比较原因字符串。
2. 重审前再查一次 `model_budget_denial(..., "reviewer")`；不允许就不重审，结果就是第一次的结果。重审调用 `audit()`，自然生成新 nonce。
3. 第一次失败时追加事件 `audit.retried`：`{candidate, decision: "NONE", reasons, nonce, reply}`，`reply` = 回复原文前 2000 字符
   （调用出错时为 `null`）。每次真实的审查调用照旧各记一条 `model.called`（role reviewer）。
4. 最终的 `audit.result`：决定为 `NONE` 且有回复时多一个字段 `reply`（原文前 2000 字符）；GO / NO_GO 时 payload 与现在完全相同。
5. 每个冻结候选最多 2 次审查调用；第二次仍失败 → UNKNOWN，原因用第二次的。“审查者看不到工人对话”、nonce 在审查前不进账本等
   现有保证不变。
6. 修复轮、子任务、`_plan_fits` 的预算预留逻辑不改（重审吃的是现有审查预算；没预算就不重审）。

### C. 连接层错误按 `retry_delays` 重试（`models.py`、`worker.py`、`kernel.py`、`ledger.py`）
1. `models.py` 新增 `class ConnectionLost(ModelError)`：请求可能已经发出、可能已计费。`_post` 里 `OSError`（含 `URLError`、
   `ssl.SSLError` / `SSLEOFError`、`TimeoutError`、`ConnectionResetError`）和 `http.client.HTTPException`（含 `IncompleteRead`、
   `RemoteDisconnected`）改抛 `ConnectionLost`，消息格式不变（`<类型名>: <错误>`）。**`ssl.SSLCertVerificationError` 仍抛普通
   `ModelError`、不重试**；`JSONDecodeError`、`UnicodeDecodeError`、HTTP 4xx（busy 码之外）、`finish_reason=length` 都不重试，照旧。
2. `complete()` 的重试循环同时接住 `ProviderBusy` 与 `ConnectionLost`，用同一组 `retry_delays`、同一个总次数；用尽时抛最后那次的
   同类异常，消息加 ` (gave up after N attempts)`；`retry_delays = ()` 时一次不重试、消息与 `__cause__` 与现在相同。
3. 用量：`ModelReply` 加 `lost_attempts: int = 0`（成功前有几次 `ConnectionLost`）。worker、planner、reviewer 三处记
   `model.called` 时，仅当 `lost_attempts > 0` 才多写 `"lost_attempts": n`（否则 payload 不变）。`Ledger.usage` 把它加进
   `tokens_unknown_calls`（这些请求的 token 未知，不当作 0），不加进 `rounds`、`model_calls`、`tokens`，现有预算闸不变。
4. busy 重试的语义（“没开始处理、没花 token”）不变，不计入 `lost_attempts`。

## 旧测试怎么改（只许改下面这几类断言，别的旧测试一个不改）
- `tests/test_busy_retry.py`：`test_other_failures_are_never_resent` 里的 `"network": OSError("reset")` 一例移出（改到新测试里证明会重发）；
  其余不重发的用例保留。
- `tests/test_owner.py`：`test_plain_waiting_paths_keep_the_request_in_memory_without_global_reads` 的 `framework` 子测试里，
  `assert_awaiting` 中 `report.audit is None` 这条只对该例放宽（框架那例写了正确代码、轮数用完，现在会去审查，审查模型没回复 → 仍待授权）。
  可以给 `assert_awaiting` 加参数，不要删别的断言。
- `tests/test_review_injection.py`：只给审查者一次坏回复的用例（`test_json_values_other_than_one_object_are_unknown`、
  `test_multiple_objects_or_fences_are_unknown`、`test_prose_with_valid_json_is_unknown`）改成给两次同样的坏回复，原因断言保持
  `reviewer reply was not exactly one JSON object`；可在其中一个用例加断言：审查调用正好 2 次、有一条 `audit.retried`。
- 如果实现后还有别的旧测试变红，先停下写进 Handoff note 问 Claude，不要自行改。

## 新测试（`tests/test_audit_tail.py`，只用 `ScriptedModel` 和 mock 的 `urlopen`）
A. ①写对代码后轮数用完（不 `finish`）、验收过、审查 GO → PASS、已交付、无 `owner.request`、裁决 PASS、`reasons[0]` 以
`worker stopped: BUDGET_EXHAUSTED` 开头；②同上但验收不过 → 与现在相同（审查 0 次、顶层待授权 UNKNOWN）；③验收过但
`max_model_calls` 已用完 → 审查 0 次、待授权；④验收过、审查 NO_GO → 待授权 UNKNOWN、不交付；⑤子任务轮数用完但代码对 →
子任务 PASS 并合并、父任务 PASS；⑥`STALLED` 且代码对 → 仍不审查、不交付；⑦外部 Agent 的 budget 模式（若现有测试夹具能写对代码）
走同一规则；⑧裁决器：手工把一份 PASS 账本里的 `worker.stopped` 换成 `STALLED` 仍判非 PASS。
B. ①先非 JSON、再 GO → PASS，审查调用 2 次、`audit.retried.reply` 是第一次原文（截断 2000）、两次 nonce 不同；②先抛
`ModelError`、再 GO → PASS，`audit.retried.reply` 为 null；③两次都非 JSON → UNKNOWN、`audit.result.reply` 有原文；④nonce 不符、
`invalid decision` → 只调 1 次；⑤第一次失败后审查预算不够 → 不重审；⑥GO / NO_GO 的 `audit.result` payload 键与现在相同。
C. ①`URLError`、`ssl.SSLEOFError`、`http.client.RemoteDisconnected`、`http.client.IncompleteRead`、`TimeoutError` 各后接成功 → 重发、
`lost_attempts == 1`、等待时长按 `retry_delays`；②混合 busy 与连接错误时总次数 = `len(retry_delays) + 1`，用尽时消息带
`gave up after`；③`retry_delays = ()` 时连接错误一次不重发、消息与现在相同；④`SSLCertVerificationError`、JSON 解析失败不重发；
⑤worker 记的 `model.called` 带 `lost_attempts`，`Ledger.usage()["tokens_unknown_calls"]` 增加、`model_calls` / `rounds` 不变。

## Out of scope
- T81、隔离、经验库、R1～R6 全部冻结；不改 `live.py` / `report.py` 的展示（新事件 `audit.retried` 不需要专门站点，但不能让它们报错）。
- 不改修复轮的触发条件、子任务改派、预算预留算法；不改 `external.py`。
- 文档与定位说明留给 T87。

## Acceptance
```
python -m unittest tests.test_audit_tail tests.test_busy_retry tests.test_owner tests.test_review_injection -v
python -m unittest discover -s tests -t .
```
复审人工核对：A-4、A-5 与 26e2fda 相比，除顶层 `owner.request` 的位置外事件种类与内容相同；B 只对两种失败重审；C 的 `lost_attempts` 不进预算闸。

## Review notes (written by reviewer)

- 2026-10-31 Claude：PASS，合并 #64（cd42ddd，审阅头 a381d81，与 Codex 复审的 7307a61 内容相同）。`m2k/` +53 / 60。
- 实现中两次澄清：①`tests/test_owner.py` framework 子测试放宽 `assert_awaiting` 三条断言（`reviewed` 参数，默认不变）；②审查调用出错、没有回复时不记 `model.called`（照旧），`tests/test_report.py` 未改。
- GLM-5.3 GO；非阻断：`audit.retried` 在预算复查前记录（与 B-3 字面一致）；裁决器只在 kernel 判 PASS 时接受 `BUDGET_EXHAUSTED`。
- Codex 独立复审（gpt-6-astra ultra）A/B/C 逐条通过；它报的阻断“HTTP 503/429 错误响应体读取中断时仍报 ModelError、不重试”判为非阻断遗留：原有行为，`tests/test_models.py::test_interrupted_http_error_body_is_also_a_model_error` 明确断言，不在卡 C-1 范围（Claude 在 407840a 试改后于 a381d81 撤回）。
- 遗留（非阻断）：上一条；`lost_attempts` 用真值判断而非 `> 0`（内置适配器不会产生负值）。

## Handoff note (written by implementer, ≤ 10 lines)

- 用户澄清：仅 owner 的 plain waiting/framework 子测试启用 reviewed 参数，核对 audit UNKNOWN、审查 2 次、audit.result NONE；其余断言不变。
- 旧测试 busy_retry：仅从 test_other_failures_are_never_resent 移出 network；其余不重试用例及断言保留。
- 旧测试 review_injection：test_json_values_other_than_one_object_are_unknown、test_multiple_objects_or_fences_are_unknown、test_prose_with_valid_json_is_unknown 各给两次坏回复，原因断言不变；首例增加审查 2 次、audit.retried 1 条断言。
- 实现：预算候选核验与交付；两类审查失败最多重审一次并留原话；连接重试及 worker/planner/reviewer 未知用量记录。
- 修复前轮 B-3 偏差：无回复的 ModelError 不记 model.called，错误原因保留 audit.retried/audit.result；tests/test_report.py 未改且原样通过。
- 对照 26e2fda 的八组 A-4/A-5 同条件场景，事件内容一致，仅预算 owner.request 时机后移。
- Python 3.13 验收：两条卡内命令均通过；定向 82 项，全套 1009 项、跳过 15 项。
- 范围检查通过，`m2k/` 新增 53/60 行；变更 Python 文件通过 3.11 语法解析；仅改 Files 允许文件。
- 未开 PR；后续 CI、GLM 及 Claude 审阅待执行；worktree 保留。
