# T22: every subtask gets its own credential, and it can only be narrower than its parent's

- **Assignee:** Claude (core logic; GLM-5.3 GO required, because the author is also the final reviewer)
- **Branch:** `task/T22-child-credentials`
- **Line budget:** ≤ 120 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/trust.py`, `m2k/kernel.py`, `tests/test_trust_child.py`, `tests/test_task_tree.py` (the `inherited_from` assertion only), `README.md`
- **Status:** DONE

## Goal
Stage 3. In T21 a subtask acts under its parent's credential. Now the framework issues each subtask its **own** child
credential at the moment the subtask starts, and the Gate verifies the whole chain before every child action:
Root → charter → parent credential → child credential → child contract.
- A child credential can only be narrower than its parent:
  - tools, budget and network no larger;
  - a write scope inside the parent's;
  - an expiry no later than the parent's;
  - exactly what the parent's plan says for that subtask.
- Revoking the parent stops every child. Revoking one child stops only that child.

## Required behaviour
1. **`trust.issue_child_credential(issuer_key, parent_body, parent_contract, child_contract, subtask_id, now)`** signs
   a credential with the same schema as today, plus `parent_credential_id`, `subtask` and `writes`. Its fields:
   - `charter_id`: the parent's;
   - `not_before`: `now`;
   - `expires_at`: the parent's `expires_at`;
   - tools, budget, network and writes: the child contract's.

   It refuses to sign anything the verifier below would reject for being too wide (`TrustError` with that code).
2. **`trust.verify_child_chain(pin, domain, charter_env, parent_env, child_env, revocations_env, parent_contract,
   child_contract, now, code_dir)`** returns the child body or raises `TrustError`.
   - First the parent chain, exactly as `verify_chain`. Any parent failure is re-raised with the code prefixed
     `PARENT_` (e.g. `PARENT_CREDENTIAL_REVOKED`).
   - Then the child, checked in this order:
     1. `CREDENTIAL_SIGNATURE`: signed by the charter's issuer, credential schema, same trust domain.
     2. `CREDENTIAL_REVOKED`: its id is in the Root's revocation list.
     3. `WRONG_PARENT`: `parent_credential_id` or `charter_id` differs from the parent's, or `subtask` is not in the
        parent contract's plan.
     4. `NOT_YET_VALID` / `EXPIRED`.
     5. `WRONG_TASK`: `task_id` is not the child contract's id or not `<parent id>.<subtask>`.
     6. `CONTRACT_MISMATCH`: the child contract's digest differs.
     7. `EXCEEDS_PARENT`: tools, budget or network above the parent credential; expiry after the parent's; `writes`
        empty or not inside the parent contract's write scope (an empty parent scope = the whole workspace).
     8. `EXCEEDS_PLAN`: tools, budget or network above the plan entry, or `writes` different from it.
     9. `EXCEEDS_CREDENTIAL`: the child contract asks for more than the child credential, or a different scope.
   - Missing or mistyped fields → `FORMAT`, as in `verify_chain`.
3. **Kernel.** With a trust chain, when a subtask starts:
   - issue its child credential (after re-verifying the parent chain);
   - record `credential.issued` under the child task id;
   - set the child's `task.submitted.trust` to `{domain, root_fingerprint, charter_id, credential_id,
     credential_sha256, parent_credential_id}`. This replaces T21's `inherited_from`.

   Then:
   - the child's `trust_chain` gate, its pre-test check, and the check before the merge all call
     `verify_child_chain`;
   - the parent keeps verifying its own chain as today;
   - if issuing fails, or a child run raises `KernelError`, that subtask counts as failed (`SUBTASK_FAILED`, parent
     FAIL) instead of leaving the parent without a verdict.
4. **README** (trust section): two sentences on child credentials.

## Out of scope
- Recall by the framework (issuer-signed recall, epochs): T24. Budget reservation: T23. Nested subtasks of subtasks.

## Acceptance
```
python -m unittest tests.test_trust_child -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
`tests/test_trust_child.py` (fresh keys via `dev_setup`, temporary folders, `ScriptedModel` only):
- **Library.** A child credential verifies.
- Each code in point 2 is produced by a minimal change:
  - signed by another key;
  - revoked child id;
  - another parent id;
  - a subtask not in the plan;
  - a patched clock before or after;
  - another task id;
  - an edited child contract;
  - re-signed with the real issuer key but with more tools, a bigger budget, network on, a later expiry, an empty or
    foreign write scope;
  - a plan entry narrower than the credential;
  - the parent revoked (`PARENT_CREDENTIAL_REVOKED`).
- **Issuing.** `issue_child_credential` refuses a child contract wider than the parent.
- **Kernel.**
  - Two subtasks under a chain → PASS. Each child has its own `credential.issued`, and its `task.submitted.trust`
    names the parent credential.
  - Revoking one child mid-run → that child's next action is denied (`trust_chain`); parent FAIL; nothing merged
    from it.
  - Revoking the parent mid-run → the running child is denied with `PARENT_CREDENTIAL_REVOKED`.
  - A child credential swapped mid-run for one re-signed with a wider budget → denied (`EXCEEDS_PARENT` or
    `EXCEEDS_PLAN`).
  - Removing the child gate check or the pre-merge child check makes a test fail.

## Review notes (written by reviewer)

**Round 1 (PR #22): PASS (Claude, 2026-10-14, after GLM GO), merged as #22.** The extra one-line edit in
`tests/test_task_tree.py` (child `credential.issued`) and `verify_chain` refusing standalone child credentials are
ratified. Later: a mistyped `writes` should be `FORMAT`; child credentials are in memory only (matters for stage 4 resume).

## Handoff note (written by implementer, ≤ 10 lines)
- `trust.py`: `issue_child_credential` (refuses anything too wide) and `verify_child_chain` (parent first, failures as
  `PARENT_<code>`, then the nine child checks in card order); `verify_chain` now refuses a child credential standing
  alone (`WRONG_PARENT`).
- `kernel.py`: each subtask gets its own credential at start (`credential.issued` under the child id, trust record with
  `parent_credential_id`); its gate, pre-test check, delivery check and the pre-merge check verify the child chain;
  an issuing failure or a child `KernelError` fails the subtask (`SUBTASK_FAILED`). `_chain_problem` → `_problem(verify)`.
- 11 tests in `tests/test_trust_child.py` (every code, issuing refusals, four kernel cases); the T21 `inherited_from`
  assertion now checks the child's own credential. Seven mutations (gate, pre-merge check, child-as-parent check, expiry
  bound, plan scope, standalone child, issuing failure) each fail tests.
- 106/120 lines in `m2k/`; full suite OK; offline 20/20; T17 owner check OK.
