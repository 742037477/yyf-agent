# T83: R0 scale benchmark (part 1) — a 100-block task pool, a three-line harness and an N = 10 pilot

- **Assignee:** Codex (gpt-6.1-sol, max) builds, validates, pilots; an independent Codex check (gpt-6-astra, ultra,
  read-only) re-validates the pool; Claude analyses; the user decides after the pilot
- **Branch:** none. Nothing in this repo changes except this card (Review notes, handoff note) and `tasks/README.md`.
- **Line budget:** none in `m2k/` (m2k code must not change). Everything else lives outside the repo; keep the
  scripts small (≤ 750 lines in total, not counting the task data).
- **Where:** `<experiments>\R0\`. m2k code runs from a clean worktree `<worktrees>\R0-main` at
  `origin/main` (create it once; keep it until R0 is finished, T84 uses it too), never from `<repo>`.
- **Status:** DONE (pilot finished 2026-10-29; full runs in T84)

## 背景（用户 2026-10-29 拍板：转向“规模化、便宜模型、可核验”，从 R0 开始；全文见项目文档 `claude/m2k-refactor-plan.md`）
T82 证明单任务、顶级模型时 m2k 比“干完自己跑验收”多拦下 0 次。重构方案的价值主张是另一回事：**大任务拆成很多块，
叶子交给便宜模型，每块合并前机械验收、坏了退回重做，总完成率不随块数下降，花费远低于贵模型单干。**
R0 用现有内核量这个主张的基线，判定标准事先写死（见 “Decision rules”）。本卡建题、写 harness、跑 N = 10 试跑后停下；
T84 跑 N = 30 / 100 并统计。

三条对照线：
- **① 贵模型单独做**：gpt-6-astra（经 Codex CLI，用户 2026-10-29 选定）一个会话拿到全部 N 块，在一个工作区做完。
- **② 便宜模型拆分、不验证**：DeepSeek-V4-Flash（端点模型名 `deepseek-flash`）每块单独做，做完直接合并。
- **③ 便宜模型 + m2k**：每块一次 `m2k run`（原生 Worker = DeepSeek-V4-Flash，可见验收受保护，GLM-5.3 审查，
  `max_repairs = 1`），只合并 PASS 的块。
- **③′（只记录、不判定）**：现有内核原样——一个契约写 N 个 `[[subtasks]]`，遇到第一个失败的子任务就整体停下。

## Goal
Under `<experiments>\R0\` there is a validated pool of 100 independent “blocks” hollowed out of real
open-source Python libraries, each with a visible check (given to every line) and a hidden check (never in any
workspace); a harness that runs lines ①, ②, ③ and ③′ with identical inputs, splices every line's output into the
same pristine trunk the same way, and scores the trunk with the hidden checks; and a pilot on the first 10 blocks.

## Required behaviour

### 1. Task pool (`pool/`)
- **Sources**: 2–4 pure-Python libraries (MIT / BSD / Apache-2.0; stdlib-only at runtime; test suite runnable with
  `<python>` + pytest — if pytest is missing there, create `R0\venv` from that Python and
  install pytest into it; never install into the base Python). Prefer libraries that are not among the most famous
  (record GitHub stars, version and commit in `pool/sources.json`); avoid e.g. more-itertools, toolz, requests, attrs.
  Vendor a pinned copy of each library under `pool/upstream/<lib>/` (source + tests, with its licence file).
- **Blocks**: 100 blocks; one block = one top-level function or one method, upstream implementation 5–60 lines,
  a mix of sizes. Hollowing replaces the body with `raise NotImplementedError` and keeps the signature, decorators and
  docstring. If the upstream docstring is missing or too thin to implement from, write a short spec docstring from the
  upstream documentation and tests (describe behaviour, never paste implementation). `pool/blocks.jsonl`: one line per
  block with `id` (b001…b100), `lib`, `file`, `qualname`, `upstream_lines`, `visible` (1–2 pytest node ids) and
  `hidden` (every other upstream test that exercises the block, ≥ 3 node ids where they exist; fewer only if upstream
  has fewer, then say so in `note`).
- **Order**: fix a seeded random order, stratified by library and size, so that the first 10 and the first 30 blocks are
  representative of the 100. N = 10 / 30 / 100 always means the first N blocks in this order.
- **Seeds**: `seed-N/` (N = 10, 30, 100) = the libraries with the first N blocks hollowed (all other code intact) plus
  **only the visible tests** of those N blocks (a pruned copy of the upstream test files that keeps the fixtures and
  helpers they need). Hidden tests live only in `pool/upstream/` and are never copied into any seed or workspace.
- **Validation (`r0.py validate`, all must hold for every block, results in `pool/validation.json`)**:
  1. upstream as-is: the block's visible and hidden tests pass;
  2. `seed-100` code (all 100 hollowed): the block's visible and hidden tests fail;
  3. `seed-100` code with **only this block** restored to upstream: its visible and hidden tests pass
     (= the block is independent of every other hollowed block). Replace any block that fails 1–3.
  4. the full upstream suite run on upstream code passes (record skips / expected failures; they stay excluded).

### 2. Configs (do not touch the three demo configs or the T82 configs)
- `m2k.r0.toml` (line ③ and ③′): built like the T82 configs (`<experiments>\T82\m2k.exp-*.toml` show how the
  trust / OBS / anchor sections were reused), with: its own ledger `R0\ledger.sqlite`; `[models.worker]` =
  `kind = "openai_compat"`, `base_url = "https://api.deepseek.com/v1"`, `model = "deepseek-flash"`, `api_key_file` =
  the DeepSeek key file path already used in `R0\m2k.ping.toml`; `[models.reviewer]` = GLM-5.3 exactly as in
  `<dev-keys>\m2k.codex.toml`; `[sandbox] backend = "none"`; `[watch]` as in the T82 configs. Sign it with
  `sign-release --slot r0`.
- **Keys: never print, copy, move or commit any key or private key; configs hold paths only.** The DeepSeek key file
  may be shared with other uses of the user — that is expected.
- Line ①: the Codex command of `<dev-keys>\m2k.codex.toml` with `-m gpt-6-astra` added (the user's default
  reasoning effort; record model and effort per run from Codex's JSON output). It is run through
  `m2k.external.CliAgent._exec` as in T82's A line, not through `m2k run`.
- `prices.toml` (fixed before any run, with source URL and date): USD per million tokens — deepseek-flash 0.14 in /
  0.28 out; glm-5.3 1.40 in / 4.40 out; gpt-6-astra 5.00 in / 0.50 cached in / 25.00 out.

### 3. Harness (`r0.py`, Python 3.13, stdlib + m2k; reuse what fits from `<experiments>\T82\run.py`)
- **Same inputs for every line.** Every block's goal text is generated once from `blocks.jsonl` (qualname, file, the
  visible test command) and used verbatim by ①, ②, ③ and ③′. Same tools for ②, ③, ③′
  (`list_files, read_file, write_file, edit_file, run_command`), same per-block worker budget for ② and ③:
  `max_rounds = 15`, `max_model_calls = 20`, `max_tokens = 400000`, `max_commands = 15`, timeout 20 min.
- **① (`run --line 1 --n N`)**: one fresh copy of `seed-N/`; one Codex session gets all N goals plus the visible test
  commands; timeout N × 6 min (N = 10: 60 min). Record exit, seconds, tokens (input / cached input / output if Codex
  reports them), the final message and whether it claims to be done (`claimed` / `admitted` / `unclear` + the quote).
- **② (`run --line 2 --n N`)**: per block, a fresh copy of `seed-100/`; call the same worker loop the kernel uses for a
  top-level task (`m2k.worker.run_worker` with the same message building, tools and gates, no protected files) with a
  throwaway ledger `R0\scratch\line2.sqlite`. No acceptance run, no review, no repair: whatever it leaves is taken.
- **③ (`run --line 3 --n N`)**: per block, `m2k --config m2k.r0.toml run <block>.toml` on a fresh copy of `seed-100/`
  (unique task id per run): `protected` = all test files in the seed; `[acceptance]` = that block's visible test
  node ids via pytest; `max_repairs = 1`; the budget above; goal + constraint “only change the function <qualname> in
  <file>”. Taken only if m2k delivered (PASS). Record verdict, reasons (`m2k status`), worker and reviewer tokens from
  the ledger, seconds, and whether a non-delivery was caused by the review step failing (reviewer error / unparsable
  reply / UNKNOWN) rather than a FAIL or NO_GO.
- **③′ (`run --line 3p --n N`)**: one contract on a fresh copy of `seed-N/` with N `[[subtasks]]` in pool order (each:
  writes = its library file, acceptance = its visible tests, budget above, `max_reassign = 1`; parent acceptance = all
  visible tests; parent budget = the sum + room for the final review). If the current kernel refuses this contract,
  record the refusal text and skip the line (it is not judged). Record how many subtasks were merged before it stopped.
- **Splicing (identical for all lines)**: the scored trunk for a line = pristine `seed-100` code with, for each taken
  block, the block's function / method source (from `def` through its end, with decorators) extracted with `ast`
  from that line's output and put in place of the hollow version. Nothing else from any line's output is kept
  (edits to other functions, tests or files are dropped). A block whose source cannot be extracted or does not parse
  counts as not done. For ① and ③′ every block is taken from their final workspace; for ② every block is taken; for ③
  only delivered blocks.
- **Truth (`truth --line L --n N`)**: copy the trunk to a temp folder, overlay the pristine upstream tests, run every
  block's hidden + visible tests (pytest node ids, one run, `--timeout` per test if pytest-timeout is available,
  otherwise a whole-run timeout), record per block pass / fail and the failing test names.
- **Tamper record**: per run, hashes of the seed's test files before / after (and new `conftest.py`,
  `sitecustomize.py`, `__pycache__`, `.pth` files). It does not change the score (splicing drops them); it is reported.
- **Cost**: per run, tokens × `prices.toml`. Lines ② / ③ / ③′ price every input token at the cache-miss rate (the m2k
  ledger does not record cache hits; this is deliberately unfavourable to m2k). Line ① prices cached input at the cached
  rate when Codex reports it.
- **Spending cap**: the harness sums DeepSeek spend over all R0 runs (from `results.jsonl`) and refuses to start a new
  block once it exceeds USD 14 (≈ ¥100); GLM and Codex are subscription quota and are only reported.
- **Resumable and orderly**: one JSON line per finished block run (② / ③) or whole run (① / ③′) in `results.jsonl`;
  reruns skip what is recorded. ② / ③ / ③′ blocks run one at a time; line ① may run at the same time as them (separate
  process and folders), never two ① runs at once.
- `tally --n N` writes `summary-N.md` and `results-N.csv` (one row per block × line, plus one row per line): total
  completion (blocks passing hidden ÷ N), all-N-pass yes/no, bad merges (taken but hidden fails), cost (USD), seconds,
  ③'s review-failure non-deliveries and its completion with those excluded, ③′'s stop point, ①'s claim label.
  Numbers and rows only, no prose conclusions.

### 4. Pilot (this card's runs)
- After `validate` passes, run N = 10 for ①, ②, ③ and ③′, then `truth` and `tally --n 10`. Stop and report.
- Do not start any N = 30 / 100 run (that is T84, after the user decides).

## Decision rules (fixed before any run; judged in T84 on N = 100, N = 30 as supporting evidence; the user decides)
1. ③ total completion ≥ 90 % of ①'s.
2. ③ cost ≤ 40 % of ①'s.
3. ③ total completion ≥ ②'s + 10 percentage points, and ③'s bad merges ≤ half of ②'s.
- All three hold → the value claim stands, go to R1. Rule 3 fails → m2k's verification adds little over the model
  testing itself; rethink the direction. Rule 1 fails → the cheap leaf model is too weak; prioritise R4 (repair,
  re-review) or a stronger leaf model. Rule 2 fails → overhead too high; prioritise R4's sampled review.
- Recorded, not judged: review-failure non-deliveries and completion with them excluded; ③′; seconds; tampering.
- **Pilot gate (N = 10 is calibration, not judged)**: if ② completes ≥ 95 % or ≤ 40 % of the 10 blocks, the pool is
  too easy or too hard — report it and wait; changing the pool needs the user's approval and a new validation.

## Out of scope
- Any change to `m2k/`, tests, CI, the demo or T82 configs, the main ledger or the main workspace.
- GitHub Actions (everything runs on the user's computer); Claude Code runs.

## Acceptance
- `r0.py validate`: 100 blocks, rules 1–4 hold for each; `pool/validation.json` written; no hidden test appears in any
  `seed-N/` (checked by node id and by file hash).
- Independent check (gpt-6-astra, ultra, read-only, fresh session): reruns `validate` from a clean copy, checks a sample
  of 10 blocks by hand (docstring gives no implementation away; hidden ≠ visible; the block really is independent),
  confirms no key appears in any file under `R0\`; ≤ 20 lines to `<worktrees>\T83-check.txt`.
- Pilot: N = 10 records for ①, ②, ③ (and ③′ or its refusal); `summary-10.md` numbers trace to `results-10.csv`.

## Review notes (written by reviewer)

- 2026-10-29 Claude: pool built from boltons (56 blocks), python-string-utils (27), rfc3986 (13), inflection (4); `validate` rules 1–4 hold for 100/100 blocks (3 blocks replaced); no hidden test in any seed. Harness and pool live in `<experiments>\R0\` (local git).
- 2026-10-29 Claude: the independent check (gpt-6-astra, ultra, fresh session, `<worktrees>\T83-check2.txt`) re-ran `validate` and confirmed the pool and the fairness of the lines; it found three harness defects (a failed pytest start could reuse a stale report and pass; an interrupted block was rerun and under-counted; truth / tally were not bound to the batch). All three were fixed with offline tests before the pilot was scored. b010's spec is thin (noted, kept).
- 2026-10-29 Claude: pilot N = 10 (epoch 2, calibration, not judged): ① 10/10, USD 1.04 (list price), 343 s; ② 5/10, USD 0.12, 5 bad merges; ③ 6/10 (7 delivered, b008 passed the visible check and review GO but fails hidden = 1 bad merge), about USD 0.33 of which about two thirds is GLM review, 1081 s. ③ losses: b002 worker STALLED, b004 DeepSeek SSL drop (no `retry_delays` on the worker), b007 rounds exhausted, b008 bad merge; `max_repairs = 1` never fired (repair only follows a test FAIL or NO_GO). ③′ was refused at start: `trust: EXCEEDS_CHARTER` (the subtask budgets add up to more than the charter caps). ② at 50 % is inside the pilot gate, so the pool's difficulty is kept.
- 2026-10-29 user: go to T84 with the worker's `retry_delays` added for ② and ③ alike, and an R0-only TEST_ONLY charter with larger caps so ③′ can run (N = 10 and 30, recorded, not judged).

## Handoff note (written by implementer, ≤ 10 lines)
