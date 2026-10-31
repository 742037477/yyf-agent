# T06: acceptance must come from the task owner

- **Assignee:** Codex (GPT-6)
- **Branch:** `task/T06-owner-acceptance`
- **Line budget:** ≤ 80 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/contract.py`, `m2k/kernel.py`, `m2k/accept.py`, `m2k/demo.py`, `examples/demo_calc/task.toml`, `tests/helpers.py`, `tests/test_pipeline.py`, `tests/test_edit_file.py`, `tests/test_contract_cli.py`, `tests/test_ledger_location.py`, `tests/test_owner_acceptance.py`, `README.md`
- **Status:** DONE

## Goal
Stage 1.5, anti-deception. Today a contract may name any acceptance command, including one that only
runs tests the worker writes itself (the demo does exactly this, and so did T03). Then "the tests pass"
proves nothing: the worker graded its own work. A second hole: a worker can drop a file such as
`unittest.py` next to the acceptance script, so the script imports the worker's fake module instead
of the real one and "passes". After this card the acceptance files are named in the contract, exist
before the worker starts, cannot be changed by the worker, and cannot be hijacked by shadow modules.

## Required behaviour
1. `[acceptance] files = ["check.py", ...]` (workspace-relative). `Acceptance` gets a field
   `files: tuple[str, ...] = ()`. `parse_contract` rejects a missing or empty list with
   `ContractError("[acceptance].files must list the task owner's acceptance files")`.
2. `Kernel.run` refuses (`KernelError`, nothing written to the ledger) when:
   - `acceptance.files` is empty;
   - any listed file does not exist in the workspace after seeding: `acceptance file missing at submit: <rel>`;
   - the command text contains none of the listed paths (compare against the posix relative path and
     its file name): `acceptance command does not use any acceptance file`.
3. Acceptance files are protected automatically: the effective protected set is
   `contract.protected ∪ acceptance.files`. The existing gate (direct write/edit denied) and the existing
   tamper check before TEST (changed/removed → FAIL `PROTECTED_FILE_CHANGED`) apply to them.
   `task.submitted` records `"acceptance_files": {rel: sha256}`.
4. Shadow-module check, after the candidate is frozen and before the verdict: a file that is new or
   changed since the baseline is a **shadow** if it sits in the workspace root or in the folder of any
   acceptance file, and its module name is in `sys.stdlib_module_names` or is `sitecustomize`,
   `usercustomize` or `conftest`. Module name = file stem for `*.py`, or the folder name for
   `<name>/__init__.py`. Any shadow → test verdict FAIL, reason `SHADOWED_MODULE`, and the reasons list
   names the files. (Unchanged seed files are never shadows.)
5. Update every in-repo contract to the new rule:
   - `examples/demo_calc/task.toml`: `files = ["check.py"]`.
   - `m2k/demo.py`: the test file becomes owner-provided seed (`test_calc.py` written into a seed
     folder before the run, listed in `files`); the scripted worker writes only `calc.py`.
   - `m2k/accept.py` bench contracts: `files=("check.py",)`.
   - tests: add `files` where contracts are built; where a test uses an inline `{python} -c ...`
     command, change it to run an owner file written by the test (behaviour under test unchanged).
   - `tests/test_ledger_location.py` (added by T07 after this card was written): only add the
     acceptance `files` declarations and owner files its contracts need. Every T07 test must keep
     checking the same behaviour; do not weaken or remove any assertion. `Kernel.run` must check the
     ledger location before the acceptance-file checks, so the T07 refusal message is unchanged.
   - Leave `tasks/T03.toml` as it is (historical, done card).
6. Two offline scenarios in `m2k accept`, before the tamper scenario:
   - `Worker 自己写验收`: a contract whose acceptance command only runs a file the worker will create →
     expected `拒绝启动：验收文件必须由任务方提供`.
   - `Worker 放同名标准库模块劫持验收`: owner `check.py` uses `unittest` to check `add(2, 3) == 5`;
     the scripted worker writes a wrong `calc.py` plus a fake `unittest.py` whose `main()` prints OK;
     reviewer scripted GO → expected `FAIL（SHADOWED_MODULE），不交付`.
7. `README.md`: add the `[acceptance] files` row to the contract table.

## Out of scope
- Running the acceptance in a separate clean copy of the workspace.
- Parsing shell commands beyond the substring check in 2.
- Changing the reviewer (T05) or the ledger location (T07).

## Acceptance
```
python -m unittest tests.test_owner_acceptance -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
python -m m2k demo
```
Tests: empty/missing `files` rejected by the parser; missing file at submit refused; command not using
the files refused; worker `write_file check.py` denied by `protected_path`; command-route rewrite of an
acceptance file → FAIL; `unittest.py`, `json/__init__.py`, `sitecustomize.py` at the root → FAIL
`SHADOWED_MODULE`; `calc.py` and a shadow-named file inside an unrelated sub-folder → not flagged.
`m2k demo` still ends PASS. Offline acceptance shows every scenario ✅.

## Review notes (written by reviewer)

**Round 1 (PR #6): PASS (Claude, 2026-10-13), merged as #6.** ci green on Windows + Linux, card-scope green (68/80 lines),
112 tests OK, offline acceptance 10/10, demo PASS. GLM UNKNOWN (API timeout). Deliberate breakage of each new check is caught.
Follow-up outside this card: bare `unittest.pyc` / `.so` / `.pyd` shadows and forged `__pycache__` bytecode → T10.

## Handoff note (written by implementer, ≤ 10 lines)

- Based on main ff4d221 (runtime baseline a17fce5); baseline allowed worker-owned checks and fake unittest.py to PASS.
- Added required owner acceptance files, submit hashes, automatic protection, and frozen-candidate shadow checks before TEST.
- Updated demo, bench scenarios, example, README, and permitted test contracts; retained all T07 assertions and ledger-first refusal.
- Verified: owner-acceptance tests 16/16; full suite 112 tests OK (1 POSIX-only skip on Windows); offline scenarios 10/10; demo PASS.
- m2k/ additions: 68/80; no dependencies added. Linux/Python 3.11 await CI; final acceptance remains with CI and reviewer.
