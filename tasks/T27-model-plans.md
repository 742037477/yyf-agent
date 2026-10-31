# T27: a model proposes the split; the plan is a proposal that must pass the gates

- **Assignee:** Claude (core logic; GLM-5.3 GO required, because the author is also the final reviewer)
- **Branch:** `task/T27-model-plans`
- **Line budget:** ≤ 170 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/contract.py`, `m2k/trust.py`, `m2k/kernel.py`, `m2k/types.py` (one new worker status), `m2k/accept.py`, `tests/test_model_plans.py`, `README.md`
- **Status:** DONE

## Goal
Stage 3, GLOBAL/DECISION. Instead of writing `[[subtasks]]` by hand, the task owner can let a planner model propose
the split.
- The plan is a **proposal** (data), like every model reply. It passes the same plan rules as a hand-written plan
  (`check_subtasks`), plus rules for a model's plan.
- An accepted plan is recorded in the ledger and then executed by the T21–T24 machinery: child credentials,
  reservations, recall and reassignment.
- Owner decisions (2026-10-14):
  - an accepted plan runs automatically;
  - a subtask's acceptance comes only from the owner's acceptance files.

  So the planner never writes an acceptance command: it only chooses which owner checks apply.

## Required behaviour
1. **Contract.**
   - `[task] plan = "model"` (the only allowed value) asks for a model plan. It is a `ContractError` together with
     `[[subtasks]]`.
   - `[acceptance] parts` is a table mapping an owner acceptance file to the command that checks it, for example
     `parts = { "check_a.py" = "{python} check_a.py" }`. It is required with `plan = "model"`. Every key must be in
     `[acceptance].files`, and every command must contain its file.
   - New fields: `TaskContract.plan: str = ""` and `Acceptance.parts: tuple[tuple[str, str], ...] = ()`.
2. **Planning** (in `Kernel.run`, after `task.submitted`, before any subtask). The planner is the worker model. It
   gets the goal, the constraints, the workspace file names (at most 200), the owner's parts with each file's text
   (at most 1500 characters each), the tools, network and the parent budget. It must reply with one JSON object:
   `{"subtasks": [{"id", "goal", "writes", "acceptance_files", "budget", "tools"?, "network"?, "max_reassign"?}]}`.
   - Each planner call is a model call of the parent (`model.called` with role `planner`). It is allowed only while
     the parent budget allows it.
   - The kernel builds each subtask's acceptance command itself: the owner's commands for its `acceptance_files`,
     joined with ` && `. A planner-supplied `acceptance` key, or any other unknown key, rejects the plan.
   - **Rules** for a model's plan, on top of `check_subtasks`:
     - 1 to 6 subtasks;
     - every `acceptance_files` entry is a key of `parts`;
     - every attempt id and attempt workspace is unused;
     - for every limited budget key, the subtasks' sum plus what the parent has used plus one review call must fit
       the parent budget.
   - Ledger: `plan.proposed` `{"attempt", "reply_head"}`, then `plan.rejected` `{"attempt", "reason"}` or
     `plan.accepted` `{"subtasks": [...], "plan_sha256"}`.
   - A rejected plan gets **one** more planner call. Its prompt adds `Your previous plan was rejected: <reason>`.
   - If no plan is accepted, the worker status is `PLAN_REJECTED` (new), the parent verdict is FAIL, and nothing runs.
3. **Execution.** The accepted plan runs exactly like a hand-written one.
   - The parent's own chain is still verified against the **submitted** contract: the credential was issued for it.
   - Child credentials and their checks use the accepted plan. `trust.verify_child_chain` (and
     `issue_child_credential`) take an optional `plan` (the subtasks), defaulting to `parent_contract.subtasks`.
     `WRONG_PARENT` and `EXCEEDS_PLAN` look up the subtask in that plan.
   - The kernel keeps the accepted plan per parent and passes it. A child can never be wider than the parent
     credential (`EXCEEDS_PARENT`), whatever the plan says.
4. **Status.** `m2k status` shows `"plan": "model"` and the accepted subtask ids.
5. **Offline scenario** before `s_bytecode`: `模型拆任务，先交越权方案`, expected `越权方案被拒；合规方案执行，PASS`.
   - The first plan names an acceptance file the owner never listed.
   - The second plan is valid and runs to PASS.
6. **README**: a short paragraph with a `plan = "model"` / `parts` example.

## Out of scope
- A separate planner model, parallel subtasks, an owner confirmation step, nested plans.

## Acceptance
```
python -m unittest tests.test_model_plans -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
`tests/test_model_plans.py` (temporary folders, `ScriptedModel` only; with and without `dev_setup` where noted):
- **Contract.** `plan` and `parts` parse. Rejected: `plan` with `[[subtasks]]`; `plan` without `parts`; a `parts` key
  not in `files`; a command that does not contain its file; `plan = "other"`.
- **A valid model plan runs to PASS.**
  - The ledger has `plan.proposed` and `plan.accepted`, and a planner `model.called` under the parent.
  - The subtasks' acceptance commands are the owner's parts.
  - With `dev_setup`, the children's credentials verify against the accepted plan while the parent credential still
    matches the submitted contract.
- **Each model-plan rule rejects**, with `plan.rejected` and its reason:
  - a planner `acceptance` key;
  - an unknown key;
  - a foreign acceptance file;
  - too many subtasks;
  - a write path on a protected file;
  - a budget that does not fit after the planner call and the review;
  - tools wider than the parent;
  - network without the parent's;
  - not JSON.
- **Retry.** A rejected first plan and a valid second → PASS; the second prompt contains the rejection reason. Two bad
  plans → `PLAN_REJECTED`, FAIL, and no subtask events.
- **Budget.** A parent budget that does not allow the planner call → `PLAN_REJECTED` without a model call.
- **Mutations.** Removing the parts-only rule, the budget headroom rule or the plan passed to the child checks makes
  a test fail.

## Review notes (written by reviewer)

**Round 1 (PR #27): PASS (Claude, 2026-10-14, after GLM GO), merged as #27.** CI was blocked by GitHub billing for
about an hour; re-run after the owner fixed it. Later: a planner `ModelError` could also use the one retry.

## Handoff note (written by implementer, ≤ 10 lines)
- `contract.py`: `TaskContract.plan`, `Acceptance.parts`, parse rules; `plan_subtasks` turns a planner reply into
  subtasks (allowed keys only, owner parts only, 1–6, acceptance commands built from `parts`) and then `_subtasks`.
- `kernel.py`: `_plan` (worker model as planner, budget-gated, two proposals at most, `plan.proposed` /
  `plan.rejected` / `plan.accepted` with `plan_sha256`), `_plan_fits` (budget headroom incl. planner use and one
  review; free attempt ids and workspaces); parent chain verified against the submitted contract while child checks
  use the accepted plan (`trust.py`: optional `plan` argument); `PLAN_REJECTED`; a planned parent never enters the T25
  repair loop; `status` shows `plan` and `planned`.
- 11 tests in `tests/test_model_plans.py`; mutations (parts-only rule, key rule, headroom, plan to child checks,
  parent chain against the planned contract, retry, repair guard) each fail tests. Offline 22/22; full suite and T17
  owner check OK; 140/170 lines in `m2k/`.
