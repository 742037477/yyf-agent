# T01: edit_file tool (exact string replace)

- **Assignee:** Codex
- **Branch:** `task/T01-edit-file`
- **Line budget:** ≤ 80 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/tools.py`, `m2k/contract.py`, `m2k/gates.py` (one line, see 6), `tests/test_edit_file.py`
- **Status:** OPEN

## Goal
Workers can currently only rewrite whole files with `write_file`. For files longer than a few
hundred lines that is slow, expensive and error-prone. Add an `edit_file` tool that replaces one
exact piece of text, so a worker can make a small change to a large file.

## Required behaviour
1. New tool `edit_file` with string params `path`, `old`, `new`. Register it in `TOOLS` with
   `path_params=("path",)` so the existing path and protected-file gates apply to it unchanged.
2. Add `"edit_file"` to `DEFAULT_TOOLS` in `contract.py`.
3. `old` must occur **exactly once** in the file. Otherwise return `FAILED` with a message that
   says how many times it occurred (0 or N), and do not change the file.
4. `old` must be non-empty; a missing file returns `FAILED`.
5. On success, write UTF-8 with `\n` newlines (same as `write_file`) and return `COMPLETED`
   with output like `edited calc.py: replaced 1 occurrence (-3 +5 lines)`.
6. `ProtectedPathGate` currently checks only `write_file`. It must also block `edit_file` on
   protected paths. In `gates.py` change only that one condition.

## Out of scope
- Regex, multiple replacements, line-number edits, diffs/patch formats.
- Any change to worker.py, kernel.py or the prompt text.

## Acceptance
```
python -m unittest tests.test_edit_file -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
Tests must cover: single match succeeds; zero matches fails and the file is unchanged; two
matches fail and the file is unchanged; a protected file is denied by the gate; a path outside
the workspace is denied by the gate; a full kernel run where a ScriptedModel worker uses
`edit_file` and reaches PASS.

## Review notes (written by reviewer)

## Handoff note (written by implementer, ≤ 10 lines)

- Added `edit_file` with exact single-match replacement, UTF-8/LF output and line counts; ambiguous (including overlapping) matches fail without writes.
- Enabled the tool by default and extended protected-path denial with only the specified condition change; 25 added lines under `m2k/` (budget 80).
- Added 12 tests covering success, unchanged failures, empty/missing inputs, gates and a ScriptedModel kernel PASS with delivery and paired ledger effects.
- Verified: `python -m unittest tests.test_edit_file -v` (12 passed); `python -m unittest discover -s tests -t .` (58 passed); `python -m m2k accept --offline` (7/7).
- Baseline: `origin/main` at `460eba1` passed its 46 tests and 7/7 offline scenarios before changes; `m2k.toml` stayed ignored and unchanged.
- Out-of-scope issues: none found. CI and reviewer approval remain pending.
