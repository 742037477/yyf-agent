# T17: attack drill — nobody can forge, stretch, move or replace authority

- **Assignee:** DeepSeek-V4-Flash via m2k (`tasks/T17.toml`, GitHub workflow `m2k-task`); GLM-5.3 reviews inside m2k
- **Branch:** `task/T17-trust-attack-drill`
- **Line budget:** ≤ 40 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/accept.py`, `tests/test_trust_attack.py`
- **Status:** DONE

## Goal
Stage 2 attack scenarios for T14–T16. Each attack runs a real task through `Kernel(..., trust=setup)` and shows that it
is refused, using only keys the test creates itself (`m2k.trust.dev_setup`, fresh keys, temporary folders).
Nothing touches the owner's keys or any real ledger.

## Required behaviour
1. `tests/test_trust_attack.py` (standard library `unittest`, temporary folders, `ScriptedModel` only). At least these
   7 tests. Each asserts the exact `TrustError` code, either in the `KernelError` message (refused at submit) or in the
   `trust_chain` gate denial (refused mid-run):
   1. **Forged credential.** Mid-run, `kernel.credential` is replaced by the same body signed with a key that is not the
      charter's issuer → the next action is denied with `CREDENTIAL_SIGNATURE`; the worker status is `TRUST_BROKEN`;
      nothing is delivered.
   2. **Self-granted extra power.** Mid-run, `kernel.credential` is replaced by a credential the real issuer key signed,
      with `max_commands` above the charter cap → `EXCEEDS_CHARTER`.
   3. **Credential moved to another task.** The credential issued for task A is installed into the kernel running task
      B → `WRONG_TASK`.
   4. **Contract edited after issuance.** Mid-run, the kernel verifies against a contract whose goal changed (patch the
      contract the gate closure uses, e.g. by running with `dataclasses.replace` through a wrapper) →
      `CONTRACT_MISMATCH`. If the closure cannot be reached cleanly, assert it through `trust.verify_chain` with the
      stored credential and an edited contract instead.
   5. **Test Root in production.** A setup whose `domain` is `PRODUCTION` but whose pin and charter are `TEST_ONLY` →
      refused at submit with `DOMAIN`; the ledger stays empty.
   6. **Swapped Root pin.** A setup whose pin is another freshly generated key → refused at submit with
      `CHARTER_SIGNATURE`; the ledger stays empty.
   7. **Charter signed by an impostor Root.** Pin and charter both come from the real setup, but the charter is
      re-signed by another key → `CHARTER_SIGNATURE` at submit.
2. `m2k/accept.py`: one new offline scenario.
   - Name: `用伪造的凭证继续干活`; expected text: `被拒，不交付`.
   - Same shape as `s_credential_revoked`, but the hook replaces the credential with a forged one (attack 1 above).
   - Put it in `OFFLINE` right before `s_bytecode`; a protected test pins `OFFLINE[-1]` to `s_bytecode`.
   - Build the kernel with the real worker model directly; do not assign `worker_model` afterwards.

## Out of scope
- Any change to `m2k/trust.py`, `m2k/kernel.py`, `m2k/gates.py` or any other file.
- Attacks on the offline signing ceremony (T18).

## Acceptance (owner-provided, `tasks/T17_check.py`)
```
python tasks/T17_check.py
```
The check script works in four steps:
1. Runs the new tests and requires at least 7, all passing.
2. Re-runs them with `trust.open_envelope` patched to skip signature checks, and again with `trust.verify_chain`
   patched to accept anything. Both must make tests fail.
3. Runs `m2k accept --offline` in a temporary folder and requires every scenario ✅, including the new one placed
   before `Worker 伪造字节码缓存劫持验收`.
4. Runs the full suite.

## Review notes (written by reviewer)

**Round 1 (run T17-run-1, PR #17): CHANGES REQUESTED (Claude, 2026-10-14, agreeing with GLM NO_GO).**
`tests/test_trust_attack.py` is good (13 tests; owner check OK); write it the same way again, the rerun starts from main.
Fix exactly one point in `m2k/accept.py`, `s_forged_credential`:
1. Build the kernel with the real worker model: create the steps list first (the hook may reference `k` before it is
   bound, names resolve at call time) and pass `ScriptedModel([...])` as the worker argument to `Kernel(...)`.
   No `Kernel(..., None, ...)` and no `k.worker_model = ...` afterwards.

**Round 2 (run T17-run-2): PASS (Claude, 2026-10-14, after GLM GO), merged as #17.** Fixed; owner check OK (13 tests;
mutations fail 5 and 7); offline 18/18.

## Handoff note (written by implementer, ≤ 10 lines)
(Written by the reviewer from the run summaries: the card was protected during the run.)
- DeepSeek wrote both files under m2k on GitHub; GLM-5.3 reviewed inside m2k.
- run-1 PASS but rejected (worker model assigned after construction); run-2 fixed it. 13 attack tests + scenario.
