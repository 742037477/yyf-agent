# T80: pre-authorization — the owner says in advance what may be granted, and the framework grants it during the run

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T80-preauth`
- **Line budget:** ≤ 205 added lines in `m2k/` in total (tests not counted; estimate ≈ 170)
- **Files you may change:** `m2k/contract.py`, `m2k/worker.py`, `m2k/external.py`, `m2k/kernel.py`, `m2k/adjudicate.py`,
  `m2k/cli.py`, `README.md`, `tests/test_preauth.py` (new), `tests/helpers.py`, `tasks/README.md`
- **Status:** DONE (merged #63, 0e9d47b)

## 背景（用户 2026-10-28 拍板；全文在 `docs/ARCHITECTURE.md` 第 3.5 阶段决定“授权两种模式”）
T79 做了“提问 → 任务以待授权结束 → 所有者 `m2k answer` 开新版本”。用户要两种授权：
- **预授权（本卡）**：所有者事先在契约里写“可放行范围”——可额外写的路径、可加的工具、能否联网、预算最多追加多少。Agent 申请的东西落在范围内时
  框架**当场放行、任务不停**，记账写明依据哪一条（带 OBS 回执）；超出才走 T79 的待授权。预算按用户已定的算法：同一任务所有版本累计，每项自动追加
  总量 ≤ 第一版预算 × 百分比。放行后仍不得超过授权书 caps。
- **实时授权（下一张 T81）**：运行原地挂起等所有者在另一个终端批准，同一次运行接着做。T81 复用本卡的“运行中放权”机制（`authority.granted`
  的 `basis` 留给 T81 加 `"m2k answer"`），只加等待和跨终端批准。

本卡的取舍（Claude 定，写进 Review notes）：可放行范围只写在契约 `[preauth]` 里（契约本来就是所有者的文件，与预算、工具同等可信）；
跨任务的常设规则 `m2k owner-rule` 不在本卡。只对**没有子任务、没有规划的顶层任务**生效；子任务的申请照旧上抛父任务待授权。

## Goal
A top-level task's contract may carry `[preauth]`: writes, tools and network that may be added, and a percentage by
which each budget field may grow. When the worker asks for something inside it (or the framework runs out of budget
and the percentage still has room), the kernel widens the task's effective contract on the spot, issues a new
credential within the charter caps, records `authority.granted` with an OBS receipt naming the clause it used, and the
same worker goes on — the native worker in the same conversation, an external agent by being started again in the same
workspace. Anything outside `[preauth]` behaves exactly as T79. The read-only adjudicator replays every grant and
refuses a PASS when any grant has no basis or the final credential does not cover the final effective contract.

## Required behaviour

### A. Contract
1. Optional `[preauth]` in the task TOML: `writes` (list of paths, as `writes`), `tools` (list of tool names from
   `DEFAULT_TOOLS`), `network` (bool, default false), `budget_percent` (int 1–100, optional). Unknown keys or bad types
   → `ContractError`. Allowed only on a top-level task with no `[[subtasks]]` and no `plan` (else `ContractError`).
   A contract without `[preauth]` keeps **exactly today's digest and today's recorded `contract` JSON** (omit the field
   when it is `None`), so existing ledgers and tests are untouched.
2. `approval_needs` (T79): an amendment whose `[preauth]` is wider than the previous version's (new writes not covered,
   new tools, network false → true, `budget_percent` added or higher) needs approval: `scope["preauth"] = [old, new]`.
   Read old contracts with `.get("preauth")` (recorded contracts before T80 have no such key).

### B. The grant (kernel; one function, reused by T81)
3. `Kernel._grant(tid, request, by)` — `request` is a T79 request object (`kind` `scope` or `budget`), `by` is `"worker"`
   or `"framework"`. It returns the new effective contract, or `None` (not grantable). Grantable only when the task is
   top-level without subtasks or plan, its submitted contract has `[preauth]`, and **every** part fits:
   - `scope`: each requested `writes` path is covered by `preauth.writes` (`in_scope`) — paths already covered by the
     effective `writes` (or effective `writes` empty) need nothing; each requested tool is in `preauth.tools`;
     `network: true` needs `preauth.network`. `question` is never grantable.
   - `budget`: each requested field is a new limit; the increase over the effective limit, plus every earlier
     `authority.granted` budget increase on this task **and on every earlier version of its amends chain**, must be
     ≤ `floor(first version's owner budget[field] × budget_percent / 100)`. Fields that are `None` (unlimited) are never raised.
   A request that changes nothing returns the effective contract unchanged and writes nothing.
4. On a grantable request, in this order: build the new contract with `dataclasses.replace` (writes / tools /
   network / budget only); when trust is on, issue its credential **into a local variable** (a charter-cap failure →
   not grantable, nothing written, old contract and credential stay); append `authority.granted` = `{"basis": {"by":
   "preauth", "clause": <the [preauth] items used>}, "by": by, "request": request, "fields": {name: [old, new]},
   "contract_sha256": <new digest>}` through `Kernel._record` **with a guard** that recomputes the budget allowance from
   the ledger inside the same write transaction (refuse → not grantable, nothing written); only then make the new
   credential and contract current and record `credential.issued`. A crash between the two leaves the last credential
   not covering the final contract, which the adjudicator rejects (fail closed).
5. The effective contract is what the rest of the run uses: the trust-chain gate (verify against the current contract,
   not the one captured at submit), every other gate, `Workspace.network`, the worker's budget checks, repairs within the
   same run, the outside-writes check, the reviewer prompt (it sees the granted writes / tools / network), the report.
   Grants never carry over to a later version (T48 versions start from the owner's new contract).

### C. Workers
6. Native worker: `run_worker(..., grant=None)`. On a valid `ask_owner` with kind `scope` or `budget`, call `grant`;
   a contract back → continue with it, feedback `GRANTED by pre-authorization: <fields>`; `None` → today's T79 path
   (`owner.request`, `AWAITING_OWNER`). When `model_budget_denial` fires and `grant` is set, ask for the exhausted
   fields at their maximum allowed limit with `by = "framework"`; granted → continue the loop; not → today's path.
   A granted request is **not** recorded as `owner.request` (only inside `authority.granted`), so nothing counts it as awaiting.
7. External agent: after an exit 0 with a valid `.m2k/ask.json` of kind `scope` (writes only — tools and network are not
   enforced for external agents, so they are never grantable there) or `budget` (`max_tokens`), call `grant`; granted →
   delete the file and start the agent again in the same workspace with the same prompt plus `The owner's
   pre-authorization granted: <fields>. Continue the task.`, at most 3 restarts per run (then T79's path). Each start has
   its own `effect.intent` / `effect.result` (`op_id` `x1`, `x2`, …) and `model.called`; the token limit counts all starts.
   Token-limit exhaustion detected after exit stays as today (no grant: the tokens are already spent).
8. The kernel passes `grant` only to the worker of a top-level task without subtasks or plan.

### D. Read-only adjudicator
9. Replay the task's `authority.granted` events in ledger order from the submitted contract: each must have
   `basis.by == "preauth"`, `fields` whose old values equal the effective contract before it, new values within the
   submitted `[preauth]` and the chain-wide budget allowance (same computation as item 3, from ledger facts only), a
   `contract_sha256` equal to the digest of the replayed effective contract (same serialisation as
   `TaskContract.digest`), a valid OBS receipt when an OBS key is configured, and — with trust on — a `credential.issued`
   after it covering that digest. The last credential must cover the final effective contract (today: the submitted
   one). Outside-writes and budget checks use the final effective contract. Any failure → not PASS with a clear reason.
   A task with no `authority.granted` adjudicates exactly as today.

### E. CLI and docs
10. `m2k run` prints each grant: `✓ 预授权放行（<条款>）：<字段 旧 → 新>`. `m2k status` lists `granted` (seq, by, fields).
11. `README.md`: a section “预授权（T80）”: the `[preauth]` keys, what happens inside and outside it, the chain-wide
    budget rule with an example (first version 100 000 tokens, 20 % → at most 20 000 extra across all versions), external
    agents restart (conversation not kept), top-level tasks only, still within the charter caps.

## Concurrency (think it through; T48, T49 and T79 were all sent back for check-then-write gaps)
- The allowance check and the `authority.granted` append are one transaction (item 4 guard). Two versions of one chain
  never run at once today (an amendment needs a verdict), but the guard keeps the rule true if that changes (T58).
- The current contract and credential change together only after the grant is written; a refused or failed grant
  leaves both untouched. Gates read the current pair, never a copy taken at submit.
- The external agent's workspace watcher (T73) keeps running across restarts; a restart must not reset its baseline.

## Out of scope
- Live waiting for the owner, `m2k answer` during a run (T81). `m2k owner-rule` (standing rules across tasks).
- Grants for subtasks, planned tasks or parents of subtasks. Any change to trust, OBS, anchor, release, delivery,
  sandbox or live-view code. T48/T49/T79 non-blocking notes.

## Acceptance
```
python -m unittest tests.test_preauth -v
python -m unittest tests.test_owner tests.test_amend tests.test_adjudicate tests.test_external_agent -v
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
`tests/test_preauth.py` (scripted models; `tests.test_external_agent`'s fake agent for the external path):
- contract parsing: each bad key / type, `[preauth]` with subtasks or plan → `ContractError`; no `[preauth]` → same digest
  and recorded JSON as before.
- native: scope request inside → `authority.granted` with OBS receipt, new `credential.issued`, worker continues in the
  same conversation and uses the granted write / tool / network, verdict PASS and adjudicator PASS; outside (one part
  out) → T79 awaiting, nothing granted; `question` → awaiting.
- budget: framework exhaustion with room → granted and the run goes on; allowance used up across two versions of one
  chain → awaiting; exactly-at-limit and one-over cases; a grant over the charter caps → awaiting, old credential still
  verifies.
- external: `.m2k/ask.json` writes request inside → agent restarted (two `x` op ids), granted writes not flagged as
  outside writes; tools / network request → awaiting; 4th request → awaiting.
- amendments: a version that widens `[preauth]` → needs approval (T79 refusal without `m2k answer`).
- concurrency: the guard refuses a grant when another `authority.granted` for the chain is appended between the
  computation and the write (inject it from the guard's first call); nothing written, contract and credential unchanged.
- adjudicator (forge ledgers like `tests/test_adjudicate.py` does): a grant without basis, with `basis.by` ≠ `preauth`,
  outside `[preauth]`, over the allowance, with a wrong `contract_sha256`, without its `credential.issued`, or a last
  credential that covers the old contract → not PASS; an honest granted run → PASS.
- Existing tests must pass unchanged (no `[preauth]` → no behaviour change). If one does not, stop and say which in the
  handoff note instead of editing it.

## Review notes (written by reviewer)

- 2026-10-28 Claude: design choices made when writing the card: pre-authorization lives only in the contract's `[preauth]` (the contract is the owner's file, as trusted as its budget and tools); standing rules across tasks (`m2k owner-rule`) are left out; only top-level tasks without subtasks or plan get grants; external agents are restarted in the same workspace (their conversation is not kept) and only writes and max_tokens are grantable for them.
- 2026-10-28 Claude: round 1 (Codex review, gpt-6-astra) sent back one point: the adjudicator crashed with IndexError when replaying a malformed historical `fields.budget`. Fixed with regression tests.
- 2026-10-28 Claude: GLM-5.3 returned an empty reply three times (about 11 minutes each); a second independent Codex review replaced it. It sent back one point: the native worker called `preauth_limits` outside the grant's refusal path, so a malformed grant history stopped the run without a verdict. Fixed: treated as not grantable (BUDGET_EXHAUSTED, then awaiting the owner as in T79), with a regression test. Line budget raised from 200 to 205 for this fix.
- 2026-10-28 Claude: PASS on 5d54feb. Claude spot-checked both reworks line by line plus `Kernel._grant` (guard and credential switch order), `run_worker` and the `CliAgent.run` restart path. CI and card-scope green. `m2k/` +203/205.
- Non-blocking notes kept for a later small-fixes card: an external agent's repair round restarts its op ids at `x1`, so `max_repairs = 1` after a NO_GO review hits a duplicate operation (also on main before T80); `preauth_change` imports `owner_request` inside the function; the reviewer sees the effective authority as an extra constraint line rather than a dedicated field.

## Handoff note (written by implementer, ≤ 10 lines)

- 基于 origin/main 48b00c9 在指定 worktree 实现；主工作区未切分支、未改文件、未 stash。
- 新增契约预授权与扩大预授权的所有者审批；无预授权时保持原 asdict、摘要、契约 JSON 和行为，旧测试未改。
- 额度重算与 authority.granted 同处 _record 的写事务 guard；凭证先在局部签发，记账成功才切换当前契约与凭证，Gate 读取当前契约。
- 原生 Worker 保留对话续跑；外部 Agent 最多重启三次，累计 token、区分 x 操作且沿用观察器；裁决器重放每次放行并核对 OBS、凭证、最终范围与预算。
- 使用指定 Python 3.13.11；预授权测试 31/31、指定旧测试组 91/91、离线场景 28/28 均通过。
- T46_check.py 返回 T46 CHECK OK；独立与 T46 内含全套均为 976 项、961 通过、15 跳过、零失败。
- m2k/ 新增 193/200 行；card_check 与 git diff --check 通过；仅改许可文件及本卡，无偏离卡项。
- 保留 worktree，未开 PR；CI、GLM-5.3 GO 与最终审阅待执行，未自行判定任务卡完成。
- 第1轮返工：对照 f25302a 复现两次放行后 []/[{}] 历史预算导致崩溃；现校验 [dict, dict] 与 int/None 值，带 seq 拒绝并兜住凭证/start_files 畸形结构；定向 60/60、离线 28/28、全套 979 项（964 通过、15 跳过）均无失败；m2k/ 累计新增 200/200 行，仅修本轮阻断点。
- 第2轮返工：以 17ff5cc 同用例复现 seq 4 ValueError，Worker 自动申请额度计算失败时以 BUDGET_EXHAUSTED 正常收尾并进入 T79 待授权；新增两版本回归，停止/冻结/裁决齐全且无新放行，外部 Agent 和内核调用均已核对；定向 61/61、离线 28/28、全套 980 项（965 通过、15 跳过）均无失败；m2k/ 累计新增 203/205 行（本轮净增 3 行）。
