# AGENTS.md — rules for coding agents working in this repo

This file is stable. Do not add status, progress notes, or logs here. Ever.

## How work arrives
- You work on exactly one task card: `tasks/Txx-*.md`. Read it fully before starting.
- Work on the branch named in the card (`task/Txx-...`). Never commit to `main`.
- Only touch the files the card lists. If you believe another file must change, stop and write
  that in your handoff note instead of changing it.

## Hard limits
- Respect the card's line budget. Smaller is better. No speculative abstractions, no "future" hooks.
- Python ≥ 3.11 standard library, plus `cryptography` for Ed25519 signatures only. Never hand-roll crypto.
  Any other dependency requires the card to say so.
- Do not create reports, evidence JSON, hash manifests, review records, or status documents.
  The ledger records runtime facts; git records development history. Nothing else is needed.
- Do not modify `AGENTS.md`, `docs/ARCHITECTURE.md`, or existing tests unless the card says so.
- Must work on Windows and Linux (paths via pathlib, text files UTF-8, no shell-specific tricks in code).

## Definition of done
1. The card's acceptance command passes, and so does the full suite:
   `python -m unittest discover -s tests -t .`
2. New behaviour has tests in `tests/`. Use `ScriptedModel`; never call a real API in tests.
3. Commit with message `Txx: <one line>` and push the branch. Never push to `main`.
   Do not open the PR yourself: the reviewer opens it (Codex's sandbox blocks PR creation).
4. Append a handoff note of at most 10 lines to the bottom of the task card:
   what changed, how you verified it, anything you could not do.
5. You do not decide that the card is done. CI (`ci`, `card-scope`) must be green and the
   reviewer must approve. If review asks for changes, fix only the listed blocking points.
   Ignore non-blocking notes. Workflow details: `docs/WORKFLOW.md`.

## Design rules that must never be broken
- A model reply is a proposal (data). Only `worker.py`, after `gates.check_all` allows it, calls `tools.execute`.
- Every effect writes `effect.intent` before and `effect.result` after, with the same op_id.
- The ledger is append-only. Never update or delete events. Usage is derived from events.
- Unknown is not zero: missing token usage stays `None`; an unparseable review is UNKNOWN, never PASS.
- Only a PASS verdict on the exact frozen candidate digest may be delivered.
- The reviewer never sees the worker's conversation.
