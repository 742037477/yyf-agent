# T14: signed envelopes and the trust chain (library only)

- **Assignee:** Claude (core logic; GLM-5.3 GO required, because the author is also the final reviewer)
- **Branch:** `task/T14-trust-core`
- **Line budget:** ≤ 190 added lines in `m2k/` (tests not counted). Raised from 170 by the author before review:
  three document types, twelve named checks and the malformed-document guard needed the room; nothing speculative.
- **Files you may change:** `m2k/trust.py`, `tests/test_trust.py`, `.github/workflows/ci.yml`, `.github/workflows/m2k-task.yml`, `.github/workflows/real-accept.yml`, `README.md`, `pyproject.toml` (dependency line only)
- **Status:** DONE

## Goal
Stage 2, the user's second core. This card builds only the library that later cards wire in:
- signed JSON envelopes;
- loading the Root public key ("pin");
- three signed documents: the framework charter, the revocation list and the task credential;
- one function that verifies the whole chain, Root → charter → credential → contract, and names exactly why it failed.

The library holds no secrets and touches no files except the ones it is asked to load.
The only new dependency is `cryptography` (Ed25519), as `AGENTS.md` allows.

**Compatibility with the old project** (Codex investigation, 2026-10-13):
- The old TEST_ONLY Root pin is JSON with `public_key_b64` and `trust_domain`. It is Ed25519, 32 bytes, fingerprint `6357ec2addf1fd69`.
- Old signed files are `{schema, body, signature_b64}`, where the signature covers the canonical JSON of `body`.
- Canonical JSON: `ensure_ascii=False, sort_keys=True, separators=(",",":"), allow_nan=False`, UTF-8; floats, duplicate keys and BOM are rejected.

This card uses the same envelope and canonical form, so the old test Root works unchanged for end-to-end drills.

## Required behaviour (`m2k/trust.py`)
1. `canonical(obj) -> bytes`, plus `loads_strict(text)`:
   - `loads_strict` rejects a BOM, duplicate keys, floats and NaN/Infinity.
   - Integers, strings, booleans, null, lists and objects only.
2. `sign(body: dict, private_key) -> dict` returns `{"schema": body["schema"], "body": body, "signature_b64": ...}`.
   `open_envelope(env, public_key) -> dict` returns the body, or raises `TrustError`. It rejects a signature that
   fails, any extra or missing top-level field, and an envelope `schema` that differs from `body["schema"]`.
3. `load_pin(path) -> Pin(public_key, trust_domain, fingerprint)`. The fingerprint is the first 16 hex characters of
   sha256 over the raw 32 key bytes. If the file also carries `fingerprint` or `public_key_sha256`, that value must match.
4. Trust domains are `TEST_ONLY` and `PRODUCTION`.
   - Every signed body carries `trust_domain`.
   - Verification takes the configured domain. A pin, charter, revocation list or credential from another domain is
     refused (`DOMAIN`). So production refuses anything the test Root signed.
5. The documents (all timestamps are integer Unix seconds):
   - **charter** `m2k.charter/1` (signed by Root, permanent until revoked or the Root changes):
     - `trust_domain`, `charter_id`
     - `issuer_public_key_b64`: the framework issuer's public key
     - `release`: `"dev"`, accepted only in `TEST_ONLY`; otherwise the `release_digest` of the m2k code
     - `caps`: `{tools: [..], max_rounds, max_model_calls, max_tokens, max_commands, network: bool}`
     - `issued_at`
   - **revocations** `m2k.revocations/1` (signed by Root): `trust_domain`, `version`, `revoked_charters: [..]`,
     `revoked_credentials: [..]`, `issued_at`.
   - **credential** `m2k.credential/1` (signed by the issuer key named in the charter):
     - `trust_domain`, `charter_id`, `credential_id` (random)
     - `task_id`, `contract_sha256`
     - `tools`, `budget` (`{max_rounds, max_model_calls, max_tokens, max_commands}`), `network`
     - `not_before`, `expires_at`
6. `release_digest(code_dir) -> str`: sha256 over the sorted `relative/path:sha256-of-file` lines of every `*.py` file
   under `code_dir`.
7. `issue_credential(charter, issuer_private_key, contract, now, ttl_s) -> envelope`. It first checks that the
   contract's tools, budget and network fit inside the charter caps; otherwise it raises `TrustError("EXCEEDS_CHARTER")`.
   The framework cannot grant more than Root granted it.
8. `verify_chain(pin, domain, charter_env, credential_env, revocations_env | None, contract, now, code_dir) -> dict`
   returns the verified credential body, or raises `TrustError` with one of these codes. Checks run in this order:
   1. `DOMAIN`
   2. `CHARTER_SIGNATURE`
   3. `REVOCATIONS_SIGNATURE` (the list must verify before it can be used)
   4. `CHARTER_REVOKED`
   5. `RELEASE`: the charter's release does not match `code_dir`
   6. `CREDENTIAL_SIGNATURE`: not signed by the charter's issuer key
   7. `CREDENTIAL_REVOKED`
   8. `WRONG_CHARTER`
   9. `NOT_YET_VALID`, `EXPIRED`
   10. `WRONG_TASK`, `CONTRACT_MISMATCH`: task id or `contract.digest()` differ
   11. `EXCEEDS_CHARTER`: the credential grants more than the charter caps
   12. `EXCEEDS_CREDENTIAL`: the contract asks for more than the credential grants

   `TrustError.code` holds the code; the message is plain English. A signed document with missing or
   mistyped fields gives `FORMAT`, never a crash.
9. Key helpers for tests and dev tooling: `generate_keypair() -> (private_key, public_key_b64)`,
   `load_private_key(path)` (unencrypted PKCS#8 PEM), `public_key_b64(private_key)`.
10. Workflows: `pip install cryptography` before tests (`ci.yml`, both OSes), and in `m2k-task.yml` / `real-accept.yml`
    before m2k runs. `README.md`: one line saying the only dependency is `cryptography`; `pyproject.toml` lists it.

## Out of scope
- Wiring into the kernel, the Gate, the CLI or the config (T15, T16).
- Signing tools, the preflight check, the end-to-end drill with the old Root (T17–T19).
- Encrypted private keys, hardware tokens.

## Acceptance
```
python -m unittest tests.test_trust -v
python -m unittest discover -s tests -t .
```
Tests use freshly generated keys only; no key file from the owner's computer.
- Round trip for every document type.
- Each failure code is triggered by its own test:
  - a tampered body;
  - another key;
  - test-domain documents under `PRODUCTION`;
  - a revoked charter and a revoked credential;
  - a wrong release, and `dev` under `PRODUCTION`;
  - expired and not-yet-valid credentials;
  - a credential issued for another task, and one for an edited contract;
  - a credential granting more than the charter, and a contract asking more than the credential;
  - `issue_credential` beyond the caps.
- Canonical JSON rejects floats, duplicate keys and a BOM.
- A pin file in the old project's format (built in the test with a fresh key) loads.

## Review notes (written by reviewer)

**GLM round 1: NO_GO, accepted (Claude, 2026-10-13).** Fixed both points:
1. A missing, None, non-int (or bool) cap, a non-list `tools` or a non-bool `network` is now FORMAT in both
   `verify_chain` and `issue_credential`; charter caps are never treated as "unlimited". Chain checks also run lazily,
   so the first failing check names the reason.
2. `public_key_sha256` in a pin is tested (match loads, mismatch gives ROOT_PIN).

**Round 2: PASS (Claude, after GLM GO), merged as #14.** Notes carried to T15/T16: bad issuer key → FORMAT,
load_pin on non-object JSON → TrustError, exact fingerprint match.

## Handoff note (written by implementer, ≤ 10 lines)

- `m2k/trust.py`: canonical JSON (old project's rules), `{schema, body, signature_b64}` envelopes, pins in the old
  `public_key_b64`/`trust_domain` format, charter / revocations / credential, `issue_credential`, `verify_chain`
  (12 codes + FORMAT for malformed signed documents). Revocation list is verified before it is used (order updated in card).
- 22 tests with fresh keys; each check disabled in turn fails a test. Full suite OK.
- `cryptography` installed in ci / m2k-task / real-accept; listed in pyproject and README.
- Not done here (by design): kernel/Gate wiring (T16), CLI and config (T15), drills (T17), old test Root e2e (T19).
