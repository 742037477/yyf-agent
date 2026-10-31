# T84: R0 scale benchmark (part 2) — full runs at N = 10 / 30 / 100 and the decision numbers

- **Assignee:** Codex (gpt-6.1-terra, high) configures, runs and tallies; an independent Codex check (gpt-6-astra,
  ultra) recomputes the decision numbers; Claude analyses; the user decides the direction
- **Branch:** none. Nothing in this repo changes except this card (Review notes, handoff note) and `tasks/README.md`.
- **Line budget:** none in `m2k/` (m2k code must not change). Harness changes in `<experiments>\R0\` stay
  small (≤ 200 added lines including tests).
- **Where:** `<experiments>\R0\` (T83's pool, harness and configs); m2k runs from `<worktrees>\R0-main`
  (a43f565, unchanged).
- **Status:** DONE (results 2026-10-30; direction pending the user)

## 背景（用户 2026-10-29 拍板“都按建议”）
T83 建好了 100 块题库和 harness，N = 10 试跑（只作校准）结果：① 10/10、② 5/10、③ 6/10，③′ 因超出授权书上限没跑起来。
本卡在不改 m2k 的前提下修两处实验配置，然后正式跑，按 T83 写死的三条规则出判定数字。题库、目标文字、预算、计价、
拼接、打分方法一律沿用 T83，不得改动。

## Required behaviour

### 1. Two configuration changes (before any formal run)
- **Worker network retry**: in `m2k.r0.toml` add `retry_delays = [10, 60, 180]` to `[models.worker]` (an existing m2k
  model option). Line ② must build its model from the same `[models.worker]` section, so both lines get it. Re-sign
  with `sign-release --slot r0` (next version). Nothing else in the config changes.
- **R0-only charter for ③′**: with `m2k trust sign-charter`, the dev Root `<dev-keys>\root.pem` (TEST_ONLY;
  never print, copy or move it) and R0's own issuer pin, sign a new charter whose caps are the current caps raised
  just enough to cover ③′'s parent budget at N = 30 (sum of 30 subtask budgets + the final review). Store it under
  `R0\trust\` and point only `m2k.r0.toml`'s `[trust].charter` at it; re-sign the release. Do not touch the demo,
  T82 or any other config, charter or key. Lines ② and ③ are unaffected (their per-block budget fits either charter).
- After both changes: `m2k --config m2k.r0.toml ping` passes, and one ③ smoke block on `b001` in `smoke` mode
  passes the trust check (not counted).

### 2. Formal runs (epoch 3; all earlier records stay, nothing earlier is reused for scoring)
- Process A (Codex agent): ① N = 10, then N = 30, then N = 100, one session each, timeout `min(N × 6, 480)` minutes.
- Process B (API lines, strictly one block at a time): ② on all 100 blocks, then ③ on all 100 blocks, then ③′ at
  N = 10, then ③′ at N = 30.
- A and B may run at the same time. Both use `Start-Process`, PIDs in `R0\full-*.pid`, logs in `R0\full-*.log`.
- ② / ③ are per-block and independent, so their N = 10 / 30 results are the first 10 / 30 of the 100-block run.
- The DeepSeek cap (USD 14 over all R0 records, unknown spend counted conservatively) stays. If it stops the run,
  stop and report; do not raise it.
- If the GLM coding-plan endpoint starts refusing for rate limits, let ③ record it as a review failure (as m2k does)
  and carry on; report how many blocks were affected.

### 3. Tally and decision numbers
- `tally --n 10`, `--n 30`, `--n 100` as in T83 (epoch 3 only).
- New `decide` command writes `decision.md` (numbers only, no prose verdicts beyond PASS / FAIL per rule):
  - for N = 100 and N = 30: the three rules of T83 with the inputs, the computed values and PASS / FAIL;
  - ③ non-delivery breakdown: test FAIL, review NO_GO, review failure, worker STALLED, MODEL_ERROR, rounds or budget
    exhausted, other — counts and block ids; bad merges with block ids;
  - ③ completion with review failures excluded; all-N-pass per line; ①'s claim label vs its truth;
  - cost split per line: DeepSeek, GLM, Codex (list-price equivalents), and seconds;
  - ③′ at N = 10 / 30: subtasks merged before it stopped, the stop reason, completion of the spliced trunk.

## Out of scope
- Any change to `m2k/`, tests, CI, other configs, the main ledger, the main workspace; any change to the pool,
  goal texts, per-block budgets, prices or the decision rules.
- GitHub Actions; Claude Code runs.

## Acceptance
- Epoch-3 records: ① 3 runs; ② and ③ 100 blocks each; ③′ 2 runs (or their refusal text).
- `summary-10.md`, `summary-30.md`, `summary-100.md`, `results-*.csv` and `decision.md` exist; every number in
  `decision.md` traces to `results.jsonl`.
- Independent check (gpt-6-astra, ultra, fresh session, read-only on `R0\`): recomputes every number in `decision.md`
  from `results.jsonl` and the truth files with its own script; ≤ 20 lines to `<worktrees>\T84-check.txt`.

## Review notes (written by reviewer)

- 2026-10-29 Claude: m2k's `retry_delays` only resends on ProviderBusy (HTTP busy codes); connection-level errors (URLError / SSL EOF / reset / timeout) become ModelError at once, so the added retry did not help. Harness-level fixes during the run (no m2k change): unknown spend counted at an upper bound; a block ending in a connection-level MODEL_ERROR is rerun once (② b008 only; ③'s b015 / b086 were not rerun — asymmetry noted below).
- 2026-10-30 Claude: results (epoch 3, `R0\decision.md`; the independent check `<worktrees>\T84-check.txt`, gpt-6-astra ultra, recomputed 2427 values with its own script and found no discrepancy). N = 100: ① 89, ② 72, ③ 52 blocks passing hidden; bad merges 11 / 28 / 19; list-price USD ① 2.80 (Codex), ② 1.16 (DeepSeek), ③ 2.79 (DeepSeek 1.16 + GLM 1.63). N = 30: 27 / 22 / 15. ③′ stopped at subtask b002 (1/10, 1/30). All three rules FAIL at N = 100 and N = 30.
- 2026-10-30 Claude: why ③ < ②: 25 ③ blocks hit the 15-round limit and were not delivered, of which 12 (plus b020) already held a correct function — the current kernel discards unfinished work, while ② merges whatever is left. With every ③ final function spliced, ③ would be 65 / 100, still below ② (not an official score). Harness defects that hurt ③ only: the visible-check command's `consider_namespace_packages=true` made RFC3986 tests import `boltons/tests` (b030, b073; also seen in b011, b041), and the workspace `.r0-pytest` folder was cleaned between acceptance and review, crashing b020 / b070. Both must be fixed before this benchmark is reused.
- 2026-10-30 Claude: the review added no measurable catch: GLM gave 71 GO and 1 NO_GO (on an empty implementation the visible test already failed); the share of bad blocks among merged ones is 27 % for ③ vs 28 % for ②. The visible check plus an LLM review is, in effect, the model testing itself. ① ran 100 blocks in one session for USD 2.80 at list price (heavy cache use), so the "cheap hands" cost premise does not hold against it here. ①'s `admitted` label at N = 100 is a regex mis-hit; its final message claims all 100 done (truth 89).
- Findings for later cards: unfinished work is thrown away (run the acceptance at budget exhaustion or force a final submit); repair only follows a test FAIL / NO_GO; connection-level model errors are not retried; review is the dominant cost and caught nothing beyond the visible test.

## Handoff note (written by implementer, ≤ 10 lines)
