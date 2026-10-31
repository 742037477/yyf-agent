# T38: start an agent CLI once before the task; Claude Code as the second external agent

- **Assignee:** Claude (GLM-5.3 GO required); the real run is started by the Codex desktop app on the owner's Windows computer
- **Branch:** `task/T38-agent-probe-claude-code`
- **Line budget:** ≤ 60 added lines in `m2k/`
- **Files you may change:** `m2k/external.py`, `m2k/cli.py`, `tests/test_external_agent.py`, `tests/fixtures/fake_agent.py`, `examples/claude_code_demo/*`, `tasks/README.md`
- **Status:** DONE

## Goal
This card is direction A, decided by the owner on 2026-10-15: "manage several vendors" first, with as little manual
work as possible.

In T37, the first four real runs failed at the start for reasons a 10-second check would have caught:
- a renamed flag (Codex 0.162 dropped `--full-auto`);
- a forbidden flag combination;
- `codex` missing from PATH.

Each of these cost a run, and the cause showed only in the ledger.

The card also adds a second vendor, Claude Code (`claude -p`). With two vendors, M2's neutrality is demonstrated, not
only claimed.

## Required behaviour
1. `CliAgent.probe()` runs the configured command once in an empty temporary folder:
   - the same environment allow-list;
   - the prompt `Reply with the single word OK.`;
   - at most 300 s.

   It returns None when the command exits 0. Otherwise it returns `STATUS (exit N): <redacted output tail>`.
2. `m2k run` probes an external worker before anything is recorded. If the probe fails, `m2k run` exits 2 with
   `拒绝启动：Worker 命令试启动失败（<name>）：<reason>`, and no ledger is created.
3. `m2k ping` probes an external agent: `OK … answered`, or `FAIL … <reason>`, with exit code 1. This replaces T37's
   skip.
4. `usage = "claude-json"` reads the `result` object of `claude -p --output-format json`:
   - input = `input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`;
   - output = `output_tokens`;
   - a result without `output_tokens` is unknown (None), never zero. The same now holds for `codex-jsonl`.
5. `examples/claude_code_demo/`: the Codex demo task (same owner check) and a Claude Code config:
   - `-p --output-format json --permission-mode acceptEdits`;
   - shell and web tools off, because Claude Code has no sandbox of its own on Windows;
   - GLM as the reviewer.

## Acceptance
- `python -m unittest tests.test_external_agent -v` and the full suite.
- Owner (through the Codex desktop app, approved to run outside its sandbox): run the Claude Code demo once. It must be
  PASS, or else stopped by the probe with a clear reason. Claude records the facts here.

## Not in this card
- DeepSeek Harness: no stable non-interactive CLI is known to M2 yet.
- An M2-level network allow-list.

## Review notes (written by reviewer)

**Round 1 (PR #38): GLM NO_GO.** The `codex-jsonl` unknown-usage rule had no test. Fixed: one assertion next to the Claude one.
**Round 2: GLM GO; merged as #38 (Claude, 2026-10-15).**

**Owner's real run (Windows, Claude Code 2.1.300, Claude subscription login, run from the Codex desktop app,
2026-10-15): `claude-demo-1` PASS.**
- Worker FINISHED, with 67,739 tokens as Claude Code reported them (cache included). The owner check passed. GLM GO:
  the spec was met, `check.py` was unchanged, and there was no text aimed at the reviewer. Delivered once.
- The probe stopped three earlier attempts before anything was recorded or spent:
  - `claude` was not on PATH;
  - the npm `.ps1` shim is not a program;
  - Claude Code was not logged in.
- Installation notes for later docs:
  - npm run from the Microsoft Store Codex app installs into that app's virtualized folder
    (`%LOCALAPPDATA%\Packages\OpenAI.Codex_*\LocalCache\Roaming\npm`);
  - point `command[0]` at `node_modules/@anthropic-ai/claude-code/bin/claude.exe`, not at `claude.ps1`;
  - log in once by hand in a normal terminal (`claude`; the browser code must be pasted within a few minutes);
  - the login is stored in `%USERPROFILE%\.claude`.
- M2 has now governed two vendors' agents (Codex in T37, Claude Code here) under the same chain.
- Follow-ups (small card):
  - the probe should name a script shim (`.ps1`/`.cmd`) and show the agent's own error line (e.g. "Not logged in"),
    not the raw JSON tail;
  - `m2k run` prints its sandbox warning ("已记入账本") even when the probe then refuses and nothing is recorded.

## Handoff note (written by implementer, ≤ 10 lines)
- `m2k/external.py`: `_exec` is shared by `run` and the new `probe`; `claude-json` usage was added; a usage event
  without `output_tokens` stays unknown.
- `m2k/cli.py`: `m2k run` probes an external worker before the ledger is opened; `m2k ping` probes too.
- Tests: 12 in `tests/test_external_agent.py` (ping OK/FAIL, run refused with no ledger, Claude usage through
  `Kernel`). Full suite green.
- Not done: the owner's real Claude Code run (after merge).
