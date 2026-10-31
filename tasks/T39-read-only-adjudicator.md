# T39: read-only adjudicator — the Gate delivers only what the ledger alone supports

- **Assignee:** Claude (GLM-5.3 GO required)
- **Branch:** `task/T39-read-only-adjudicator`
- **Line budget:** ≤ 110 added lines in `m2k/`
- **Files you may change:** `m2k/adjudicate.py`, `m2k/kernel.py`, `m2k/cli.py`, `tools/task_result.py`, `tests/test_adjudicate.py`, `tests/test_pipeline.py`, `tasks/README.md`
- **Status:** DONE

## Goal
This is stage 5, card 1. The owner decided on 2026-10-15 to separate three roles:
- the kernel works and records;
- a **read-only adjudicator** recomputes the verdict from the ledger and only reports it;
- only the **Gate** releases, and only on the adjudicator's PASS.

Until now the kernel delivered on its own PASS. A kernel bug, or a tampered kernel, could deliver work that the
recorded facts do not support.

## Required behaviour
1. `m2k/adjudicate.py`: `adjudicate(ledger, task_id)` writes nothing. It returns PASS only when all of these hold:
   - the ledger hash chain is intact;
   - the kernel's verdict is PASS, and it names the last frozen candidate;
   - every protected and acceptance file recorded at submit has the same hash in that candidate;
   - the worker FINISHED;
   - the acceptance result for that candidate is PASS with exit 0 and no reason;
   - the review of that candidate is GO with a nonce;
   - when there is a trust chain, the issued credential has the submitted credential id and contract digest;
   - every merged subtask was merged verified, after a PASS, and itself adjudicates PASS.

   A kernel verdict UNKNOWN, an acceptance UNKNOWN, or a review that is neither GO nor NO_GO gives UNKNOWN. Any other
   gap gives FAIL, with the reasons.
2. The kernel's delivery Gate first records `gate.adjudication` (verdict, candidate, reasons), and delivers only on
   PASS. Otherwise it records `delivery.refused` with `adjudication: …`, and the report says so.
3. `m2k adjudicate TASK_ID` prints the adjudication as JSON. Exit 0 only on PASS.
4. `tools/task_result.py` (the GitHub release step) uses the adjudicator, not its own verdict checks.

## Acceptance
`python -m unittest tests.test_adjudicate -v` and the full suite, which covers:
- a lying kernel (it claims PASS over a failed test) is not delivered;
- a PASS appended afterwards changes nothing;
- an edited ledger fails;
- a garbled review gives UNKNOWN;
- a forged credential fails;
- a merged subtask whose facts later fail makes its parent fail;
- the CLI exit codes.

## Review notes (written by reviewer)

**Round 1 (PR #39): PASS (Claude, 2026-10-15, after GLM GO), merged as #39.** Carried forward:
- Append attack, for T41. The adjudicator honours the last event of each kind. Someone who can write the ledger
  could append a whole forged tail (frozen candidate, test, review, verdict) while the hash chain stays valid. Only
  independent OBS evidence and anchors can expose that.
- Untested branches: worker not FINISHED, a verdict naming another candidate, GO without a nonce.

## Handoff note (written by implementer, ≤ 10 lines)
- `m2k/adjudicate.py` (79 lines). The kernel Gate is +9 lines in `_finish`; `m2k adjudicate` is in the CLI;
  `task_result.py` now calls `adjudicate`.
- `tests/test_adjudicate.py`: 7 tests. `tests/test_pipeline.py`: the event order now includes `gate.adjudication`.
  Full suite green; offline scenarios 23/23.
- Not in this card: verifying credential signatures inside the adjudicator (the kernel's chain checks already do it
  at every step), and OBS evidence (T41).
