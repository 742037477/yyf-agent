# T07: local runs keep the ledger outside the workspace

- **Assignee:** Codex (GPT-6)
- **Branch:** `task/T07-ledger-outside-workspace`
- **Line budget:** ≤ 40 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/cli.py`, `m2k/kernel.py`, `m2k/accept.py`, `tests/test_ledger_location.py`, `m2k.example.toml`, `README.md`
- **Status:** DONE

## Goal
Stage 1.5. On GitHub the ledger already lives outside the workspace (T04). Locally it does not:
the default is `.m2k/ledger.sqlite` next to the config, and a task like `tasks/T03.toml` uses the
repository itself as its workspace, so the ledger sits inside the folder the worker may write.
A worker could then delete or rewrite the record of what it did. After this card the kernel refuses
to run any task whose workspace contains the ledger, and the default ledger moves to a per-user folder.

## Required behaviour
1. Default ledger path (used when neither `--ledger` nor `ledger =` in the config is given):
   - Windows: `%LOCALAPPDATA%\m2k\ledger.sqlite`
   - other systems: `$XDG_STATE_HOME/m2k/ledger.sqlite`, else `~/.local/state/m2k/ledger.sqlite`
   An explicit `--ledger` or config `ledger` still wins (relative paths resolve as today).
2. `Kernel.run` refuses before writing anything to the ledger when the resolved ledger file is
   inside the resolved workspace (or equal to a path in it): `KernelError` whose message contains
   `ledger is inside the workspace` and both paths. `m2k run` prints it and exits non-zero.
3. If the old default `.m2k/ledger.sqlite` exists in the current folder and the new default is in
   use, `m2k status | log | report | check-ledger` print one line to stderr:
   `旧账本仍在 .m2k/ledger.sqlite；查看旧任务请加 --ledger .m2k/ledger.sqlite`. Nothing is moved or deleted.
4. `task.submitted` records the resolved ledger path (`"ledger": "<path>"`).
5. New offline scenario in `m2k accept`, placed before the tamper scenario:
   name `账本放在工作区里`, expected `拒绝启动，账本不受影响`. It builds a contract whose workspace
   folder contains the bench ledger file and checks that the run raises the refusal and that the
   ledger has no event for that task id.
6. `m2k.example.toml`: remove the `ledger = ".m2k/ledger.sqlite"` line and add a comment explaining
   the new default and that the ledger must never be inside a task workspace.
   `README.md`: one sentence in the quick-start saying where the ledger lives now.

## Out of scope
- Stopping a command from reaching the ledger by absolute path. That needs OS isolation (T08);
  detecting a rewritten ledger needs the external anchor (T09).
- Moving or migrating existing ledgers.
- Changing `m2k accept --dir` or `m2k report --out` defaults.

## Acceptance
```
python -m unittest tests.test_ledger_location -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
Tests (temporary folders only; set `LOCALAPPDATA` / `XDG_STATE_HOME` / `HOME` via `unittest.mock.patch.dict`):
default path on the current OS; explicit `--ledger` wins; ledger inside workspace → refused and no event
written; ledger next to (not inside) the workspace → runs. Offline acceptance must show 8/8.

## Review notes (written by reviewer)

**Round 1 (PR #5): PASS (Claude, 2026-10-13), merged as #5.** ci green on Windows + Linux, card-scope green (32/40 lines),
96 tests OK, offline acceptance 8/8. GLM NO_GO overruled: `s_tamper` was never in `OFFLINE`; it always runs last separately.
Deliberate breakage caught: removing the inside-workspace check fails 6 tests; dropping the `ledger` field fails 1.

## Handoff note (written by implementer, ≤ 10 lines)

- Based on `origin/main` at `fbf8a84`; baseline passed 83 tests (1 skipped) and 7/7 offline scenarios.
- Default ledger now uses per-user state storage; explicit paths keep their precedence and relative base; read commands warn about the untouched old ledger.
- Kernel rejects resolved ledger paths inside/equal to the workspace before preparation or event writes; `task.submitted` records the resolved ledger path.
- Added 13 temporary-folder, scripted-model tests covering paths, refusal without changes, successful delivery, CLI errors, and migration warnings.
- Verified: `python -m unittest tests.test_ledger_location -v`; full suite OK (96 tests, 1 existing skip); `python -m m2k accept --offline` reports 8/8.
- Changes stay in the allowed files plus this handoff; 32 added lines under `m2k/` (budget 40); example config and quick-start updated.
- CI and reviewer approval remain pending; no real API was called.
