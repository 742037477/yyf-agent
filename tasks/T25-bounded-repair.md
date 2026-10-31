# T25: a failed task may be repaired a bounded number of times, from ledger facts

- **Assignee:** Codex (GPT-6.1)
- **Branch:** `task/T25-bounded-repair`
- **Line budget:** ≤ 70 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/contract.py`, `m2k/worker.py`, `m2k/kernel.py` (the single-task path in `run` only), `tests/test_repair.py`, `README.md`
- **Status:** DONE

## Goal
Stage 3. Today a task whose acceptance fails is simply FAIL. With bounded repair, the worker gets a few more chances in
the same workspace.
- Each chance starts from the **facts** of the failure as the ledger recorded them: the acceptance output or the
  reviewer's reasons. It never starts from the worker's own story.
- Each chance produces a new frozen candidate that goes through the whole chain again.
- The number of repairs has a hard limit, and the budget is never reset.
- A task that **cheated** is never repaired. Cheating here means it touched protected files, shadowed modules, wrote
  outside its scope, broke the trust chain, or changed files during the test.

## Required behaviour
1. **Contract.** `[task] max_repairs` (int, 0–5, default 0) → `TaskContract.max_repairs`. Any other value →
   `ContractError`. With 0, behaviour is exactly as today.
2. **Worker.** `run_worker(..., briefing: str = "")`. A non-empty briefing is added to the task prompt under the
   heading `Facts from the ledger (not opinions):`, after the acceptance line. Nothing else changes in the prompt.
3. **Kernel, single-task path** (contracts without subtasks). After a verdict of FAIL, a repair round starts when all
   of these hold:
   - the worker had FINISHED;
   - the test reason is empty (a plain acceptance failure) or the test passed but the review was NO_GO;
   - fewer than `max_repairs` repairs have been done for this task;
   - the model budget still allows a worker call.

   In a repair round:
   - append `repair.started` `{"attempt": n, "candidate": <previous digest>, "facts": <briefing>}`. The briefing is
     built only from ledger events: the last `test.result` (`exit_code`, last 1500 characters of `output`) and, if the
     review ran, the last `audit.result` (`decision`, `reasons`);
   - run the worker again in the same workspace with that briefing;
   - then freeze, run the same checks, test and review as the first attempt.

   The verdict is recorded only once, for the last attempt. Its reasons start with `repair attempts: n`. Every
   attempt's `candidate.frozen`, `test.result` and `audit.result` stay in the ledger. Usage keeps accumulating under
   the same task id.
4. **No repair** for UNKNOWN, for a worker that did not finish, or for any integrity failure:
   `TRUST_*`, `SHADOWED_MODULE`, `PROTECTED_FILE_CHANGED`, `OUT_OF_SCOPE`, `CANDIDATE_CHANGED`.
5. **README**: one row in the task contract table for `max_repairs`.

## Out of scope
- Repair inside subtasks (a subtask keeps `max_repairs = 0`; T24 handles reassignment). Any change to `_run_subtasks`.

## Acceptance
```
python -m unittest tests.test_repair -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
`tests/test_repair.py` (temporary folders, `ScriptedModel` only):
- `max_repairs` parsing: default 0; 0–5 accepted; -1, 6, `"2"`, `true` rejected.
- With `max_repairs = 0`, a failing task is FAIL with one attempt, as today.
- Wrong code, then a repair round that fixes it → PASS.
  - The second worker prompt contains the heading and the acceptance output from the ledger.
  - There are two `candidate.frozen` and two `test.result` events, and one `repair.started` whose `facts` match the
    first `test.result`.
- Review NO_GO, then a repair → the briefing contains the review reasons.
- Repairs stop at the limit: always wrong with `max_repairs = 2` → FAIL after exactly 3 attempts.
- No repair after:
  - a protected file was changed;
  - a shadow module was planted;
  - an UNKNOWN review;
  - a worker that stalled.
- The budget is never reset: a repair round that would exceed `max_model_calls` does not start, or stops as today.
  The ledger usage equals the sum over all attempts.
- The worker's own `finish` summary never appears in the briefing.

## Review notes (written by reviewer)

**Round 1 (PR #26): PASS (Claude, 2026-10-14, after GLM GO), merged as #26.** The branch predated T24; the reviewer
merged main into it and kept T24's `REPEATED_FAILURE` check inside the repair loop. Full suite, offline 21/21 and the T17
owner check green after the merge. Later: an explicit guard before reading the last `audit.result` for the briefing.

## Handoff note (written by implementer, ≤ 10 lines)

- Baseline: `origin/main` at `26e19b1`; unchanged suite passed (352 tests, 9 skipped).
- Added strict `max_repairs` parsing, ledger-fact worker briefings and bounded single-task repair; budgets accumulate.
- Every attempt repeats freeze/check/test/audit; integrity failures and UNKNOWN never repair; only the final verdict is recorded.
- Added 20 ScriptedModel tests covering repair, limits, latest facts, budgets, integrity and unchanged subtask behaviour.
- Verified: `python -m unittest tests.test_repair -v`; full suite (372 tests, 9 skipped); offline acceptance (20/20).
- Scope: listed files plus this handoff only; 64 added lines in `m2k/` (limit 70); `_run_subtasks` unchanged.
- Local verification was on Windows; Linux CI and reviewer approval remain pending. No PR opened.
