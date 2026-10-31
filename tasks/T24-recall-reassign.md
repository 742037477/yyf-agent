# T24: recall a subtask, reassign it, and let the successor start from ledger facts

- **Assignee:** Claude (core logic; GLM-5.3 GO required, because the author is also the final reviewer)
- **Branch:** `task/T24-recall-reassign`
- **Line budget:** ≤ 160 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/contract.py`, `m2k/trust.py`, `m2k/kernel.py`, `m2k/cli.py`, `m2k/accept.py`, `tests/test_recall.py`, `README.md`
- **Status:** DONE

## Goal
Stage 3, the user's "召回" (2026-10-13). A parent can **recall** a subtask: stop it and withdraw its authority. Then it
**reassigns** the subtask to a successor.
- The successor learns what happened from the **ledger**: what was tried, what failed and why. It never learns it
  from the predecessor's own account.
- The successor never gets more budget than what is left of the subtask's plan.
- A recalled attempt's late result stays in the ledger but is never adopted.
- A successor that hands in exactly the candidate that already failed is failed at once.

## Required behaviour
1. **Plan.** Subtask field `max_reassign` (int 0–3, default 0), checked in `check_subtasks`. Attempt `n` of a subtask
   has:
   - task id `<parent>.<sub>` for n = 1 and `<parent>.<sub>.e<n>` after that (`contract.child_task_id`);
   - workspace `<ws>.subtasks/<sub>` or `<sub>.e<n>`.

   At submit, every possible attempt id must be unused and every possible attempt workspace new or empty.
2. **Recall** (`kernel.recall(ledger, task_id, reason) -> str | None`, also `m2k recall <task_id> [--reason ...]`).
   - It appends `subtask.recalled` `{"task_id", "reason"}` under the parent.
   - It refuses, returning or printing the reason with exit 1, for:
     - an unknown task;
     - a task that is not a subtask;
     - a subtask that already has a verdict;
     - a subtask that was already recalled.

     Nothing is written in those cases.
   - Because recall is a ledger fact, the owner can recall a running subtask from another process.
   - From then on that attempt's chain check fails with `RECALLED`: every action is denied by `trust_chain` (the
     worker stops), the pre-test check and the pre-merge check fail, and nothing of it is merged.
   - This holds with or without a Root trust chain. Every subtask always has the `trust_chain` gate. Without a Root
     chain it checks recall only.
3. **Reassignment.** When an attempt ends without being merged (any reason, including recall), and attempts remain
   (`1 + max_reassign` in total), a successor attempt starts:
   - **Budget.** The plan budget minus what all earlier attempts actually used, per key, never below 0. If
     `max_rounds`, `max_model_calls` or a limited `max_tokens` would be 0, there is no successor. It is reserved
     through `Ledger.reserve` like any attempt.
   - **Workspace.** A fresh copy of the parent workspace. Earlier attempts' workspaces are never merged or reused.
   - **Credential.** Its own child credential with `"epoch": n`. `_child_checks` accepts the epoch task id; the epoch
     must be an int ≥ 1.
   - **Goal.** The subtask goal plus a block `Facts from the ledger about earlier attempts (not opinions):`. The
     block is built only from earlier attempts' ledger events, at most 3000 characters:
     - worker stop status and reason;
     - each effect: tool, path or command, status, exit code;
     - effects whose outcome was UNKNOWN, marked `outcome unknown — that workspace was discarded; do not assume it
       happened`;
     - gate denials;
     - the test verdict, reason and output tail;
     - the review decision and reasons;
     - the recall reason.

     Never the worker's thoughts or `finish` summary.
   - **Repeated failure.** If the successor's frozen files inside its write scope are identical to those of an
     earlier attempt that failed its acceptance (test FAIL with no integrity reason) or got NO_GO, its test fails
     with reason `REPEATED_FAILURE`. The acceptance command does not run.

   `subtask.started` and `subtask.finished` record `"epoch"`. The parent's usage includes every attempt. The parent
   fails (`SUBTASK_FAILED`) only after the last attempt.
4. **Offline scenario** before `s_bytecode`: `子任务被召回后继续动手，改派接手`, expected
   `召回后的动作被拒，前任结果不采用；接手者凭账本事实完成，PASS`.
5. **README**: a short paragraph on recall, reassignment and `m2k recall`.

## Out of scope
- Parallel attempts, recall of the parent itself, any change to `worker.py`, repair inside an attempt (T25).

## Acceptance
```
python -m unittest tests.test_recall -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
`tests/test_recall.py` (temporary folders, `ScriptedModel` only; with and without `dev_setup` where noted):
- **Recall refusals.** Recall of an unknown task, of a top-level task, of a finished subtask, and recalling twice →
  refused, with nothing written.
- **Recall mid-run** (worker hook calls `kernel.recall`), with and without a Root chain:
  - the next action is denied with `RECALLED`;
  - the attempt is not merged;
  - with `max_reassign = 0` the parent is FAIL.
- **Recall from another `Ledger` connection** (as `m2k recall` would) → same result.
- **`m2k recall` CLI** → exit 0 and the event; exit 1 for a refusal.
- **A recalled attempt that still calls `finish` with correct files** → its candidate and test are in the ledger, its
  verdict is not PASS, and nothing is merged.
- **Reassignment after a failed acceptance:**
  - the successor `p1.a.e2` passes and is merged; the parent is PASS;
  - its goal contains the first attempt's test output and its commands, but not the first worker's `finish` summary
    or thoughts;
  - its credential has epoch 2.
- **Budget.** The successor's budget equals the plan minus the first attempt's usage. A first attempt that used
  everything leaves no successor.
- **Repeated failure.** A successor handing in the same failing file → `REPEATED_FAILURE`, and the acceptance command
  is not run.
- **UNKNOWN effect.** A predecessor effect with UNKNOWN outcome (a timed-out command) appears in the facts with the
  warning.
- **Submit checks.** An existing attempt id or a non-empty attempt workspace for any epoch is refused at submit.
- **Mutations.** Removing the recall check, the budget subtraction or the repeated-failure check each makes a test
  fail.

## Review notes (written by reviewer)

**Round 1 (PR #25): PASS (Claude, 2026-10-14, after GLM GO), merged as #25.** Dropping `Kernel.recall` is ratified.
The partial-usage note does not apply: after a `KernelError` no successor starts. Later: a distinct message for an
unknown task in `recall`; a test that exit codes appear in the facts block.

## Handoff note (written by implementer, ≤ 10 lines)
- `contract.py`: `Subtask.max_reassign` (0–3), `child_task_id`. `trust.py`: child credentials carry `epoch`, bound to the
  attempt's task id. `kernel.py`: module `recall()` (ledger fact; refusals write nothing), recall checked inside every
  subtask's chain check (gate always present for subtasks, with or without a Root chain); `_attempt` per epoch with
  reservation, fresh workspace, facts block in the goal, own credential; successor budget = plan − earlier usage;
  `REPEATED_FAILURE` before the test runs; parent usage counts every attempt. `cli.py`: `m2k recall`.
- Author edit before review: dropped `Kernel.recall` (the module function and the CLI cover it) to stay in budget.
- 16 tests in `tests/test_recall.py`; removing the recall check, the recall gate without a Root chain, the budget
  subtraction, the usage accumulation, the repeated-failure check, the attempt-id check or the epoch binding each fails
  tests. Offline 21/21 (new scenario before `s_bytecode`); full suite and T17 owner check OK; 160/160 lines in `m2k/`.
