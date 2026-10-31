# T37: first adapter — a vendor agent CLI as the worker (generic CLI Worker + Codex)

- **Assignee:** Claude (GLM-5.3 GO required); the real run is done by the owner on Windows
- **Branch:** `task/T37-cli-worker-codex`
- **Line budget:** ≤ 160 added lines in `m2k/`
- **Files you may change:** `m2k/external.py`, `m2k/kernel.py`, `m2k/models.py`, `m2k/cli.py`, `tests/test_external_agent.py`, `tests/fixtures/fake_agent.py`, `examples/codex_demo/*`, `examples/codex_demo/seed/*`, `tasks/README.md`
- **Status:** DONE

## Goal
This is stage 7a, the first adapter (owner decision 2026-10-14: right after stage 3). M2 governs another company's
agent, starting with Codex.

The owner decided on 2026-10-15 to run it on the owner's Windows computer with the existing ChatGPT login, so there
is no extra API cost.

M2 cannot see each action of an external agent. It governs the **conclusion**:
- the trust chain is checked before the agent starts, and again before the test and before the delivery;
- the agent's environment is an allow-list, so no M2 key reaches it;
- the kernel freezes what the agent left and checks the owner's protected and acceptance files;
- the owner's acceptance runs, a reviewer from another vendor (GLM) reads the work, and the result is delivered once.

What the agent says about its work is never adopted.

## Required behaviour
1. `[models.worker] kind = "cli"` builds a `CliAgent`. Its settings:
   - `command` (argv; the prompt goes to stdin);
   - `env` (the only variables passed on);
   - `timeout_s`;
   - `usage = "codex-jsonl"` (sum the `turn.completed` usage, or else None);
   - `isolation` (what the vendor declares; recorded, not verified).
2. Before the agent starts, the `trust_chain` gate runs. A broken chain gives `TRUST_BROKEN`, and the agent never
   starts.
3. The ledger records:
   - one `effect.intent` / `effect.result` pair, `tool = "external_agent"`, `op_id = "x1"`. The intent holds the
     agent, argv, declared isolation and the prompt's sha256; the result holds the status, exit code, seconds and a
     redacted output tail;
   - one `model.called` (role worker) with the reported tokens, or None.
4. How the agent run ends:
   - exit 0 → `FINISHED`, with an empty summary;
   - another exit → `MODEL_ERROR`;
   - timeout (process tree killed) → effect `UNKNOWN` and `STALLED`;
   - reported tokens over `max_tokens` → `BUDGET_EXHAUSTED`.

   After that, the normal chain applies: freeze, integrity, acceptance, review, delivery.
5. The kernel refuses an external agent with `plan = "model"`, or with a contract that does not grant every tool: the
   agent can do everything a worker can, so the contract must say so.
6. Subtasks work unchanged: each attempt runs the agent in its own copy, and only a PASS inside its scope is merged.
7. `m2k ping` skips an external agent and says so.
8. `examples/codex_demo/`: a task (`wordstats.py`, owner check in `seed/check.py`) and a config template with Codex as
   the worker and GLM as the reviewer.

## Acceptance
- `python -m unittest tests.test_external_agent -v` and the full suite. A fake agent stands in for Codex and covers:
  good work, a rewritten acceptance check, a confident story over wrong code, exit failure, timeout, budget, secrets
  not passed, a broken chain, refused contracts, config, and subtasks.
- Owner, on Windows, runs the real Codex once (steps below). The run must be PASS with a delivery. Claude records the
  ledger facts here.

## How the owner runs it (Windows, from the repo folder)
1. `git pull`, then `python -m pip install cryptography` (once).
2. `codex login status` must show the ChatGPT login.
3. `python -m m2k trust init-dev --out examples/codex_demo/keys`, and paste the `[trust]` block it prints into
   `examples/codex_demo/m2k.codex.toml`. Set `api_key_file` there to your GLM key file.
4. `python -m m2k --config examples/codex_demo/m2k.codex.toml run examples/codex_demo/task.toml`

## Not in this card
- Network allow-listing at M2 level. On Windows, Codex's own sandbox is the only isolation; M2 records it as declared.
- Codex reading files outside the workspace: its sandbox restricts writes, not reads. The owner's key files are
  readable to it, as they are to any program the owner runs.
- Claude Code and DeepSeek Harness: the second adapter card.

## Review notes (written by reviewer)

**Round 1 (PR #37): GLM NO_GO.** The `m2k ping` skip (item 7) had no test. Fixed: `test_ping_skips_an_external_agent_without_starting_it`.

**Round 2: GLM GO; merged as #37 (Claude, 2026-10-15).**

**Owner's real run (Windows, codex-cli 0.162, ChatGPT login, 2026-10-15): `codex-demo-7` PASS.**
- Worker FINISHED (`codex exec` exit 0), with 140,408 tokens as Codex reported them. The owner check passed. GLM GO:
  the spec was met, `check.py` was unchanged, and there was no text aimed at the reviewer. Delivered once to
  `examples/codex_demo/out/codex-demo-7`.
- Earlier attempts, each judged correctly, never PASS:
  - demo-1/2: Codex 0.162 refused `--full-auto`, then refused `--approve-for-me` with `--sandbox` (exit 2) → FAIL;
  - demo-3: run inside the Codex app's sandbox, m2k could not read the GLM key → UNKNOWN;
  - demo-4: plain PowerShell had no `codex` on PATH (Store app) → FAIL;
  - demo-5/6: wrong key path (a folder, then a hidden `.txt` extension; Claude's mistake) → UNKNOWN.
- The example config now holds the verified flags (`exec --sandbox workspace-write --skip-git-repo-check --json -`)
  and notes on the Store app and the key file.
- Found: on Windows the Codex app's sandbox also blocks reads outside the workspace, which is stricter than the card
  assumed.
- Follow-up: check an agent CLI's flags before a run (a dry start), so a flag change fails before the task starts.

## Handoff note (written by implementer, ≤ 10 lines)
- `m2k/external.py` (130 lines): `CliAgent`. `kernel._work` sends the model loop or the external agent through the
  same chain. `build_model` gains `kind = "cli"`; `m2k ping` skips external agents.
- `tests/test_external_agent.py` has 10 tests, using a fake agent (`tests/fixtures/fake_agent.py`) in place of Codex.
  The full suite is green; offline scenarios 23/23.
- The demo check was verified with a reference `wordstats.py` (not committed).
- Not done: the real Codex run on the owner's Windows computer (after merge; steps above).
