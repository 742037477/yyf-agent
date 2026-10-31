# T09: anchor the ledger head outside the machine

- **Assignee:** Codex (GPT-6)
- **Branch:** `task/T09-ledger-anchor`
- **Split (2026-10-13):** defensive code only. The attack drill (rewrite an event and recompute the whole
  chain, requirement 6) moves to card T12, DeepSeek via m2k, after this card is merged. Do not write it here.
- **Line budget:** ≤ 100 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/anchor.py`, `m2k/cli.py`, `tests/test_anchor.py`, `tools/anchor_push.py`, `.github/workflows/m2k-task.yml`, `m2k.example.toml`, `README.md`
- **Status:** DONE

## Goal
Stage 1.5, anti-deception. The hash chain catches a single edited event, but anyone who can write the
ledger file can rewrite an event and recompute every hash after it; `m2k check-ledger` then says OK.
After this card the current chain head (sequence number + hash) is copied, at fixed moments, to an
append-only anchor file and from there to a place the worker can never write (GitHub). A rewritten or
truncated ledger no longer matches its anchors, and `m2k check-ledger --anchors` says exactly where.

Reuse check (2026-10-13): `<other-ledger>` holds no code, only per-requirement
`ledger.jsonl` files and evidence copies written by another tool's workflow. It sits on the same disk
and is writable by the same user, so it is not external and is not used as the anchor store.
What we keep from it is the shape: JSON lines, each carrying the previous line's hash. Leave it untouched.

## Required behaviour
1. `m2k/anchor.py`:
   - `head(ledger) -> {"seq", "hash", "events", "ledger_id"}`; `ledger_id` = hash of the first event
     (identifies the ledger); an empty ledger has no head (error).
   - `append(path, ledger, note) -> dict`: appends one JSON line
     `{"v": 1, "ts", "ledger_id", "seq", "hash", "events", "note", "prev"}` where `prev` = sha256 of the
     previous line's exact text (64 zeros for the first line). Creates parent folders. UTF-8, `\n`.
   - `check(path, ledger) -> list[str]` (empty = OK). Problems, in plain words: anchor file's own chain
     broken at line N; anchor for a different `ledger_id` (skipped, reported once); the ledger is
     shorter than an anchored `seq` (truncated); the event at an anchored `seq` has a different hash
     (rewritten). The normal `verify_chain` still runs first.
2. Config `anchor_file = "<path>"` (relative to the config folder). When set, `m2k run` appends an
   anchor right before submitting (note `before <task_id>`, only if the ledger is non-empty) and right
   after the verdict (note `<task_id> <verdict>`).
3. CLI: `m2k anchor [--out FILE] [--note TEXT]` appends one anchor (default file: `anchor_file`) and
   prints it; `m2k check-ledger [--anchors FILE]` (default `anchor_file` when set) prints
   `ledger hash chain OK` / `BROKEN at seq N` and then `anchors OK (N checked)` or each problem; exit 1
   on any problem.
4. `tools/anchor_push.py --anchors FILE --repo-dir DIR [--push]` (standard library + `git` on PATH):
   `DIR` is a local clone used only for anchors, on branch `ledger-anchors`. It copies FILE to
   `DIR/anchors/<ledger_id[:12]>.jsonl` only if the existing copy is an exact prefix of FILE (append-only;
   otherwise exit 1 with `anchor file is not an extension of the published one`), commits
   `anchors: <ledger_id[:12]> up to seq N`, and with `--push` pushes the branch. Prints what it did.
5. `m2k-task.yml`: after the run, append an anchor for the run's ledger and write the anchor line into
   the job summary (`$GITHUB_STEP_SUMMARY`) and the uploaded artifact.
6. (Moved to T12.) No new `m2k accept` scenario in this card; `m2k/accept.py` stays unchanged.
7. Docs: `m2k.example.toml` gets `anchor_file` with a comment (keep it outside every workspace);
   `README.md` gets a short "外部锚点" paragraph: what it proves, and the one-time setup of the anchors
   clone (`git clone -b ledger-anchors …`, or create the orphan branch).

## Out of scope
- Signing the chain head (stage 5); timestamps from a third party.
- Creating the `ledger-anchors` branch on GitHub (the owner or Claude does it once by hand).
- Any GitHub-side check of the anchors branch.

## Acceptance
```
python -m unittest tests.test_anchor -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline   # unchanged scenarios still all ✅
```
Tests: append twice → second line's `prev` = hash of the first; check OK on an untouched ledger;
an anchored event whose stored hash is edited in SQLite (no recomputation) → `check` reports that seq as rewritten; deleted tail events →
truncated; edited anchor line → anchor chain broken; other ledger's anchors reported, not mixed;
`anchor_push` refuses a non-prefix update and accepts an append (use a temp git repo; skip if `git` is missing).

## Review notes (written by reviewer)

**Round 1 (PR #10): PASS (Claude, 2026-10-13, after GLM GO), merged as #10.** ci + card-scope green, offline 12/12;
removing the rewrite / truncation / prev-chain checks is caught. Windows anchors clone: set core.autocrlf=false.

## Handoff note (written by implementer, ≤ 10 lines)

- Based on main `0f875aa`; implemented ledger heads, linked JSONL anchors, CLI defaults and run checkpoints.
- Added append-only publishing from a dedicated `ledger-anchors` clone; workflow summary/artifact capture and setup docs.
- `python -m unittest tests.test_anchor -v`: 25 passed (including temporary Git clone and local bare-remote push).
- `python -m unittest discover -s tests -t .`: 176 passed/skip total, 1 platform skip; baseline was 151 total, 1 skip.
- `python -m m2k accept --offline`: all 12 existing scenarios passed; `m2k/accept.py` unchanged as required.
- Scope: 100 added lines under `m2k/`; only card-listed files plus this handoff changed.
- Live GitHub anchor publishing/workflow not run; remote anchor branch setup remains with the owner/reviewer, T12 drill deferred.
- CI and reviewer approval remain pending; this card's OPEN status is unchanged.
