# T21: task tree skeleton — a parent task runs its subtasks, each under the full chain

- **Assignee:** Claude (core logic; GLM-5.3 GO required, because the author is also the final reviewer)
- **Branch:** `task/T21-task-tree`
- **Line budget:** ≤ 250 added lines in `m2k/` (tests not counted). Raised from 200 by the author before review: the two
  offline scenarios (~40 lines in `accept.py`) were not counted when the budget was set; nothing speculative.
- **Files you may change:** `m2k/contract.py`, `m2k/kernel.py`, `m2k/gates.py`, `m2k/accept.py`, `m2k/types.py` (one new worker status), `tests/test_task_tree.py`, `README.md`
- **Status:** DONE

## Goal
Stage 3 starts. A task contract can list subtasks. The kernel runs them **one after another**; each one is a real task
with its own workspace, its own write scope, its own acceptance and its own independent review.
- Only a subtask that PASSes is merged back into the parent workspace, and only the files inside its write scope.
- When every subtask is merged, the parent runs the owner's overall acceptance and an independent review of the
  combined result, and only then delivers.
- The subtask list is written in the contract here. A model proposing the split comes in T27; delegated child
  credentials in T22; budget reservation in T23; recall and reassignment in T24.

Owner decisions (2026-10-14):
- A subtask's acceptance can only use acceptance files the task owner provided.
- Subtasks run sequentially.

## Required behaviour
1. **Contract** (`[[subtasks]]` in task.toml, parsed into `TaskContract.subtasks`). Fields of each subtask:
   - `id`: matches `[a-z0-9][a-z0-9_-]{0,31}`, unique.
   - `goal`: non-empty.
   - `writes`: a non-empty list of workspace-relative paths. A path ending in `/` is a folder prefix, anything else
     is one exact file. It may not be absolute, contain `..`, or name a protected or acceptance file.
   - `acceptance`: a command.
   - `acceptance_files`: non-empty, a subset of the parent's `[acceptance].files`, and the command must use one of
     them.
   - `budget`: required keys `max_rounds`, `max_model_calls`, `max_commands`; `max_tokens` is required when the
     parent sets it.
   - `tools`: optional, a subset of the parent's tools.
   - `network`: optional, `true` only if the parent has `network = true`.

   For every budget key the parent sets, the sum over the subtasks is ≤ the parent's value. Any violation is a
   `ContractError` naming the subtask. Without `[[subtasks]]` nothing changes.
2. **`TaskContract.writes`** (new, default empty = unrestricted).
   - A new gate `write_scope` (in `DEFAULT_GATES`, after `protected_path`) denies `write_file` / `edit_file` outside
     `writes`.
   - After a task with `writes` is frozen, any file added, changed or removed outside `writes` (compared with its
     baseline; this catches command side effects) fails the test with reason `OUT_OF_SCOPE`, like protected files.
3. **Running a parent with subtasks** (`Kernel.run`):
   - Submit as today: the same checks, credential and `task.submitted`, which also records the subtask plan.
   - The parent has no worker of its own. For each subtask in order:
     1. Copy the parent workspace's current files into a fresh child workspace at `<parent workspace>.subtasks/<id>`.
        It must not exist yet or must be empty; it is outside the parent workspace.
     2. Run the child task `<parent id>.<sub id>` through the normal single-task chain: worker, gates, freeze, test
        with the subtask's acceptance, independent review, verdict. Child settings:
        - `goal` is the subtask goal plus one line naming the parent goal;
        - `protected` and constraints are the parent's, plus a constraint naming the write scope;
        - `writes`, budget, tools and network come from the subtask;
        - no delivery.

        The child's `task.submitted` records `"parent": <parent id>`.
     3. With a trust chain, the child acts under the **parent's** credential:
        - every child action, the pre-test check and the merge re-verify the parent chain against the parent
          contract;
        - the child's `task.submitted.trust` copies the parent record plus `"inherited_from": <parent id>`.
     4. If the child's verdict is PASS and the chain still holds, merge into the parent workspace exactly the files
        inside the subtask's write scope that the child changed, added or removed. This is an effect: write
        `effect.intent` / `effect.result` with tool `merge_subtask` and the same op_id, and check that the merged
        files now match the child's frozen candidate.
     5. Otherwise stop with the new worker status `SUBTASK_FAILED`. The parent verdict is FAIL, or UNKNOWN when the
        child was UNKNOWN. The reason names the subtask. Later subtasks do not run and nothing is delivered.
   - After all subtasks are merged:
     - write `worker.stopped` (FINISHED, `all N subtasks passed and merged`);
     - then run the normal parent path: freeze, protected / shadow / scope checks, the owner's overall acceptance,
       independent review of the combined diff, verdict, G6/G7 delivery.
   - The parent's review is a model call. It is allowed only while parent usage **plus all its children's usage** is
     within the parent budget; otherwise it is UNKNOWN as today.
4. **`m2k status <parent>`** adds `"subtasks": [{"id", "task_id", "verdict"}]`.
5. **Offline acceptance**: two new scenarios, placed before `s_bytecode` (`OFFLINE[-1]` stays `s_bytecode`):
   - `组合任务：两个子任务通过后合并，总验收通过`, expected `PASS，交付合并后的结果`.
   - `子任务写出自己的范围`, expected `写入被拒；命令绕道写入 → 子任务 FAIL，不合并、不交付`.
6. **README**: one short paragraph under the task contract section, with a `[[subtasks]]` example.

## Out of scope
- Child credentials (T22), reservation events (T23), recall and reassignment (T24), repair (T25), forced author ≠
  reviewer (T26), a model proposing the split (T27), parallel execution.

## Acceptance
```
python -m unittest tests.test_task_tree -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
`tests/test_task_tree.py` (temporary folders, `ScriptedModel` only) must cover:
- **Parsing.** A valid plan parses. Each rule in point 1 rejects a bad plan: duplicate id, bad id, empty `writes`,
  `..` or absolute path, writing a protected or acceptance file, acceptance file not owned by the parent, command not
  using its files, budget over the parent in sum, tools not in the parent's, network without the parent's.
- **Two subtasks pass.** Both merge, the parent acceptance passes and the result is delivered. The ledger shows two
  `merge_subtask` intent/result pairs, and the child tasks have `parent`.
- **First subtask fails its acceptance.** The parent verdict is FAIL, the second subtask never starts, the parent
  workspace is unchanged and nothing is delivered.
- **Scope.**
  - `write_file` outside the scope is denied by `write_scope`.
  - A `run_command` that writes outside the scope makes the child FAIL with `OUT_OF_SCOPE`, and nothing is merged.
- **Merge is exact.** A child that also edits a file outside its scope through a command is not merged at all (FAIL).
  A child that deletes a file inside its scope deletes it in the parent.
- **Combined acceptance.** Both subtasks pass their own checks, but the parent's overall check fails → parent FAIL,
  not delivered.
- **Budget.** Children that used up the parent's model-call budget → the parent review is UNKNOWN, not delivered.
- **Trust.** With `dev_setup`:
  - the children run under the parent credential (`inherited_from` recorded);
  - revoking the parent credential after the first merge stops the second subtask's first action (`trust_chain`),
    and nothing is delivered.
- **Unchanged behaviour.** A contract without subtasks runs exactly as before; the full suite stays green.

## Review notes (written by reviewer)

**Round 1 (PR #21): GLM NO_GO, one blocking point, fixed.** The README `[[subtasks]]` example did not name its
acceptance file in the command; it now does, and a test parses the README example. The README limits line is updated.

**Round 2: PASS (Claude, 2026-10-14, after GLM GO), merged as #21.** Card amendments (budget 250, `types.py`) ratified.
Offline 20/20; 19 tests in `tests/test_task_tree.py`. Later: name the subtasks in the budget-sum error; a write
inside a protected folder is caught by the protected gate, not by `check_subtasks`.

## Handoff note (written by implementer, ≤ 10 lines)
- `contract.py`: `Subtask`, `TaskContract.writes/subtasks`, `[[subtasks]]` parsing with every rule of point 1; `in_scope`.
- `gates.py`: `write_scope` gate. `types.py`: `SUBTASK_FAILED`. `kernel.py`: `run()` runs subtasks in order in
  `<ws>.subtasks/<id>` under the parent credential, merges only in-scope changes of a PASS (`merge_subtask` intent/result,
  verified), stops on the first non-PASS; `OUT_OF_SCOPE` check after freeze; parent review budget counts children's
  usage; baseline per run (was a kernel attribute); `status` lists subtasks.
- Plan rules live in `check_subtasks` (objects), run at parse time and again by the kernel before submit, so a plan
  built in code cannot widen tools, network, scope or budget.
- 18 tests in `tests/test_task_tree.py`; removing the scope gate, the scope check, the merge delete, the usage roll-up,
  the UNKNOWN mapping, the pre-merge chain check, the trust gate or the kernel re-check each fails a test.
- Offline 20/20 (two new scenarios before `s_bytecode`); T17 owner check OK; full suite OK.
