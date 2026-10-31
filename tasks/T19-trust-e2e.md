# T19: end-to-end drill — a real task on GitHub under the old TEST_ONLY Root

- **Assignee:** Part A Codex (on the owner's computer) · Part B owner (one GitHub secret) · Part C Claude (workflow + run)
- **Branch:** `task/T19-trust-e2e`
- **Line budget:** ≤ 5 added lines in `m2k/` (tests not counted). Raised from 0 by the author before review: Part A
  stopped at `ROOT_PIN` because the old pin stores the full 64-hex sha256 as `fingerprint`; `load_pin` now accepts
  that form too (pin check only, nothing else in `m2k/`).
- **Files you may change:** `trust/test/root-pin.json`, `trust/test/charter.json`, `trust/test/issuer-pin.json`, `trust/test/caps.toml`, `.github/workflows/m2k-task.yml`, `tasks/T19.toml`, `tasks/T19_check.py`, `README.md`, `m2k/trust.py` (`load_pin` fingerprint check only), `tests/test_trust.py`, `examples/trust_demo/greet.py` (written by the Part C run)
- **Status:** DONE

## Goal
Stage 2 finish. One real task (DeepSeek works, GLM reviews) runs on GitHub under the full chain:
- the **old project's TEST_ONLY Root**, public key fingerprint `6357ec2addf1fd69`, signs a framework charter on the
  owner's computer;
- the workflow's framework issuer signs a task credential;
- the Gate re-verifies the chain before every action.

The ledger of that run must show `trust.root_fingerprint = 6357ec2addf1fd69`. This proves the signing design works
with the existing test Root, and that the only thing left for production is the offline signing with the real Root.

Rules:
- The Root private key stays on the owner's computer. It never enters the repository or CI.
- The issuer private key is TEST_ONLY and lives only in the owner's folder and one GitHub secret.
- Only public files are committed.

## Part A — Codex, on the owner's computer (`<repo>`)
1. `git pull`, then `git switch -c task/T19-trust-e2e`.
2. Make a folder outside the repository, `<trust-test>\`, and in it:
   - `python -m m2k trust keygen --role issuer --out <trust-test>\issuer`
   - copy `<old-project-public>\CK-AB-01\pin_path.json` to `<trust-test>\root-pin.json` unchanged.
3. Write `<trust-test>\caps.toml`:
   `tools = ["list_files","read_file","write_file","edit_file","run_command"]`, `max_rounds = 40`,
   `max_model_calls = 44`, `max_tokens = 3000000`, `max_commands = 20`, `network = false`.
4. Sign the charter with the old test Root:
   `python -m m2k trust sign-charter --root-key <old-project-private>\root-test-only.pem --issuer-pin <trust-test>\issuer-pin.json --caps <trust-test>\caps.toml --domain TEST_ONLY --out <trust-test>\charter.json`
   (release `dev`). Do not print, copy or open the Root key for any other purpose.
5. Write `<trust-test>\m2k.toml` with only a `[trust]` table pointing at those files, then run
   `python -m m2k --config <trust-test>\m2k.toml trust status`. It must exit 0, show Root fingerprint
   `6357ec2addf1fd69`, and show `issuer_key matches: true`.
   - If loading the old pin or the old Root key fails, stop. Report the exact error and the pin's field names (never
     the key values) in the handoff note.
   - Do not work around a failure.
6. Copy only the public files into the repository: `root-pin.json`, `charter.json`, `issuer-pin.json` and `caps.toml`
   go into `trust/test/`. Commit `T19: TEST_ONLY trust chain signed by the old test Root (public files)` and push the
   branch. Never commit `issuer.pem` or anything under `<old-project-private>`.

## Part B — owner, GitHub web page (once)
Settings → Secrets and variables → Actions → New repository secret:
- name `M2K_TEST_ISSUER_KEY`;
- value: the whole content of `<trust-test>\issuer.pem`.

This key is TEST_ONLY; it can be revoked at any time by signing a revocation list with the test Root.

## Part C — Claude, on the same branch
1. `m2k-task.yml`: when the secret `M2K_TEST_ISSUER_KEY` is set and `trust/test/charter.json` exists:
   - write the key to `/root/m2k/trust/issuer.pem` (root-only, mode 600);
   - copy the three public files from the pristine checkout to `/root/m2k/trust/`, never from the task workspace;
   - add `[trust]` (TEST_ONLY) to the generated config.

   With no secret the workflow behaves as today.
   The isolation proof step also shows that a sandboxed command cannot see `/root/m2k/trust`.
2. `tasks/T19.toml` + `tasks/T19_check.py`: a tiny real task with an owner check.
   - The task: create `examples/trust_demo/greet.py` with `greet(name)` returning `"hello, <name>"`.
   - The owner check imports it, asserts the result and runs the full suite.
3. Dispatch `m2k-task` with `tasks/T19.toml` on this branch. The run is accepted when:
   - the verdict is PASS;
   - `m2k status` shows `trust.domain = TEST_ONLY` and the charter id from `trust/test/charter.json`;
   - the ledger's `task.submitted.trust.root_fingerprint` is `6357ec2addf1fd69`;
   - `credential.issued` is present.
4. `README.md`: a short paragraph on how the TEST_ONLY chain in `trust/test/` was made, and that production needs a
   new charter signed offline with the real Root (preflight → USB → `sign-charter --domain PRODUCTION`).

## Out of scope
- The production Root, the production charter, making `[trust]` mandatory (decided after this card).

## Acceptance
- Part A: `m2k trust status` exits 0 on the owner's computer, and the handoff note shows its output, which contains no
  key material.
- Part C: ci green; the dispatched run meets point 3 above; the ledger summary is pasted into the review notes.

## Review notes (written by reviewer)

**Round 1 (Part A): BLOCKED, resolved by the author (Claude, 2026-10-14).** Codex stopped correctly at `ROOT_PIN`.
The old pin's `fingerprint` is the full sha256 of the key (64 hex), while T15 required the 16-hex form. `load_pin` now
accepts either; any other value is still `ROOT_PIN` (test added). The real old pin loads as `TEST_ONLY 6357ec2addf1fd69`.
Codex: resume Part A from step 4 on this branch (the issuer files in `<trust-test>\` can be kept).

**Round 2 (Part A resumed): PASS (Claude, 2026-10-14).** The charter verifies against `trust/test/root-pin.json`
(identical to the old project's pin); its issuer key equals `issuer-pin.json`; no private key in the repository.

**Round 3 (Part C run `T19-run-2`, GitHub, DeepSeek worker, GLM-5.3 reviewer): meets point 3.** Ledger summary:
`verdict PASS` (test PASS, review GO); `trust = {domain TEST_ONLY, charter_id 53495ac89d74adca944ce6457356e554,
credential_id 557ac43a278bcd468310f7a84d80b847}`; `task.submitted.trust.root_fingerprint = 6357ec2addf1fd69`;
`credential.issued` 1; sandbox bwrap, network false; ledger hash chain OK; anchors OK. `T19-run-1` ran before the
secret existed (`trust: null`) and does not count. Two workflow fixes found on the way: `git switch -C` for a dispatch
on the card branch; a failing `trust status` is shown as an annotation with the key's shape, never its content.

**Final: PASS (Claude, 2026-10-14, after GLM GO), merged as #19.** Later: trailing newlines in `trust/test/*.json`
(cosmetic); stray spaces in the key-shape annotation (cosmetic).

## Handoff note (written by implementer, ≤ 10 lines)
- Part A blocked on baseline `origin/main` (`adfe108`); working branch: `task/T19-trust-e2e`.
- Synced main after the initial pull found the deleted T18 upstream; no code changes.
- Generated TEST_ONLY issuer files in `<trust-test>/`; copied the old Root pin byte-for-byte unchanged.
- Loading that pin failed before signing: `m2k.trust.TrustError: ROOT_PIN: pin fingerprint does not match the key`.
- Pin field names only: `fingerprint`, `public_key_b64`, `schema`, `trust_domain`.
- Stopped per Part A: Root private key not opened; caps/config/charter not created; no public trust files committed.
- Acceptance `trust status` not run because pin loading failed; full suite: 292 tests, OK (9 skipped).
- Source pin needs reviewer resolution before Part A can resume; no workaround attempted; Parts B/C not started.

## Handoff note — Part A resumed (2026-10-14, ≤ 10 lines)
- Resumed on `1afd944` after Round 1; the previous `ROOT_PIN` failure record above is retained.
- Kept the existing issuer files; verified the old Root pin is unchanged from its source and loads with the expected fingerprint.
- Created caps/config outside the repository; used the old TEST_ONLY Root only for `sign-charter --release dev`.
- Acceptance: `python -m m2k --config <trust-test>/m2k.toml trust status` exited 0; output (whitespace compacted):
- `release: dev`
- `{"domain":"TEST_ONLY","Root fingerprint":"6357ec2addf1fd69","charter id":"53495ac89d74adca944ce6457356e554","issuer fingerprint":"7852708f1eb43dea","caps":{"max_commands":20,"max_model_calls":44,"max_rounds":40,"max_tokens":3000000,"network":false,"tools":["list_files","read_file","write_file","edit_file","run_command"]},"revocation list version":null,"issuer_key matches":true}`
- Full suite: `python -m unittest discover -s tests -t .` — 293 tests, OK (9 skipped).
- Added only the four public files in `trust/test/`; issuer private key and local config stay outside the repository.
- Parts B/C not started; Part A awaits CI and reviewer approval.

## Handoff note — Part C (Claude, ≤ 10 lines)
- `m2k-task.yml`: with secret `M2K_TEST_ISSUER_KEY` and `trust/test/charter.json`, stages the issuer key root-only in
  `/root/m2k/trust`, copies the public files from the pristine checkout, appends `[trust]` TEST_ONLY, runs `trust status`;
  the sandbox proof also shows `/root/m2k/trust` is invisible; the summary lists `task.submitted.trust` from the ledger.
- `tasks/T19.toml` + `tasks/T19_check.py` (greet task); `examples/trust_demo/greet.py` written by DeepSeek in run-2.
- README: how `trust/test/` was made; production needs a new charter signed offline with the real Root.
- Verified: run-2 summary above; full suite 293 OK locally; card-scope check OK.
