# T03: unit tests for tools.py

- **Assignee:** DeepSeek-V4-Flash via m2k (`tasks/T03.toml`)
- **Branch:** `task/T03-tools-tests`
- **Line budget:** 0 lines in `m2k/` (tests only)
- **Files you may change:** `tests/test_tools.py`
- **Status:** DONE

## Goal
`m2k/tools.py` is only tested indirectly. Add direct unit tests. This card is also the first
time m2k governs a real model working on m2k itself.

## Required behaviour
Create `tests/test_tools.py` (standard library `unittest`, temporary directories only) with at
least 8 tests covering:
1. `Workspace.resolve`: a normal relative path works; `../x`, an absolute path outside, and
   `a/../../x` raise `ScopeError`.
2. `list_files`: lists nested files; skips `.git`, `__pycache__` and `.m2k`.
3. `read_file`: returns content; a missing file gives `FAILED`; content longer than `MAX_READ`
   is truncated.
4. `write_file`: creates parent folders; writes UTF-8 (test a Chinese string).
5. `run_command`: exit 0 → `COMPLETED`; exit 3 → `FAILED` with `exit_code` 3;
   a command that sleeps longer than its timeout → `UNKNOWN` with reason `TIMEOUT`.
   Use `{python} -c "..."` so it works on Windows and Linux. Keep timeouts ≤ 2 s.

Do not modify any other file.

## How to run (owner, on Windows)
```
git switch -c task/T03-tools-tests
python -m m2k run tasks/T03.toml
```
If PASS: `git add tests/test_tools.py tasks/T03-tools-tests.md`, commit `T03: unit tests for tools.py`,
push, and open the PR (or ask Codex to "commit and open the PR for T03").

## Acceptance
```
python -m unittest tests.test_tools -v
python -m unittest discover -s tests -t .
```

## Review notes (written by reviewer)

**Round 1 (run T03-run-1, PR #4): CHANGES REQUESTED (Claude, 2026-10-13).** Fix exactly these two points, nothing else:
1. Every `timeout_s` in the file must be ≤ 2 (two `run_command` tests pass `timeout_s=5`).
2. `test_read_file_truncates_long_content` must fail if `read_file` stops cutting the text. Today it only checks the
   `[truncated` marker and a prefix, so it still passes when the full content is returned with the marker appended.
   Assert that the part before the marker is exactly `MAX_READ` characters long.

**Round 2 (run T03-run-2): CHANGES REQUESTED (Claude, 2026-10-13).** Round 1 points are fixed; keep them. One new point:
3. The suite fails on Windows: `Workspace.resolve` returns fully resolved paths, but a test compared it with the
   unresolved temp folder (Windows temp is `C:/Users/RUNNER~1/...`, resolved `C:/Users/runneradmin/...`).
   Compare every path that `resolve` returns with a `.resolve()`d expected path (e.g. `self.root.resolve()`).

**Round 3 (run T03-run-3): PASS (Claude, 2026-10-13), merged as #4.** ci green on Windows + Linux, card-scope green,
GLM-5.3 GO; 7 deliberate breakages of `tools.py` are all caught by the new tests.

## Handoff note (written by implementer, ≤ 10 lines)
(Written by the reviewer from the ledger: this card was protected during the run, so the worker could not edit it.)
- DeepSeek (`deepseek-flash`) wrote `tests/test_tools.py` under m2k on GitHub (workflow `m2k-task`); GLM-5.3 reviewed inside m2k.
- run-1 PASS but rejected in review (timeouts 5 s; weak truncation test); run-2 fixed both, failed on Windows (8.3 temp path);
  run-3 fixed it: 11 tests, all PASS. Tokens: 60,385 + 64,156 + 84,501 = 209,042.
