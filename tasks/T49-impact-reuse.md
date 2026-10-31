# T49: impact analysis — an amended version reruns only the subtasks the change affects and reuses the rest

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T49-impact-reuse`
- **Line budget:** ≤ 160 added lines in `m2k/` in total (tests not counted; 第 1 轮返工按用户授权提高，原因见交接说明)
- **Files you may change:** `m2k/kernel.py`, `m2k/adjudicate.py`, `m2k/cli.py`, `README.md`, `tests/test_reuse.py` (new),
  `tests/test_amend.py`, `tests/test_adjudicate.py`, `tests/helpers.py`, `tests/test_recall.py`, `tasks/README.md`
- **Status:** DONE (merged #61, 66d6dcb)

## 背景（用户 2026-10-28 拍板）
第 3.5 阶段第二张。T48 让所有者在两次运行之间用 `m2k run --amends` 开新版本，但**保守**：旧版本的一切证据一律作废、子任务全部重跑。
第 3.5 阶段的完成标准是“改验收版本后旧候选失效，无关子任务结果保留”。本卡只做这一件事：**影响分析与子任务结果复用**。

原 T49 的其余部分按用户 2026-10-28 的决定拆成两张，**本卡不做**（写在这里，免得实现时越界）：
- **T79 挂起 + 提问 + 所有者批准**：Agent 的 `ask_owner` 动作（外部 Agent 写 `.m2k/ask.json`）记 `owner.request`，任务以“待授权”结束
  （判定 UNKNOWN、退出码 3）；**顶层任务预算用完也改为“待授权”**（子任务照旧）；专用命令 `m2k answer` 只能在交互终端里运行、
  所有者亲手输入 YES 才记 `owner.approved` 并开新版本；从此在 `--amends` 里扩大范围、加工具、联网、加预算都必须经过它；追加记 `budget.raised`。
- **T80 自动预算规则**：`m2k owner-rule add/list/revoke`（同样交互终端 + YES，记账本）；同一任务所有版本加起来每项自动追加总量
  ≤ 第一版预算 × 规则百分比；只在预算用完挂起时触发，框架自动开下一版并记 `budget.raised`（写明依据哪条规则）；隔离前仍不得超过授权书 caps。

## Goal
When the owner amends a task that has owner-written subtasks, the new version decides — from ledger facts only —
which subtasks the change cannot have affected, and does not run them again: their earlier PASS (subtask
acceptance + subtask review, already merged into the files the new version starts from) still counts. Every other
subtask, and everything after it, runs again as today. The parent's own acceptance and final review always run
again on the new version's candidate (T48 rule unchanged). The decision and its reasons are recorded, and the
read-only adjudicator recomputes them; a forged reuse is never PASS.

## Required behaviour

### A. Which subtasks are reused (one shared function)
1. Write one function in `m2k/adjudicate.py` (next to `amendment_basis`) that both the kernel and the adjudicator use,
   like `amendment_basis` / `outside_changes`. Input: the ledger, the amended task id, the new version's owner
   contract (the same JSON-able data recorded as `"contract"`), and the new version's start files (path → digest,
   see item 4). Output: `reused` = {subtask id → source child task id} and `rerun` = {subtask id → short reason}.
   Every subtask of the new contract is in exactly one of the two.
2. Reuse happens only when **both** the old and the new contract have owner-written `subtasks` and **neither** has
   `plan` (a model-planned split is never reused: reason `planned by a model`). Otherwise every subtask reruns.
3. Going through the new contract's subtasks **in order**, subtask `s` is reused only if all of these hold; the first
   one that fails is its reason (wording may differ, meaning may not):
   a. every subtask before it in the new contract is reused (they run one after another, each sees the previous
      ones' files) — reason `an earlier subtask reruns`;
   b. the old contract has a subtask with the same id whose fields are equal except `budget` and `max_reassign`
      (these do not change what was judged) — reason `subtask changed`;
   c. the parent fields that enter the child's contract or its review are equal in both owner contracts: `goal`,
      `constraints`, `protected`, `acceptance.timeout_s` — reason `parent changed: <field names>`;
   d. the old version merged it: the old parent has a `subtask.finished` with `verdict` PASS for that id and the
      matching `merge_subtask` `effect.result` with `status` COMPLETED and `detail.verified` true; or the old version
      itself **reused** it (a `subtask.reused` event, item 5), in which case the source is that event's source
      (follow the chain back to the child that really ran) — reason `not merged in the earlier version`;
   e. the source child adjudicates PASS (read-only `adjudicate(..., check_chain=False)` with the same OBS / root
      arguments the caller uses; the kernel must **not** write a `gate.adjudication` onto the old child) —
      reason `earlier result does not adjudicate PASS`;
   f. the digests of `s`'s acceptance files in the new start files equal the source child's recorded
      `acceptance_files` — reason `acceptance file changed`;
   g. the new start files inside `s.writes` equal exactly the source child's last `candidate.frozen` files inside
      `s.writes` (same paths, same digests) — reason `files in its write scope changed`;
   h. compared with the old version's last `candidate.frozen` files, the new start files changed or added no path
      other than parent acceptance files that `s` does not use (the usual case: the owner's seed brings a new
      parent `check.py`) — reason `owner files changed: <paths>` (at most 10 listed).
   Rule h is deliberately strict: a subtask's acceptance could read any file, so any other owner file change
   reruns it. Document this in the README section.

### B. Kernel
4. In an amendment (`run(..., amends=...)`), after the workspace is prepared and the baseline frozen and **before
   anything is written** for the new task, compute item 1. Record in `task.submitted.amends`:
   `"start_files": <baseline files map>` and `"impact": {"reused": {...}, "rerun": {...}}`. Whenever the new
   contract has owner-written `subtasks`, `impact` is recorded (all in `rerun` when reuse is impossible). Tasks
   without `--amends` and amendments whose new contract has no `subtasks` record nothing new (no `start_files`, no
   `impact`; T48 payloads unchanged).
5. In `_run_subtasks`, a reused subtask does not run: no reservation, no child credential, no workspace, no worker,
   no merge. Append on the parent, at the point where it would have started,
   `subtask.reused` = `{"id", "task_id": <source child>, "candidate": <source child's last frozen digest>}`, then go
   on with the next subtask. It uses no budget (its usage is already in `prior_usage`). Rerun subtasks run exactly
   as today. When every subtask is reused, the parent still stops `FINISHED` and its acceptance and final review run.
6. Budget check: in an amendment, the existing rule "subtask budgets add up to at most the parent's budget" counts
   only the subtasks that will run (the effective parent budget after T48's deduction); every other subtask rule
   still applies to all subtasks. A failing check still refuses with `KernelError` before any event is written for
   the new task.
7. Concurrency (T48 lesson — think it through, test what can be tested): the reuse decision is fixed at submit and
   is derived from append-only facts of **finished** tasks (the old version has a verdict, so recall and reassignment
   of its children are impossible), plus the new version's own frozen baseline — files are never read from the
   old child workspaces. Two concurrent amendments of one version are still decided by T48's atomic successor guard;
   the losing one must not have written anything, including no `subtask.reused`. Add a test that a concurrent
   change to an old child's workspace after the decision has no effect on the new version.

### C. Read-only adjudicator
8. For a task whose `task.submitted.amends` has `impact`: not PASS (problem line says which) when
   - the digest of `amends.start_files` (same computation as `freeze`) is not the recorded `baseline`;
   - recomputing item 1 from the ledger with the recorded `contract` and `start_files` gives a different `impact`;
   - the task's `subtask.reused` events (ids and sources) are not exactly `impact.reused` in contract order, or a
     reused id also has a `subtask.started` or a merge in this task.
   An amendment whose recorded `contract` has `subtasks` but **no** `impact` key is not PASS either.
   Everything else is adjudicated as today (the parent's own acceptance, review, merges of rerun subtasks).

### D. Display and docs
9. `m2k status` shows `reused` (subtask ids) when there are `subtask.reused` events.
10. `README.md`: add to “改需求：开新版本（T48）” a short paragraph “影响分析（T49）”: only owner-written subtasks can be
    reused; the order rule; which changes rerun a subtask (rule a–h in plain words, incl. the strict h); the parent
    acceptance and final review always rerun; how to see it (`m2k status`, `impact` in the log).

## Out of scope
- Everything listed for T79 and T80 above. Any change to `worker.py`, `external.py`, `verify.py`, `gates.py`, trust,
  OBS, anchor, release or delivery code.
- Reusing the parent's own test or review, or any evidence of a task without subtasks (T48 rule stays).
- Reusing model-planned subtasks; finer dependency tracking than rules a–h; `m2k live` / `m2k report` changes.
- T48's non-blocking notes (files left in a refused workspace, etc.).

## Acceptance
```
python -m unittest tests.test_reuse tests.test_amend tests.test_adjudicate -v
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
`tests/test_reuse.py` (scripted models; reuse `tests/test_recall.py`'s `RecallCase.tree` two-subtask setup
`a` → `b`, extend it there if needed):
- v1 PASS; v2 changes only the parent acceptance via a seed with a new `check.py` that neither subtask uses →
  both reused: no worker call for `a`/`b`, no reservation, two `subtask.reused` events with the right sources and
  digests, the parent test and final review run, v2 PASSes, delivers, and adjudicates PASS; v1's events untouched.
- v2 changes subtask `b`'s goal → `a` reused, `b` reruns (`subtask changed`); v2 changes `a` → both rerun
  (`subtask changed`, `an earlier subtask reruns`); only `budget` / `max_reassign` of a subtask changed → reused.
- each of rules c, f, g, h gives its reason (c: parent goal; f: a subtask acceptance file changed by the seed;
  g: an in-scope file changed by the seed; h: another owner file changed by the seed).
- v1 stopped at a failing `b` (after `a` merged) → v2 reuses `a`, reruns `b` (`not merged in the earlier version`).
- v1 with a reassigned epoch for `a` (`a` merged from `p1.a.e2`) → the source is `p1.a.e2`.
- v3 amends a v2 that reused `a` → v3's source for `a` is still the child that really ran in v1.
- the source child no longer adjudicates PASS (append an `outside.changed` to it) → reruns with that reason, and
  the kernel wrote nothing onto the old child.
- model-planned v1 → all rerun (`planned by a model`); amendment whose new contract has no subtasks → no `impact` key (T48 payload unchanged).
- budget: v2 whose effective budget is too small for all subtask budgets but enough for the rerun ones → runs;
  too small for the rerun ones → refused, nothing written.
- concurrency: two connections amend v1 at once (copy T48's race test) → one wins, the loser wrote nothing and no
  `subtask.reused`; after the decision, changing a file in v1's child workspace does not change v2's run or result.
- `tests/test_adjudicate.py`: forged `impact` (an id moved from `rerun` to `reused`), forged `start_files`, a missing
  or extra `subtask.reused`, a reused id that also ran, an amendment with subtasks but no `impact` → not PASS;
  an honest reuse → PASS.
- `tests/test_amend.py`: only change assertions that reuse now changes (e.g.
  `test_version_briefing_and_review_line_reach_new_subtasks` amends with a budget-only change, which now reuses both
  subtasks — make that test change the parent goal so the subtasks still run, keep what it checks). Do not weaken
  any other T48 assertion.

## Review notes (written by reviewer)

- 2026-10-28 Claude: round 1 sent back: inherited reuse was trusted without checking the source; sources are now resolved recursively through each version's recorded impact and verified where they really ran; rules b and c compare with that version's contract.
- 2026-10-28 Claude: PASS on 33054c7. Codex review (gpt-6-astra) round 1 sent back one point: inherited reuse (an earlier version's `subtask.reused`) was trusted without checking its source, so a wrong or forged `a → p1.b` let the new version reuse another subtask's PASS with zero worker calls and deliver; fixed by resolving sources recursively through each version's recorded impact and verifying them where they really ran (started, finished PASS, verified merge of that candidate), with rules b and c compared against that version's contract; round 2 PASS. GLM-5.3 GO twice. CI and card-scope green. `m2k/` +147 (budget raised to 160 for the fix).
- Non-blocking notes kept for a later small-fixes card: reuse depends on the kernel's `merge-<id>` op_id naming (fails safe to rerun); when source resolution fails, the recorded reason can read `subtask changed` / `parent changed` instead of `not merged in the earlier version`; the `--amends` CLI path re-parses the TOML with a zeroed budget to defer the sum check; the started-or-merged check in the adjudicator is dense; `amendment_impact`'s `**checks` is undocumented.

## Handoff note (written by implementer, ≤ 10 lines)

- Shared ledger-only impact analysis between submission and read-only adjudication; reuse is an unaffected prefix with original child/epoch sources.
- Reused subtasks create no reservation, credential, workspace, worker run or merge; parent acceptance and final review always run again.
- Amendment budget sums count only rerun subtasks, including through the CLI; every other subtask rule still applies.
- Added reason, three-version, source integrity, forged evidence, contract order, CLI and concurrency coverage; losing submissions write no events.
- Python 3.13.11: targeted acceptance 59/59; offline scenarios 28/28; T46 CHECK OK (all attack/defence checks and its full suite passed).
- Full suite: 902 tests, 887 passed, 15 skipped; baseline 34cc843 targeted suite: 37/37; card scope passed, m2k/ additions 122/130.
- Out-of-card findings: none; worktree retained for review; no PR opened.
- CI, GLM-5.3 GO and final reviewer approval remain pending; the card is not marked done.
- 第 1 轮返工：递归核对每版 impact 与唯一复用事件，在真实运行版核验来源 parent、started、PASS 和已验证合并；b/c 对照该版契约；新增伪造链、OBS、重复事件等 9 项回归。
- 四项验收通过：定向 68/68、离线 28/28、T46 CHECK OK、全套 911 项（896 通过、15 跳过）；m2k/ 累计新增 147/160 行，递归追溯、防环与真实来源证据核验需超过原 130 行，按本轮授权同步预算；待 CI/复审。
