# T29: stage 3 end to end — a model splits a real task, several workers build it, every part is reviewed

- **Assignee:** 大肥鱼 (DeepSeek, planner and workers) via m2k (`tasks/T29.toml`, GitHub workflow `m2k-task`);
  GLM-5.3 reviews every subtask and the whole
- **Branch:** `task/T29-stage3-e2e`
- **Line budget:** no change in `m2k/`
- **Files you may change:** `examples/expenses/*.py`
- **Status:** DONE

## Goal
Stage 3 run for real, with no scripted model. The owner gives only:
- a goal: a small expense-report tool in `examples/expenses/`;
- three part checks and one whole check, in `tasks/T29/`;
- a budget.

The run then works like this:
1. The planner model (`plan = "model"`) proposes the split. The plan is checked before anything runs: only owner
   checks, no wider tools or network, budget room for the review.
2. Each subtask runs in its own workspace copy, under its own child credential and budget.
3. GLM reviews each subtask, and only a PASS is merged back.
4. The parent runs the whole check and a final review. Only PASS is delivered.

## Required behaviour (what the ledger must show)
1. `plan.accepted` with 2–3 subtasks, each choosing owner part checks only. A first plan may be rejected, and then the
   second must pass.
2. For each subtask:
   - `subtask.started` and a child `credential.issued`;
   - a `test.result` PASS and an `audit.result` GO;
   - a `merge_subtask` intent and result.
3. The parent `test.result` PASS (`tasks/T29/check_all.py`), `audit.result` GO, `verdict` PASS. The ledger hash chain
   and anchor are intact.
4. Only files under `examples/expenses/` change (`tools/task_result.py` enforces this before the push).

## Acceptance (owner-provided)
`python tasks/T29/check_all.py` runs `check_records.py`, `check_summary.py` and `check_cli.py`, and checks that the
tool imports the standard library only. `tasks/T29/_common.py` is shared by the checks and is an owner file too.

## After a PASS (Claude)
Read the ledger facts above from the run annotations. Open the PR, then review and merge it. Then close stage 3:
- mark stage 3 ✅ in `docs/ARCHITECTURE.md`;
- add T21–T34 to the Excel generator;
- update the progress archive.

## Review notes (written by reviewer)

**Runs.**
- Runs 1–3 never reached work. DeepSeek's API was overloaded: no start within 900 s, then HTTP 503, then the job's
  60-minute limit. This led to T36 (resend when busy), the `m2k-models` ping and a 120-minute job limit.
- Run 4 (`T29-run-4`, GitHub run 38681988141, 16 minutes) PASSED.

**Ledger facts (run 4):**
- `plan.accepted` on the first proposal: `records` → `summary` → `cli`. Each subtask picked exactly its owner check,
  with 11 rounds, 12 calls, 5 commands and 800000 tokens.
- For each subtask: `budget.reserved`, `subtask.started` in its own workspace (`/srv/m2k-ws.subtasks/<id>`),
  `subtask.finished` PASS, and `merge_subtask` with `verified: true`.
- Parent: worker FINISHED ("all 3 subtasks passed and merged"); `test.result` exit 0 (records, summary and cli OK,
  T29 ALL OK); `audit.result` GO; `verdict` PASS.
- Ledger hash chain and anchor OK. Usage: 25 model calls, 20 rounds, 5 commands, 94,671 tokens.
- `task_result.py` released exactly the three files under `examples/expenses/`.

**PASS (Claude, 2026-10-15, after GLM GO), merged as #36.** Claude read the three files and re-ran `check_all.py`: OK. The code is clean, standard library only, and validates
input carefully.

## Handoff note (written by implementer, ≤ 10 lines)
Written by Claude from the run (the card is protected from the workers).
- 大肥鱼 planned three subtasks and built `records.py` (74 lines), `summary.py` (42) and `cli.py` (52).
- GLM reviewed each subtask and the whole; everything passed on the first attempt, with no reassignment and no repair.
- Stage 3 is complete with this card: a model split a real task, and governed workers built it under the full chain.
