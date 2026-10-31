# T18: read-only self-check before an offline Root signing

- **Assignee:** Codex (GPT-6)
- **Branch:** `task/T18-sign-preflight`
- **Line budget:** 0 added lines in `m2k/` (tooling only)
- **Files you may change:** `tools/sign_preflight.py`, `tests/test_sign_preflight.py`, `README.md`
- **Status:** DONE

## Goal
Stage 2. The owner signs with the real Root only on a machine that is offline and runs no agent. The owner's
"lightweight check" (decided 2026-10-13) is three steps:
1. disconnect the network;
2. close every agent and related process;
3. look over scheduled tasks and startup items for anything unfamiliar, and only then insert the USB key
   (drive `H:`).

This card adds a script that does the looking and prints one clear table. It only reads; it never changes, kills,
disables or deletes anything.

## Required behaviour (`tools/sign_preflight.py`, standard library only, runs on Windows; Linux/macOS best effort)
1. `python tools/sign_preflight.py [--allow NAME ...]` runs these checks and prints a table:
   `check | result (PASS / FAIL / LOOK) | details`.

   | Check | How | Result |
   | --- | --- | --- |
   | `network` | A TCP connection to `1.1.1.1:53` (2 s timeout) and a DNS lookup of `example.com` | Either succeeds → **FAIL**. On Windows also list network adapters whose status is `Up` (`powershell -NoProfile -Command "Get-NetAdapter \| Where-Object Status -eq 'Up' \| Select-Object -ExpandProperty Name"`); any adapter Up → **FAIL**, with the names. |
   | `agent processes` | The process list (`tasklist /FO CSV /NH` on Windows, `ps -eo comm=` elsewhere), matched case-insensitively against a built-in list: `codex`, `claude`, `chatgpt`, `cursor`, `copilot`, `windsurf`, `ollama`, `lmstudio`, `deepseek`, `zcode`, `cc-switch`, `node`, `python` | Any match except the script's own Python process, and except names passed with `--allow` → **FAIL**, listing the processes. |
   | `scheduled tasks` | On Windows, `schtasks /query /fo CSV /v`: enabled tasks whose path does not start with `\Microsoft\` | **LOOK**, listing name and command (at most 40 rows), for the owner to judge. None → **PASS**. Not Windows → **LOOK** with "check by hand". |
   | `startup items` | On Windows: `reg query` of `HKCU` and `HKLM` `\Software\Microsoft\Windows\CurrentVersion\Run`, plus the two Startup folders | **LOOK** with each entry; none → **PASS**. |
   | `signing drive` | Whether `H:\` exists | Present → **LOOK** "USB already inserted; the checks above should be done before inserting it". Absent → **PASS**. |
2. Exit code:
   - 0 when there is no FAIL; the last line reads `可以插入 U 盘签发（LOOK 项请人工确认）`.
   - 1 when any check fails; the last line reads `不要插入 U 盘：先处理 FAIL 项`.
   - A check that cannot run (command missing, timeout 10 s) is **LOOK** with the reason, never PASS.
3. The script never writes files, never kills or changes anything, and never reads any key file.
4. `README.md` "信任链" section: one paragraph on the signing ceremony.
   - Disconnect, close agents, run `python tools/sign_preflight.py`, insert the USB key, run `m2k trust sign-charter`
     (or `sign-revocations`) with `--root-key H:\...`, remove the key, then reconnect.

## Out of scope
- Automatic fixes, killing processes, disabling tasks. Checking the signing machine for malware.
- Anything in `m2k/`.

## Acceptance
```
python -m unittest tests.test_sign_preflight -v
python -m unittest discover -s tests -t .
```
Tests patch `socket`, `subprocess.run`, `os.path.exists` and the platform (no real network, no real process list).
- Network reachable → FAIL and exit 1.
- An adapter Up → FAIL.
- `Codex.exe` running → FAIL, and the same name passed with `--allow` → no FAIL.
- The script's own `python` process is not flagged.
- A non-Microsoft scheduled task → LOOK, listed.
- A Run-key entry → LOOK.
- `H:\` present → LOOK.
- A command that is missing or times out → LOOK, not PASS.
- All clear → exit 0 with the "可以插入 U 盘" line.
- The script calls no writing or killing API (inspect the source in a test: no `open(` in write mode, no `taskkill`,
  no `os.remove`, no `shutil`).

## Review notes (written by reviewer)

**Round 1 (PR #18): PASS (Claude, 2026-10-14), merged as #18. GLM NO_GO overruled:** its schtasks column list omits
"Start In"; the real order puts Task To Run at 8 and Scheduled Task State at 11, as implemented. Later: py-launcher
parent process flagged as python (fail-safe); DNS-thread queue.Empty guard.

## Handoff note (written by implementer, ≤ 10 lines)

- Added the standard-library read-only preflight table, bounded probes, explicit allowlist and PID-based own-process exclusion on Windows.
- Added signing ceremony instructions in README; no changes under `m2k/` (0 added lines).
- Baseline: main `602361b`, full suite passed (270 tests, 9 skipped) before implementation.
- `python -m unittest tests.test_sign_preflight -v`: passed, 22 tests; all external inputs mocked.
- `python -m unittest discover -s tests -t .`: passed, 292 tests, 9 skipped (same skips as baseline).
- Real Windows smoke check returned 1 for live network/agents; tasks/startup parsed after fixing repeated CSV headers and empty Run values.
- Linux/macOS exercised with mocked outputs only; final acceptance requires CI (`ci`, `card-scope`) and reviewer approval.
