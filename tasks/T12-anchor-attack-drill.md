# T12: attack drill — rewrite the ledger and recompute the whole chain

- **Assignee:** DeepSeek-V4-Flash via m2k (`tasks/T12.toml`, GitHub workflow `m2k-task`); GLM-5.3 reviews inside m2k
- **Branch:** `task/T12-anchor-attack-drill`
- **Line budget:** ≤ 40 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/accept.py`, `tests/test_anchor_attack.py`
- **Status:** DONE

## Goal
Stage 1.5, attack scenario for T09. The hash chain only catches a careless edit. Someone who can write the
ledger file can change an event and recompute every hash after it. `verify_chain` then reports OK, and only the
external anchors (T09, `m2k/anchor.py`) reveal the rewrite. This card proves that with real tests and an
`m2k accept` scenario. The drill rewrites only a ledger the test itself created in a temporary folder.

## Required behaviour
1. `tests/test_anchor_attack.py` (standard library `unittest`, temporary folders, `ScriptedModel` only), at least 3 tests:
   - **Rewrite and recompute.** Build a small ledger, for example by running one task through `Kernel` with scripted
     models, then append an anchor with `anchor.append`. In SQLite, change the payload of the task's `verdict`
     event. Then walk the events in `seq` order and recompute that event's hash and every later event's hash with
     `m2k.ledger._event_hash(prev, task_id, kind, op_id, payload, ts)`. Here `prev` is the hash of the previous
     event, already recomputed. Store both the new `prev_hash` and the new `hash`.
     Assert that `ledger.verify_chain()` returns OK. Assert that `anchor.check(...)` reports
     `ledger rewritten at anchored seq N`, where N is the anchored seq.
   - **Truncate and stay consistent.** Delete the last events after an anchor; the remaining chain is still
     consistent. Assert that `verify_chain()` is OK and that `anchor.check` reports `ledger truncated`.
   - **Untouched control.** The same steps without any change give `anchor.check(...) == []`.
2. `m2k/accept.py`: a new offline scenario that runs after the existing tamper scenario, on its own small bench
   in a sub-folder of the run folder, so it never touches the shared bench ledger.
   - Name: `有人改写账本并重算整条哈希链`.
   - Expected text: `普通校验发现不了；对照外部锚点发现改写`.
   - `ok` only when `verify_chain` is OK after the rewrite and `anchor.check` reports the rewritten seq.
   - Append it in `run_accept` after `s_tamper`.

## Out of scope
- Any change to `m2k/anchor.py`, `m2k/ledger.py` or any other file.
- Signing the chain head (stage 5).

## Acceptance (owner-provided, `tasks/T12_check.py`)
```
python tasks/T12_check.py
```
It runs the new tests and requires at least 3 tests that pass. It then re-runs them twice:
- with `anchor.check` patched to return `[]`, which must make tests fail (they rely on the anchors);
- with `Ledger.verify_chain` patched to report a break, which must make tests fail (the recompute is real).

Finally it runs `m2k accept --offline` in a temporary folder and requires every scenario ✅ including the new one,
then runs the full suite.

## Review notes (written by reviewer)

**Round 1 (run T12-run-2, PR #11): CHANGES REQUESTED (Claude, 2026-10-13).** `tests/test_anchor_attack.py` is good; write it the same way again (the rerun starts from main).
Fix exactly these three points in `m2k/accept.py`:
1. The scenario rewrites the shared bench ledger (`b.ledger`). It must build its own bench in a sub-folder of the
   run folder, e.g. `Bench(b.base / "anchor-drill")`, and close that ledger when done (Windows keeps the file locked).
2. It must not be in `OFFLINE` (a protected test pins `OFFLINE[-1]` to `s_bytecode`). Append it in `run_accept`
   right after `results.append(s_tamper(bench))`.
3. The expected text must be exactly `普通校验发现不了；对照外部锚点发现改写`.
`tasks/T12_check.py` now checks all three.

**Round 2 (run T12-run-3): PASS (Claude, 2026-10-13, after GLM GO), merged as #11.** All three points fixed; owner check OK;
offline 13/13. Note for the workflow: pushes by the m2k-task bot do not trigger PR checks; reopen the PR to run them.

## Handoff note (written by implementer, ≤ 10 lines)
(Written by the reviewer from the run summaries: the card was protected during the run.)
- DeepSeek wrote both files under m2k on GitHub; GLM-5.3 reviewed inside m2k.
- run-1 FAIL: round budget exhausted, no command run. run-2 PASS but rejected in review (shared bench, wrong place,
  wrong expected text). run-3 fixed all three: 3 tests + scenario 13, owner check OK.
