# T23: a subtask's budget is reserved atomically before it starts

- **Assignee:** Codex (GPT-6.1)
- **Branch:** `task/T23-budget-reservation`
- **Line budget:** ≤ 70 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/ledger.py`, `m2k/kernel.py` (`_run_subtasks` only), `tests/test_budget_reservation.py`
- **Status:** DONE

## Goal
Stage 3. Today the subtask budgets are only checked on paper: their sum must not exceed the parent's (T21). Recall
and reassignment (T24) will start extra subtasks at run time, and parallel runs will come later. So every subtask must
**reserve** its budget from the parent's pool in the ledger before it starts.
- The check and the reservation happen in one database transaction. If two runs compete for the last share, exactly
  one of them gets it.
- When a subtask ends, its reservation is released, and from then on only what it really used counts.

## Required behaviour
1. **`Ledger.reserve(parent_id, child_id, amounts, limits) -> bool`** (`amounts` and `limits` map budget keys to ints;
   a limit of `None` means no limit for that key). Inside **one** `BEGIN IMMEDIATE` transaction:
   1. For each limited key, compute what is already committed:
      - the parent's own usage (`usage(parent_id)`);
      - for every child already reserved under this parent: its actual usage if it was released, otherwise its
        reserved amount.
   2. If `committed + amounts[key] <= limits[key]` for every limited key, append `budget.reserved`
      `{"child": child_id, "amounts": amounts}` under `parent_id` and return True.
   3. Otherwise append `budget.refused` `{"child", "amounts", "short": {key: amount missing}}` and return False.
   4. Reserving a child that was already reserved under this parent returns False and records nothing new.

   The events go into the same hash chain as every other event. Refactor `append` so the insert can run inside an
   already-open transaction; do not duplicate the hashing code.
2. **`Ledger.release(parent_id, child_id)`** appends `budget.released` `{"child": child_id}` once. A second call, or a
   call for a child that was never reserved, does nothing.
3. **Kernel** (`_run_subtasks`):
   - before a subtask starts (before its workspace is copied), reserve its budget against the parent contract's
     budget;
   - if the reservation is refused, the subtask fails without starting: worker status `SUBTASK_FAILED`, reason
     `subtask <id>: budget reservation refused (<short>)`, parent verdict FAIL;
   - after the subtask's run ends, release it, whatever happened (PASS, FAIL, UNKNOWN or a `KernelError`).

## Out of scope
- Reassignment or recall (T24), parallel execution, any change to how `usage()` is computed.

## Acceptance
```
python -m unittest tests.test_budget_reservation -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
`tests/test_budget_reservation.py` (temporary folders, `ScriptedModel` only):
- A reservation within the limits → True, with one `budget.reserved` event.
- Over the limits → False, with `budget.refused` and its `short`; nothing else is recorded.
- A limit of `None` is ignored.
- The same child reserved twice → False; release twice → only one `budget.released`.
- After release only actual usage counts:
  - reserve 5 calls;
  - the child uses 2;
  - release;
  - a new reservation of `limit - 2` now fits.
- **The race.** 20 rounds. In each round two threads, each with its **own** `Ledger` connection to the same file and
  synchronised by a `threading.Barrier`, reserve amounts that fit alone but not together. Every round exactly one
  gets True, and after all rounds `verify_chain()` is OK.
- **Kernel.**
  - A two-subtask run records, in order: `budget.reserved` for a, then a's `task.submitted`, then `budget.released`
    for a; then the same for b.
  - With `Ledger.reserve` patched to refuse, the parent is FAIL with the reservation reason, and the child never
    starts (no events under its id).
  - A child that raises `KernelError` is still released.

## Review notes (written by reviewer)

**Round 1 (PR #24): PASS (Claude, 2026-10-14, after GLM GO), merged as #24.** Checked merged onto main: full
suite and offline 20/20 green; the race test passed 5 repeated runs, and removing the transaction lock in `reserve`
makes it fail in every round. Later: index budget events if parents grow large.

## Handoff note (written by implementer, ≤ 10 lines)

- Based on main `aebfc0e`; its 25 ledger/task-tree tests passed before implementation.
- Added atomic reserve/release transactions sharing the existing event hash-chain insert.
- Subtasks reserve before workspace copying; refusal fails the parent; all exit paths release.
- Added 14 tests, including 20 two-connection race rounds, usage accounting and lifecycle failures.
- Passed all acceptance commands: targeted 14 tests; full suite 352 tests (9 skipped); offline 20/20 scenarios.
- Added 62 lines under `m2k/`; only card-listed files and this handoff changed. Remote CI/reviewer approval pending.
