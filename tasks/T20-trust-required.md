# T20: no trust chain, no run (unless the owner says so for this one run)

- **Assignee:** Codex (GPT-6.1)
- **Branch:** `task/T20-trust-required`
- **Line budget:** ≤ 60 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/cli.py`, `m2k/report.py`, `m2k.example.toml`, `README.md`, `.github/workflows/m2k-task.yml`, `tests/test_trust_required.py`, `tests/test_anchor.py`, `tests/test_ledger_location.py`, `tests/test_sandbox.py`
- **Status:** DONE

## Goal
Stage 2 close. Decided by the owner on 2026-10-14: a task with no `[trust]` is **refused by default**.
- A run without the chain is only possible when the owner asks for it explicitly, run by run, with `--untrusted`.
  The run is then visibly marked as unauthorised.
- So that a new user is not stuck, one command, `m2k trust init-dev`, creates a complete TEST_ONLY chain on their
  computer.
- The kernel itself does not change (`Kernel(..., trust=None)` stays for tests and the offline scenarios). The rule
  lives where a person starts a task: `m2k run`.

## Required behaviour
1. **`m2k run` refuses without `[trust]`.**
   - With no `[trust]` table in the config and no `--untrusted` flag, it prints this to stderr and exits 2, before any
     ledger write or model call:

     `拒绝启动：没有信任链（[trust]）。两个选择：1) 运行 m2k trust init-dev --out keys 生成测试链，并把它打印的 [trust] 段加进 m2k.toml；2) 只这一次不授权运行：m2k run --untrusted <task>（会记入账本）。`
   - The check comes right after the contract is loaded (an invalid contract is still reported first) and before the
     sandbox check.
2. **`m2k run --untrusted <task>`** runs as today without a chain.
   - It prints `警告：未授权运行（没有信任链），已记入账本。` to stderr.
   - The ledger already shows it: `task.submitted.trust` is `null`.
   - With `[trust]` configured, `--untrusted` is an error (exit 2, message `已配置 [trust]，不能同时用 --untrusted`).
     A configured chain is never silently skipped.
3. **The HTML report** (`m2k report`) shows a red banner at the top when `task.submitted.trust` is `null`:
   `未授权运行：这个任务没有信任链（Root → 授权书 → 凭证）`.
   - With a chain, it shows one line: domain, Root fingerprint, charter id, credential id.
4. **`m2k trust init-dev --out DIR`** creates a fresh TEST_ONLY chain in `DIR`.
   - `DIR` must not exist yet, or be empty; never overwrite.
   - Files: `root.pem`, `root-pin.json`, `issuer.pem`, `issuer-pin.json`, `caps.toml`, `charter.json` (release `dev`)
     and `revocations.json` (version 0, empty).
   - Reuse the existing keygen / sign code paths in `cli.py`; no second implementation of signing.
   - Default `caps.toml`: `tools` = every tool in `m2k.tools.TOOLS` (sorted), `max_rounds = 100`,
     `max_model_calls = 120`, `max_tokens = 10000000`, `max_commands = 100`, `network = false`.
   - It prints the Root fingerprint, then a ready-to-paste `[trust]` table with `domain = "TEST_ONLY"` and paths to the
     files in `DIR` written with forward slashes.
   - Last line: `TEST_ONLY：只用于开发和测试。正式运行需要用真 Root 离线签发的授权书。`
   - Private keys are written mode 600 where the OS supports it, as `keygen` does.
   - `m2k trust status` with that table exits 0.
5. **`m2k-task.yml`**: without the secret `M2K_TEST_ISSUER_KEY` the trust staging step fails with
   `::error::M2K_TEST_ISSUER_KEY secret missing: m2k-task always runs under the trust chain (T20)`. It no longer runs
   without a chain, and the workflow never passes `--untrusted`. Update the header comment line to match.
6. **`m2k.example.toml`**: the `[trust]` block is no longer commented out as optional. It says that `m2k run` refuses
   without it, and that `m2k trust init-dev --out keys` makes a test chain.
7. **README**:
   - Quick start (the real-model part) gains the `init-dev` step before `m2k run`.
   - In "信任链", replace the five manual steps with `init-dev`, keeping one sentence that the individual `keygen` /
     `sign-charter` commands still exist.
   - Add one sentence on `--untrusted`.

## Existing tests
`tests/test_anchor.py`, `tests/test_ledger_location.py` and `tests/test_sandbox.py` call `main([... "run", ...])` without
a chain.
- Only add `"--untrusted"` to those invocations. Change nothing else in those files.
- If another existing test breaks, stop and write it in the handoff note.

## Out of scope
- Any change to `m2k/kernel.py`, `m2k/trust.py`, `m2k/gates.py` or the offline scenarios.
- A production charter. Requiring `[trust]` inside `Kernel` itself.

## Acceptance
```
python -m unittest tests.test_trust_required -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
`tests/test_trust_required.py` (temporary folders, `ScriptedModel` or a stubbed `build_model`, no network) must cover:
- No `[trust]`, no flag → exit 2, the exact refusal text, the ledger file not created or empty, no model built.
- An invalid contract without `[trust]` → still the contract error.
- `--untrusted` → the run completes; `task.submitted.trust` is `null`; the warning is printed.
- `[trust]` plus `--untrusted` → exit 2.
- `init-dev` into a new folder → the seven files exist; `trust status` on the printed table exits 0; the Root fingerprint
  printed equals `root-pin.json`'s; a second `init-dev` into the same non-empty folder fails and changes no file.
- With the printed `[trust]` table, `m2k run` on a scripted task succeeds and `task.submitted.trust.domain` is
  `TEST_ONLY`.
- The HTML report contains the red banner for an untrusted run and the chain line for a trusted one.

## Review notes (written by reviewer)

**Round 1 (PR #20): PASS (Claude, 2026-10-14, after GLM GO), merged as #20.** Refusal comes after the contract check and
before sandbox, ledger and models; `init-dev` smoke-tested by hand (seven files, keys mode 600, `trust status` OK, second
run refused). Offline 18/18, full suite OK. Later: the `--untrusted` warning prints before a possible sandbox refusal;
`next()` default in the report; nested `main()` calls in `init-dev` do not carry `--config`.

## Handoff note (written by implementer, ≤ 10 lines)

- Base: `origin/main` at `484a0fd`; baseline suite passed (293 tests, 9 skipped).
- Added default trust refusal, explicit `--untrusted`, and mutually exclusive configured trust.
- `trust init-dev` reuses keygen/signing, creates the seven TEST_ONLY files, and never overwrites non-empty output.
- Reports show the red unauthorised banner or chain identifiers; workflow/example/README require trust.
- Verified: `python -m unittest tests.test_trust_required -v` (8 passed); full suite (301 tests, 9 skipped).
- Verified: `python -m m2k accept --offline` (18/18); `git diff --check` clean; `m2k/` added 45/60 lines.
- Existing tests changed only by adding `--untrusted`; kernel/trust/gates and offline scenarios untouched.
- Reviewer opens the PR; remote CI/card-scope and final approval remain pending; card status remains OPEN.
