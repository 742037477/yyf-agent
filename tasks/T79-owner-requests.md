# T79: owner requests — an agent can ask, a run can wait for the owner, and only the owner can approve more scope or budget

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T79-owner-requests`
- **Line budget:** ≤ 230 added lines in `m2k/` in total (tests not counted)
- **Files you may change:** `m2k/tools.py`, `m2k/worker.py`, `m2k/external.py`, `m2k/types.py`, `m2k/kernel.py`,
  `m2k/adjudicate.py`, `m2k/cli.py`, `README.md`, `tests/test_owner.py` (new), `tests/helpers.py`, `tests/test_amend.py`,
  `tests/test_reuse.py`, `tests/test_adjudicate.py`, `tests/test_pipeline.py`, `tests/test_repair.py`,
  `tests/test_external_agent.py`, `tests/test_recall.py`, `tasks/README.md`
- **Status:** DONE (merged #62, 74e8ba4)

## 背景（用户 2026-10-19、2026-10-28 拍板）
第 3.5 阶段第三张（原 T49 拆出的第二部分）。现在框架没有“提问—等人回答”的通道：原生 Worker 回文字会记 `proposal.invalid`，外部 Agent
无人值守，预算用完直接判 FAIL。本卡加上这条通道，守住“设计记录：Agent 提问 / 人工复核”的五条中的前四条（第五条“动画举手气泡与等人工灯”
由 Claude 另做，不在本卡）：
1. 问题按提议记账（`owner.request`），任务挂起，不占预算；
2. 回答必须能证明来自所有者本人——**隔离前**用专用命令 `m2k answer`，只能在交互终端里运行、所有者亲手输入 `YES`
   （挡得住跑偏的 Agent，挡不住专门攻击框架的 Agent，与本机验证范围一致；隔离后改为只有所有者能写的 inbox，不在本卡）；
3. 回答只澄清需求，不扩大权限：扩大范围、加工具、联网、追加预算都要在新契约里写明，并且同样必须经 `m2k answer` 批准；
4. 回答算契约新版本（T48），验收和审查按新版本判，审查者能看到回答。

用户 2026-10-28 的另外两条决定：**顶层任务预算用完不再判 FAIL，改为“待授权”**（判定 UNKNOWN，退出码 3），子任务预算用完照旧；
预算追加不归 Root，由所有者批准、框架签发并记 `budget.raised`（隔离前追加后的有效预算仍须在授权书 caps 内——现有凭证签发已经保证）。
自动预算规则是下一张 T80，本卡只留好扩展点（`approval.by` 字段），不实现规则。

## Goal
A worker that cannot go on without the owner — a question about the goal, more files / tools / network than it is
allowed, or more budget — can say so; so can the framework when a top-level task runs out of budget. The run then
ends as “awaiting the owner” (not PASS, not FAIL, nothing delivered), with the request in the ledger. The owner
reads it, writes the next contract version, and runs `m2k answer` in a terminal; after typing YES the new version
runs (T48, with T49's reuse) carrying the owner's answer. An amendment that widens scope or raises the budget is
refused unless the owner approved it this way, and the read-only adjudicator checks that every such amendment
carries an approval that matches the ledger.

## Required behaviour

### A. The worker asks (`owner.request`)
1. Native worker: a new built-in step `ask_owner` (listed by `tools.describe` like `finish`, never in
   `contract.tools`, never executed, never gated). Args: `kind` one of `question`, `scope`, `budget`; `text`
   (non-empty string, at most 2000 characters); optional `request` (object: for `scope` any of `writes` (list of
   paths), `tools` (list), `network` (bool); for `budget` a `budget` object with `max_*` integer fields). Bad args →
   the same `INVALID REPLY` feedback path as an unparseable reply (recorded as `proposal.invalid`, counts toward
   stalling). Valid → append `owner.request` = `{"kind", "text", "request", "by": "worker"}` and stop the worker with
   a new `WorkerStatus.AWAITING_OWNER` (reason names the kind). A request is data: the framework never applies it.
2. External agent (`CliAgent`): the prompt gets one more rule: if it cannot continue without the owner, write
   `.m2k/ask.json` (`{"kind", "text", "request"}`, same rules as item 1) and stop. Before launching the agent the
   framework deletes any existing `.m2k/ask.json` in the workspace (`.m2k/` is already outside every candidate,
   `tools.SKIP_DIRS`). After a run that exited 0, if the file exists: valid (≤ 20 000 bytes, same rules) →
   `owner.request` with `"by": "worker"` and stop `AWAITING_OWNER`; invalid → stop `STALLED` with the reason
   `invalid .m2k/ask.json: …`. Non-zero exit / timeout / token limit keep today's statuses (the file is ignored).
3. A subtask that asks: the child stops `AWAITING_OWNER` as above; `_run_subtasks` then stops the parent at once with
   `AWAITING_OWNER` (no reassignment, later subtasks do not run).

### B. The framework asks: top-level budget exhaustion
4. When the worker of a **top-level task without subtasks or plan** stops `BUDGET_EXHAUSTED` (first run or a repair
   run), the kernel appends `owner.request` = `{"kind": "budget", "text": <worker reason>, "request": null,
   "by": "framework"}` on that task. The worker status stays `BUDGET_EXHAUSTED`. Subtasks, planners, reservations and
   parents of subtasks behave exactly as today.

### C. Awaiting the owner
5. A task is *awaiting the owner* when it, or any descendant (`adjudicate.descendants`), has an `owner.request`.
   Such a top-level run still freezes its candidate and records `test.result` as today, gets no review, records
   verdict **UNKNOWN** with first reason `awaiting owner: <kind>: <text, ≤ 300 chars>`, except that a repair task
   keeps `repair attempts: N` first and the awaiting reason immediately after it. It is never delivered, and its
   repairs stop. Asking uses no budget beyond the model call that produced the request.
6. `RunReport` gets `awaiting` (the last request's payload plus its ledger `seq`, or `None`). `m2k run` prints
   `⏸ 待授权（<kind>）：<text>` and the next step `m2k answer <task_id> <新契约.toml>`, and exits **3**.
   `m2k status` shows `awaiting` (each request: seq, task, kind, text) and, when the task already has a successor,
   `answered_by: <successor task id>`.

### D. What needs the owner's approval (one shared function)
7. Write one function in `m2k/adjudicate.py` (next to `amendment_basis`), used by the CLI (to show), the kernel (to
   enforce) and the adjudicator (to recompute): given the ledger, the amended task id and the new **owner** contract
   data, return `needs` = `{"requests": [seq, …], "scope": {...}, "budget": {...}}`:
   - `requests`: the `seq` of every `owner.request` of the amended task and its descendants, ascending;
   - `scope`: top-level fields only — `tools` gains a tool → `{"tools": [old, new]}`; `network` false → true;
     `writes` wider (old non-empty and the new one is empty or has a path not covered by an old path) — each as
     `[old, new]`;
   - `budget`: each field whose new **owner** budget is above the previous version's owner budget (the previous
     version's `amends.owner_budget` when it has one, else its contract budget; a number → `None` counts as a raise,
     `None` → a number does not) as `[old, new]`.
   An amendment needs approval when any of the three is non-empty.
8. Kernel: `Kernel.run(contract, amends=..., approval=None)`. `approval` = `{"by": "m2k answer", "text": <str or None>,
   "needs": <what the owner was shown>}`. Refuse with `KernelError`, before anything is written for the new task,
   when the amendment needs approval and `approval` is `None` (message names what needs approval and says to run
   `m2k answer` in a terminal), or when `approval["needs"]` differs from the recomputed `needs` (“the situation
   changed since you confirmed; run m2k answer again”). **Concurrency:** the recomputation and comparison run inside
   the same `Ledger.append(guard=...)` transaction as T48's successor check (extend that guard), so nothing can slip
   in between the check and the `task.submitted`.
9. Record `task.submitted.amends.approval` = `{"by", "text", "needs"}` when an approval was given. When
   `needs.budget` is non-empty, right after `task.submitted` append `budget.raised` = `{"by": "owner", "task": <new id>,
   "amends": <old id>, "fields": needs.budget}` through `Kernel._record` (it gets an OBS receipt). The effective
   budget is computed exactly as T48 does; the existing credential checks keep it within the charter caps.

### E. `m2k answer <task_id> <contract.toml> [--text "…"]`
10. Refuse (exit 2, nothing written) unless `sys.stdin.isatty()` is true: “m2k answer 只能由所有者在交互终端里运行”.
11. Load the config and contract exactly as `m2k run --amends` does. When `--text` is given (non-empty, ≤ 2000
    characters), append the constraint `Owner's answer: <text>` to the new contract **before** anything else (so it
    is part of the version, its digest and credential, and the reviewer sees it with the other constraints). This
    changes the parent's constraints, so T49 reruns every subtask — intended (an answer can change what any part means).
12. Compute `needs` with item 7 and print, in Chinese: the amended task and the new version number; every pending
    request (seq, task, kind, text, request); every scope and budget change as old → new; the answer text. Then ask
    `输入 YES 确认（其他任何输入取消）：`. Anything but `YES` → print “已取消”, exit 1, nothing written.
13. On `YES`, run the new version exactly as `m2k run --amends <task_id> <contract>` would (same startup checks,
    printing and exit codes), passing `approval` with the `needs` that were shown.
14. `m2k run --amends` itself is unchanged except that the kernel now refuses amendments that need approval
    (item 8); the refusal is the usual non-zero exit without a traceback.

### F. Read-only adjudicator
15. For an amendment: recompute `needs` from the ledger with the recorded owner contract (the recorded `"contract"`
    with `budget` replaced by `amends.owner_budget`; `needs` never looks at constraints). Not PASS when `needs` is
    non-empty and `amends.approval` is missing, its `by` is not `m2k answer`, or its `needs` differs; or when
    `needs.budget` is non-empty and there is not exactly one `budget.raised` on the task whose `fields` equal it (and,
    with an OBS key, whose OBS receipt is valid); or when a `budget.raised` exists without such a need.

### G. Docs
16. `README.md`: a section “挂起与所有者批准（T79）”: how a worker asks (native `ask_owner`, external `.m2k/ask.json`);
    top-level budget exhaustion now waits for the owner (exit 3); how to answer (`m2k answer`, only in a terminal,
    type YES; `--text`); which changes need it (scope, tools, network, budget) and that plain `--amends` refuses
    them; the limit (an agent running as the same Windows user could still forge this; isolation later).

## Out of scope
- Automatic budget rules and `m2k owner-rule` (T80). An inbox only the owner can write (after isolation).
- Changing a contract while it runs, resuming the same task id, keeping the worker's conversation (stage 4).
- Removing budget caps from the charter (after isolation). `m2k live` / `m2k report` (Claude does the animation).
- Any change to trust, OBS, anchor, release, delivery or sandbox code; T48/T49 non-blocking notes.

## Acceptance
```
python -m unittest tests.test_owner tests.test_amend tests.test_reuse tests.test_adjudicate -v
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
`tests/test_owner.py` (scripted models; `tests.test_external_agent`'s fake agent for the external path):
- native `ask_owner` question → `owner.request` by worker, worker `AWAITING_OWNER`, verdict UNKNOWN with the
  `awaiting owner` reason, no review, nothing delivered, `RunReport.awaiting` set; each bad-args case → `INVALID REPLY`.
- external agent writes a valid `.m2k/ask.json` → same; invalid file → `STALLED`; a stale file from the seed is
  deleted before the run and does not count; non-zero exit with the file → today's status.
- subtask asks → parent stops `AWAITING_OWNER` at once, later subtask not run, no reassignment.
- top-level budget exhaustion (first run and inside a repair) → `owner.request` by framework, verdict UNKNOWN,
  worker status still `BUDGET_EXHAUSTED`; a subtask's budget exhaustion is unchanged.
- `needs`: each scope widening, each budget raise (incl. number → `None`), narrowing that needs nothing, requests
  collected from descendants.
- kernel: an amendment that needs approval and has none → refused, nothing written; mismatched `approval.needs` →
  refused; matching approval → runs, records `amends.approval`, one `budget.raised` with an OBS receipt when the
  budget was raised; the answer constraint reaches the worker and the reviewer prompt.
- concurrency: two `answer` runs on one awaiting task at once (copy T48's barrier test) → one wins, the loser wrote
  nothing (no `task.submitted`, no `budget.raised`); an `owner.request` appended to a descendant between the CLI's
  display and the kernel's submit → refused as “situation changed”.
- CLI: `m2k answer` with stdin not a TTY → exit 2, nothing written; TTY + `YES` (patch `isatty` and `input`) → runs
  end to end; TTY + anything else → exit 1 nothing written; `m2k run` exits 3 when awaiting and prints the next step;
  `m2k status` shows `awaiting` and `answered_by`; plain `m2k run --amends` raising the budget → refused, no traceback.
- `tests/test_adjudicate.py`: an approval-needing amendment without approval, with forged `needs`, with
  `by` ≠ `m2k answer`, a missing or extra `budget.raised`, a `budget.raised` whose fields differ → not PASS; an honest
  approved amendment → PASS.
- Existing tests — only these assertion changes are allowed: `tests/test_pipeline.py`
  `test_round_budget_stops_worker_and_skips_review` and `tests/test_repair.py` `test_repair_stops_at_remaining_budget`
  (limit 4): verdict FAIL → UNKNOWN (awaiting the owner); `tests/test_amend.py` / `tests/test_reuse.py` tests whose
  amendment raises the budget or widens scope now pass an `approval` (or assert the refusal). Do not weaken anything else.

## Review notes (written by reviewer)

- 2026-10-28 Claude: clarified during implementation: detecting "awaiting the owner" must not read the whole ledger on plain runs, plain adjudication or check-delivery (worker status and the request the kernel just wrote are enough; descendant scans only for amendments, status and answer); for repaired tasks "repair attempts: N" stays the first reason and the awaiting reason follows it.
- 2026-10-28 Claude: round 1 sent back three points: approval was checked only after the new workspace was prepared; refused amendments still left startup events; a deeply nested .m2k/ask.json raised RecursionError. Fixed: approval and needs are checked before the workspace and before any startup record; nested files count as invalid.
- 2026-10-28 Claude: PASS on 6b684a2. Codex review (gpt-6-astra) round 1 sent back three points (approval checked only after the new workspace was prepared, so the owner could not then answer with the same contract; refused amendments still left startup events in the ledger; a deeply nested `.m2k/ask.json` raised RecursionError); all fixed with regression tests and spot-checked by Claude (single-point rework rule, no second full review). GLM-5.3 GO twice. CI and card-scope green. `m2k/` +197/230.
- Non-blocking notes kept for a later small-fixes card: the adjudicator reports an awaiting task as FAIL while the kernel says UNKNOWN (neither delivers); a `.m2k/ask.json` that is a directory gives a raw OS error; `request: null` is invalid while an omitted `request` is accepted; a `question` may carry any `request` object; a planner that calls `ask_owner` has no special awaiting handling; pre-T79 amendments that raised the budget now adjudicate not PASS (no approval recorded); `status()` scans every `task.submitted` for `answered_by`; the contract is JSON round-tripped on every run.

## Handoff note (written by implementer, ≤ 10 lines)

- 在指定 worktree 实现请求校验、原生/外部提问、子任务传播、顶层预算挂起和交互 answer；未改主工作区。
- needs 与后继检查同处 Ledger.append 的写事务；并发落败方无事件；预算追加经 _record 留 OBS 回执。
- 可信基线 origin/main 0651e73：相关旧测试 125/125；最终定向验收（-v）96/96，离线场景 28/28。
- T46_check.py：T46 CHECK OK，攻击、防御开关、离线场景和内含全套回归均通过。
- 独立全套验收 939 项：924 通过、15 跳过、无失败；Python 使用指定的 3.13 解释器。
- 首轮三个失败记录见先前提交；现均已在卡内修复，无卡外遗留；未改 test_delivery.py、test_obs.py 或修复首因断言。
- m2k/ 新增 177/230 行；card_check 和 diff --check 通过，仅改允许文件与本卡。
- 保留 worktree，未开 PR；CI、GLM-5.3 GO 和最终审阅仍待执行，未自判完成。
- 本次修正：请求 payload/seq 随 Worker 结果在内存上抛，框架使用 append 返回值，普通裁决只读当前任务；修复次数保持首因，待授权原因紧随其后；C.5 与指定 Review notes 已同步。
- 第1轮返工（对照 94d3be5）：仅修审批前移与嵌套 ask 文件3项阻断，新增6项回归；四条验收均通过（102/102、28/28、T46 CHECK OK、全套945项/15跳过），m2k/累计新增197/230行；保留 worktree，待复审。
