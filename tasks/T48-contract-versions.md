# T48: contract versions — the owner amends a finished task; the new version continues from its last candidate and is judged afresh

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T48-contract-versions`
- **Line budget:** ≤ 190 added lines in `m2k/` in total (tests not counted)
- **Files you may change:** `m2k/kernel.py`, `m2k/cli.py`, `m2k/adjudicate.py`, `m2k/verify.py`, `m2k/worker.py`, `m2k/ledger.py`,
  `m2k/external.py`, `README.md`, `tests/test_amend.py` (new), `tests/test_adjudicate.py`, `tests/helpers.py`,
  `tasks/README.md`, `tests/test_ledger.py`
- **Status:** DONE (merged #60, 453f5c4)

## 背景（用户 2026-10-28 拍板）
第 3.5 阶段“需求变更”。现在一次 `m2k run` 一气呵成：中途停不下、也接不上（暂停与续接是第 4 阶段 T51/T52）。所以**改需求只在两次运行
之间发生**：所有者改好契约，用 `m2k run --amends <旧任务>` 开一个**新版本**，从旧版本最后冻结的候选接着做。本卡只做版本链本身，
且**保守**：旧版本的验收与审查结论对新版本一律无效，全部重跑。拆分（用户同意）：
- **T48（本卡）**：版本链、从旧候选继续、预算**跨版本累计**（用户决定：契约里的预算是整个任务所有版本的总额，已用的接着算；
  要多花就在新契约里写大，与改派、修复的规矩一致，防止靠反复改需求溢花钱）、审查者与裁决器按新版本判。
- **T49**：影响分析——只让受影响的子任务、候选与审查失效，未受影响的结果复用；超出范围就挂起待授权；Agent 提问。“挂起”就是这次运行
  以“待授权”结束，所有者回答或授权后用 T48 开新版本接着做。

## Goal
A task owner can change a finished task's contract (goal, constraints, acceptance, budget, tools, subtasks…) and
run it again as version n+1 of the same piece of work. The new version starts from exactly the files the previous
version left (its last frozen candidate, checked against the ledger), plus any new owner files from its seed; it
spends from the budget that is left across all versions; and everything about the old verdict is only a fact
for the worker, never evidence for the new one. The ledger shows the chain, what the owner changed, and the
read-only adjudicator checks that the chain and the budget accounting are real.

## Required behaviour

### A. Recording the contract (every top-level task from now on)
1. `task.submitted` of a top-level task (no parent) gets `"contract"`: the owner's contract as canonical JSON-able
   data (the same structure `TaskContract.digest()` hashes, paths as strings), so a later version can be compared
   with it. Subtasks do not get it. Nothing else in `task.submitted` changes for runs without `--amends`.

### B. `m2k run --amends <task_id> <contract.toml>`
2. New optional flag on `run`. The new contract has its own **new** `task_id` and its own **new, empty**
   workspace (existing rules). Refuse with a `KernelError` (the CLI turns it into the usual non-zero exit, no
   traceback), before anything is written for the new task, when the amended task:
   - is not in the ledger, has a `parent`, or has no `verdict` event yet;
   - has no `"contract"` in its `task.submitted` (it ran before T48);
   - is already amended by another task (the chain is linear: at most one successor per version);
   - has a workspace whose current files (same file rule as `freeze`) do not equal its **last** `candidate.frozen`
     files (message: the earlier version's workspace no longer matches its last frozen candidate).
3. Workspace: copy exactly the files of that last candidate from the old workspace into the new (empty) workspace,
   then, if the new contract has a `seed`, copy the seed over it (owner files win, e.g. a new `check.py`). The
   existing "workspace must be empty when a seed is used" rule applies to the workspace **before** the copy.
   Protected and acceptance hashes, the baseline, shadow and scope checks then work exactly as today on the
   result.
4. Budget across versions: `prior` = the usage (`Ledger.usage` keys `rounds`, `model_calls`, `tokens`,
   `commands`, plus `tokens_unknown_calls`) of every earlier version **and all their descendant tasks** (every
   subtask and every reassigned epoch, by the `parent` links in `task.submitted`; reuse/move the descendant walk
   that `adjudicate.outside_changes()` already has instead of writing a second one). The version runs with an
   effective budget = owner budget − prior (per field: `max_rounds`−`rounds`, `max_model_calls`−`model_calls`,
   `max_tokens`−`tokens` when not `None`, `max_commands`−`commands`). If any effective value is ≤ 0, refuse at
   submit (`KernelError`: budget already used up by earlier versions, naming the field and both numbers). Apply
   it with `dataclasses.replace` **before** the credential is issued, so every existing gate, reservation and
   trust check just sees a smaller budget; nothing else in the budget code changes. Unknown token usage stays
   unknown: it is recorded, not treated as zero, and not used to reduce `max_tokens` (same as today).
5. `task.submitted` of the new version gets `"amends": {"task": <old id>, "version": n, "from_candidate":
   <old last candidate digest>, "changed": [...], "prior_usage": {...}, "owner_budget": {...}}`. `version` is the
   old version + 1 (a task without `amends` is version 1). `changed` = sorted names of the contract fields whose
   values differ between the old and new owner contracts, ignoring `task_id`, `workspace`, `seed` and
   `deliver_to`. `prior_usage` is item 4's numbers; `owner_budget` is the budget as written in the new contract
   (the effective one is what `"contract"` and `contract_sha256` already show).
6. What the worker is told (facts, not instructions): the first worker run of the version gets a briefing, through
   the existing `briefing` path of `Kernel._work` (native worker and `CliAgent` alike), like:
   `this is version n of the task (amends <old>); the owner changed: goal, acceptance`, the old version's final
   verdict and its reasons (at most 1500 characters), and the remaining budget. Nothing from the old worker's
   conversation or the old reviewer's text beyond the recorded `verdict` reasons.
7. What the reviewer is told: one framework line in the audit prompt (not inside the untrusted context block):
   `Contract version n; the owner changed: …. Judge only against the current Goal, Constraints and acceptance.`
   The reviewer still never sees the worker's conversation and still gets the diff against the version's own
   baseline (the files it started from).
8. Old evidence never counts: the new version's test, review, verdict, adjudication and delivery are only about
   its own frozen candidate. Delivery rules are unchanged (the new version needs a `deliver_to` that the existing
   checks accept; nothing is marked "superseded" on the old delivery — out of scope).

### C. Read-only adjudicator
9. For a task whose `task.submitted` has `amends`: not PASS (problem line says which) when the amended task is
   missing, has a parent, has no `"contract"`, or has another successor; when `version` is not its version + 1;
   when `from_candidate` is not the digest of its last `candidate.frozen`; or when `prior_usage` differs from the
   usage the adjudicator recomputes from the ledger with the same descendant walk. Tasks without `amends` are
   adjudicated exactly as today.

### D. Docs
10. `README.md`: a short section “改需求：开新版本（T48）”: when to use `--amends`; it continues from the last
    candidate; the budget is the total across versions (write a bigger one to allow more); the old verdict does not
    carry over; the chain is linear; tasks run before T48 cannot be amended.

## Out of scope
- Reusing any earlier evidence (unaffected subtasks, reviews) and impact analysis — T49.
- Suspending, asking the owner, authorizing out-of-scope changes — T49. Changing a task while it runs — stage 4.
- Marking an old delivery as superseded; `m2k live` / `m2k report` changes; amending a subtask on its own.
- Any change to trust, OBS, anchor, release or delivery code.

## Acceptance
```
python -m unittest tests.test_amend tests.test_adjudicate -v
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
`tests/test_amend.py` (scripted models; build ledgers with the existing helpers):
- v1 FAIL (bad code) → owner amends with a fixed goal → v2 starts with v1's files (assert the copied file bytes
  and `amends.from_candidate`), its worker briefing names version 2, the changed fields and v1's verdict, and v2
  PASSes and delivers; v1's events are untouched (same count and hashes before and after).
- seed overlay: v2 with a new owner `check.py` in its seed → the new acceptance file is what v2 hashes and runs.
- `changed` lists exactly the edited fields (e.g. `goal` and `acceptance`; not `task_id`/`workspace`/`seed`).
- budget: v1 uses k model calls; v2 with the same budget gets `max_model_calls − k` (assert through the recorded
  numbers and by v2 stopping on the budget gate when it runs out); a v2 budget already used up → refused at
  submit, nothing written for v2; v1 with subtasks and a reassigned epoch → all their usage counts.
- every refusal in item 2 (missing, child task, no verdict, pre-T48 task without `"contract"` — build one by
  appending a `task.submitted` without it — second successor, workspace changed after v1's last freeze); a v3
  that amends v2 → version 3 and prior usage of v1 and v2 together.
- the reviewer's prompt contains the version line, outside the untrusted block; the external agent path gets the
  briefing too (use the existing fake agent).
- CLI: `m2k run --amends <id> <contract>` end to end with a config, as the existing CLI tests do.
- `tests/test_adjudicate.py`: a forged chain is not PASS — tampered `prior_usage`, wrong `version`, wrong
  `from_candidate`, two successors of one version; an honest v2 adjudicates exactly like a v1.

## Review notes (written by reviewer)
- 2026-10-28 Claude: round 1 sent back the non-atomic successor check; `m2k/ledger.py` and `tests/test_ledger.py` added to Files for the fix.
- 2026-10-28 Claude: PASS on e7081f5. Codex review (gpt-6-astra) round 1 sent back one point: the single-successor check and the new version's `task.submitted` were not atomic, so two concurrent `--amends` of one task both ran, both ended UNKNOWN and locked the chain; fixed with a guard checked inside the `BEGIN IMMEDIATE` of `Ledger.append` (`successor_denial`), the loser refused before any worker run with no event, plus race and retry tests. GLM-5.3 GO twice. CI and card-scope green. `m2k/` +136 (≤ 190).
- Non-blocking notes kept for a later small-fixes card: a losing or late-refused submission leaves the copied files in its workspace (a retry needs a fresh workspace); the refusal for a deleted old workspace shows the raw OS error instead of the card's wording; the parent walks in `_work` / `_audit` assume every parent `task.submitted` exists; the briefing prints the remaining budget as a raw dict; the `--amends` help text is English while the CLI help around it is Chinese.

## Handoff note (written by implementer, ≤ 10 lines)
- Recorded canonical top-level contracts and linear amendments from verified last candidates with owner seed overlays.
- Deducted every earlier version and descendant epoch's usage from owner budgets before credential issuance.
- Added native/external worker briefings, current-version review prompts, and read-only chain/usage checks.
- Verified with Python 3.13.11 on Windows: targeted tests 36/36; offline scenarios 28/28.
- tasks/T46_check.py passed: 21 attack tests, all defence checks, offline scenarios, and its full suite (T46 CHECK OK).
- Full suite: 876 run, 861 passed, 15 skipped; baseline a931f7d: 853 run, 838 passed, 15 skipped.
- Scope gate passed; 117 added lines under m2k/ (limit 190); no out-of-card implementation changes.
- CI, GLM-5.3 GO and final reviewer approval remain pending; no PR opened.
- Round-1 fix: atomic successor submission rejects races before workers without events and permits legal retries; latest validation: 37 targeted tests, 14 ledger tests, 28 offline scenarios, T46 CHECK OK, 880 tests (865 passed, 15 skipped); m2k/ additions now 136/190.
