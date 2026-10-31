# T16: every task runs under a credential the whole chain vouches for

- **Assignee:** Claude (core logic; GLM-5.3 GO required, because the author is also the final reviewer)
- **Branch:** `task/T16-trust-in-kernel`
- **Line budget:** ≤ 125 added lines in `m2k/` (tests not counted). Raised from 90 by the author before review: the
  pre-test and pre-delivery re-checks, the worker stop and a shared test-setup helper did not fit; nothing speculative.
- **Files you may change:** `m2k/kernel.py`, `m2k/gates.py`, `m2k/cli.py`, `m2k/accept.py`, `m2k/worker.py` (stop on a trust denial), `m2k/types.py` (one new worker status), `m2k/trust.py` (`dev_setup` helper only), `tests/test_trust_kernel.py`, `README.md`
- **Status:** DONE

## Goal
Stage 2, G0/G1/G2. With `[trust]` configured, a task can only run inside a chain the offline Root vouches for:
Root → charter → (revocations) → credential → this exact contract.
- The framework issues the credential itself at submit, with its issuer key, and never beyond the charter caps.
- The Gate re-verifies the whole chain before **every** action, before TEST and before delivery.
- A revocation, an expiry or any tampering stops the task from doing anything further, and from being delivered.
- Without `[trust]` the kernel behaves as today, and the ledger says so.

## Required behaviour
1. `Kernel(..., trust: TrustSetup | None = None)`. With a setup, at submit, before `task.submitted`:
   - `verify_charter`;
   - the issuer key's public key must equal the charter's `issuer_public_key_b64` (`TrustError("ISSUER_KEY")`);
   - `issue_credential(charter, issuer_key, contract, now, ttl_s)`;
   - then `verify_chain` once.

   Any `TrustError` → `KernelError("trust: <code>: …")`, and nothing is written to the ledger.
2. `task.submitted` records `"trust"`. Without `[trust]` it is `null`. With it:
   `{"domain", "root_fingerprint", "charter_id", "credential_id", "credential_sha256"}`, where `credential_sha256` is
   the sha256 of the canonical credential envelope.
   A new event `credential.issued` stores the credential envelope (public data: no secrets).
3. `TrustChainGate` (name `trust_chain`, first in the chain):
   - before every proposal it calls `verify_chain(...)` with the stored credential, `setup.revocations()` (re-read)
     and `time.time()`;
   - any `TrustError` → deny with reason `<code>: <message>`.
   - The worker stops on the first `trust_chain` denial (new status `TRUST_BROKEN`, reason `trust chain broken: <code>`);
     it does not keep proposing.
4. The kernel re-verifies the chain right after the candidate is frozen, before TEST, and again before G6 delivery.
   - On failure: test verdict FAIL with reason `TRUST_<code>`, no review, no delivery.
   - Reasons list `trust chain broken at <stage>: <code>`.
5. `m2k run` builds the setup with `load_setup` when the config has `[trust]` and passes it to the kernel.
   `m2k status` shows `trust` (domain, charter id, credential id) or `none`.
6. Offline acceptance scenario `凭证在任务中途被撤销`, expected `后续动作全部被拒，不交付`:
   - Setup: fresh TEST_ONLY keys in a sub-folder of the run folder.
   - The scripted worker writes a file, and the scenario's hook then writes a Root-signed revocation of the credential.
   - The worker then tries a second write and `finish`.
   - Pass when the second write is denied by `trust_chain` and nothing is delivered.

## Out of scope
- More attack drills (T17). Signing ceremony (T18). The old test Root drill (T19).
- Requiring `[trust]` for every run. That becomes the default only once T19 has passed.

## Acceptance
```
python -m unittest tests.test_trust_kernel -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
Tests use fresh keys in temporary folders. Required cases:
- A valid setup → PASS. The ledger has `credential.issued`, and `task.submitted.trust` is filled in.
- The issuer key does not match the charter → `KernelError`, and the ledger is empty.
- The charter was revoked before the run → `KernelError`.
- The credential is revoked mid-run → the next action is denied by `trust_chain`, the worker stops, the run is not
  PASS, and nothing is delivered.
- An expired credential (patched clock) → the same result.
- The charter in use is replaced mid-run by another validly Root-signed charter with a different id → denied
  (`WRONG_CHARTER`). The charter file itself is read once, at submit, so editing it later changes nothing.
- Without `[trust]` → the run is unchanged and `trust` is `null`.
- Removing the gate or the pre-TEST re-check makes a test fail.

## Review notes (written by reviewer)

**Round 1 (PR #16): PASS (Claude, 2026-10-14, after GLM GO), merged as #16.** Offline 17/17; each re-check removal fails
tests. Card amendments made before review are ratified. Follow-ups moved to T17.

## Handoff note (written by implementer, ≤ 10 lines)

- Kernel: `trust=` issues the credential at submit (verify_charter → issuer key match → issue → verify_chain), nothing in
  the ledger on refusal; `task.submitted.trust` + `credential.issued`; `TrustChainGate` first in the chain; worker stops
  with `TRUST_BROKEN`; chain re-checked before TEST (FAIL `TRUST_<code>`, no review) and before delivery.
- `m2k run` loads `[trust]`; `m2k status` shows trust. `trust.dev_setup` builds a fresh TEST_ONLY chain for tests/drills.
- Offline scenario 14 `凭证在任务中途被撤销` (17/17). 10 tests; removing the gate, the worker stop, the pre-test or the
  pre-delivery re-check each fails tests. Card edits (budget 125, three extra files, status name) made before review.
