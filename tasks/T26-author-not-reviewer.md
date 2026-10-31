# T26: the author never reviews its own work

- **Assignee:** Codex (GPT-6.1)
- **Branch:** `task/T26-author-not-reviewer`
- **Line budget:** ≤ 25 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/cli.py`, `m2k/kernel.py` (constructor check only), `tests/test_author_reviewer.py`, `tests/test_anchor.py`, `tests/test_ledger_location.py`, `tests/test_sandbox.py`, `tests/test_trust_required.py`, `README.md`
- **Status:** DONE

## Goal
Stage 3. Today `m2k run` only *warns* when the worker and the reviewer are the same model. A model reviewing its own
work is self-grading, so this becomes a refusal. Subtasks (T21) use the same worker and reviewer as their parent, so
this one rule also keeps every subtask's author away from the parent's review.

## Required behaviour
1. **`m2k run` refuses** (exit 2, before any ledger write or model call) when `[models.worker]` and
   `[models.reviewer]` name the same model.
   - "Same" means the `model` values are equal ignoring case and surrounding spaces, or the two built adapters have the
     same `.name`.
   - Message on stderr: `拒绝启动：Worker 和审查者是同一个模型（<name>），不能自己审自己。请在 m2k.toml 给 [models.reviewer] 换一个模型。`
   - The old warning goes away.
2. **`Kernel(...)` refuses** to be built with the very same adapter object as worker and reviewer
   (`worker_model is reviewer_model`): `KernelError("worker and reviewer must be different models")`. Nothing else
   changes in `kernel.py`.
3. **README**: one sentence where models are configured: the reviewer must be a different model from the worker.

## Existing tests
`tests/test_anchor.py`, `tests/test_ledger_location.py`, `tests/test_sandbox.py` and `tests/test_trust_required.py`
stub `build_model` and get two models with the same name.
- In those files, only give the stubbed worker and reviewer different names (for example `ScriptedModel(..., name="worker")`
  and `name="reviewer"`). Change nothing else.
- If another existing test breaks, stop and write it in the handoff note.

## Out of scope
- Checking that two different model names are really different vendors.
- Anything about subtasks beyond the sentence in the goal.

## Acceptance
```
python -m unittest tests.test_author_reviewer -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
`tests/test_author_reviewer.py` (temporary folders, stubbed `build_model`, no network):
- Same `model` in both tables (also with different case or extra spaces) → exit 2, the exact message, no ledger file,
  no model call.
- Different models → the run proceeds as before.
- Two adapters with the same `.name` → exit 2.
- `Kernel(ledger, m, m)` → `KernelError`; `Kernel(ledger, m1, m2)` → fine.

## Review notes (written by reviewer)

**Round 1 (PR #23): PASS (Claude, 2026-10-14, after GLM GO), merged as #23.** Checked merged onto main after T22:
full suite and offline 20/20 green. Later: a non-string `model` value should be refused cleanly, not crash on `.strip()`.

## Handoff note (written by implementer, ≤ 10 lines)

- Baseline: main `d345a47`; 320 tests passed (9 environment skips); new refusal cases reproduced the old self-review behaviour.
- CLI now refuses equal normalized configured model IDs or equal adapter names with exit 2 and the required message, before ledger creation or model calls.
- Kernel constructor rejects only identical adapter objects; README states the reviewer must use a different model.
- Added 7 scripted, temporary-folder tests; the four existing CLI test files already use distinct stub names, so no changes were needed there.
- Verified: T26 tests 7/7; full suite 327 tests OK (9 environment skips); offline acceptance 20/20; diff whitespace check passed.
- Added 7 lines under `m2k/` (budget 25); CI and reviewer approval remain pending; PR creation is left to the reviewer.
