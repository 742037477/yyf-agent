# T30: small defensive fixes collected during reviews

- **Assignee:** Codex (GPT-6.1)
- **Branch:** `task/T30-small-defensive-fixes`
- **Line budget:** ≤ 45 added lines in `m2k/` (tests and `tools/` not counted)
- **Files you may change:** `m2k/sandbox.py`, `m2k/cli.py`, `m2k/kernel.py`, `m2k/trust.py`, `m2k/contract.py`, `tools/sign_preflight.py`, `tests/test_small_fixes.py`, `tests/test_recall.py` (the unknown-task assertion only, added by the reviewer)
- **Status:** DONE

## Goal
Reviews of T08–T25 noted small weaknesses. None of them is exploitable today, but each one is a place where bad
input crashes the framework or a message misleads. Fix exactly these ten. Each fix must fail closed: refuse with a
clear message, never silently continue.

## Required behaviour
1. **Sandbox: Python installed at `/`.** If `sys.prefix` or `sys.base_prefix` resolves to `/`, the bubblewrap backend
   is unavailable, with reason `Python is installed at /; binding it would expose the whole file system`.
   `m2k run` then refuses as it does today for an unavailable sandbox.
2. **Config: `[sandbox]` not a table** (e.g. `sandbox = "bwrap"`). `m2k run` exits 2 with
   `配置错误：[sandbox] 必须是一个表，例如 [sandbox] backend = "bwrap"`, before any ledger write.
3. **Config: a non-string `model`** in `[models.worker]` or `[models.reviewer]`. `m2k run` exits 2 with
   `配置错误：[models.worker] 和 [models.reviewer] 的 model 必须是字符串`, instead of crashing on `.strip()`.
4. **Recall of an unknown task.** `kernel.recall` returns `unknown task '<id>'` for a task with no events. "Not a
   subtask" stays only for a task that exists but has no parent.
5. **Status of a task without `task.submitted`.** `kernel.status` (and so `m2k status` / `m2k report`) raises
   `KernelError("unknown task '<id>'")` instead of a `KeyError` or `StopIteration`.
6. **Child credential with a mistyped `writes`.** A `writes` that is not a list of strings fails with `FORMAT` in
   `verify_child_chain`. Today a string is turned into a tuple of characters.
7. **Budget-sum error.** When subtask budgets add up to more than the parent's, the `ContractError` lists the subtask
   ids.
8. **`--untrusted` warning.** Print `警告：未授权运行（没有信任链），已记入账本。` only after the sandbox check has
   passed, so a run refused for its sandbox never claims it was recorded.
9. **Repair briefing.** A repair round starts only when the last `audit.result` exists and its `candidate` equals the
   candidate that just failed. Otherwise stop as if no repair were allowed.
10. **Preflight DNS check** (`tools/sign_preflight.py`). An unexpected exception in the lookup thread, or an empty
    result queue, gives **LOOK** with the reason, never a crash and never PASS.

## Out of scope
- Anything not in this list. The trust test files in `trust/test/` stay byte-for-byte as they are.

## Acceptance
```
python -m unittest tests.test_small_fixes -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
`tests/test_small_fixes.py`: one test per point (temporary folders, `ScriptedModel`, mocks for `sys.prefix`, the
lookup and the platform; no network). Plus one test for T24 left over from review: the facts block a successor
receives contains the exit code of a failed command (`(exit 1)`).

## Review notes (written by reviewer)

**Round 1: the card missed one file.** Point 4 changes the unknown-task message, which `tests/test_recall.py` asserted;
the implementer correctly left it and reported it. The reviewer added the file to the card and updated that one line.

**Round 2 (PR #28): PASS (Claude, 2026-10-14, after GLM GO), merged as #28.** The reviewer merged main (T27) into the
branch; the `writes` type check now sits in the plan-aware `_child_checks`. Full suite and offline 22/22 green.
Later: `[models.*]` given as a scalar, or without `model`, should also be refused cleanly.

## Handoff note (written by implementer, ≤ 10 lines)

- Baseline: origin/main 192e90e; all 388 existing tests passed (9 platform skips).
- Implemented the ten defensive fixes; 21 added lines under m2k/ (budget 45).
- Added 11 offline tests, including T24 successor facts containing a failed command's exit code.
- PASS: python -m unittest tests.test_small_fixes -v (11 tests).
- PASS: python -m m2k accept --offline (21/21 scenarios).
- Full suite: 399 tests, 1 failure, 9 skips; only tests/test_recall.py:51 expects the old unknown-task message.
- That assertion must expect "unknown task 'nobody'"; the file is outside this card, so it remains unchanged pending scope authorization.
- No PR opened; CI and reviewer acceptance remain pending.
