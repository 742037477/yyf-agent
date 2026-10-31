# T47: Delivery receipts — every file in a delivery folder must match an OBS-signed receipt; anything else is unverified

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T47-delivery-receipt`
- **Line budget:** ≤ 240 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/delivery.py` (new), `m2k/kernel.py`, `m2k/ledger.py`, `m2k/obs.py`, `m2k/adjudicate.py`, `m2k/cli.py`, `tools/task_result.py`, `.github/workflows/m2k-task.yml`, `README.md`, `tests/test_delivery.py` (new), `tests/test_obs.py`, `tests/test_anchor.py`, `tests/test_release.py`, `tests/test_trust_required.py`, `tests/test_pipeline.py`, `tests/test_adjudicate.py`, `tests/test_task_tree.py`, `tasks/README.md`
- **Status:** DONE (#48)

## Goal
Stage 5, the last implementation card before the stage 5 attack drill (T46). Stage 5 decision in
`docs/ARCHITECTURE.md`: **safety does not come from a file appearing in the delivery folder, but from a valid receipt.
Output with no receipt, or whose receipt does not match the files, is unverified.**

Today (after T64) that rule is not implemented:
1. `_deliver` copies the candidate to `deliver_to/<task_id>` and records `delivery.intent` / `delivery.result`, but
   neither event has an OBS receipt, and the T63 checkpoint is recorded *before* the delivery, so the delivery is never
   anchored. `delivery.result` holds only one digest, not the per-file hashes.
2. Nothing can check a delivery folder. A file added to `deliver_to/<task_id>/`, or a whole `deliver_to/<fake-id>/`
   written by the agent, looks exactly like a real delivery.
3. When G7 post-verification fails (`post_verified: false`), the copied files stay in place.
4. Nothing refuses a `deliver_to` that overlaps the workspace, so the agent can write the delivery folder directly.
5. The CI release path (`tools/task_result.py`, which pushes files to the card branch) adjudicates without the OBS key,
   the OBS log or the Root pin — weaker than the Gate.

After this card, a delivery is a folder **plus a portable receipt file** next to it. `m2k check-delivery` lists every
entry of a delivery root and marks anything without a matching, valid receipt as **未经核验**.

Owner decisions (2026-10-17):
- **Portable receipt.** Each delivery writes `deliver_to/<task_id>.m2k-receipt.json` (the OBS-signed receipt plus the
  exact ledger event it signs). With only the OBS public key, anyone can check the files; with the ledger and OBS log,
  the check also re-runs the adjudication. (Groundwork for stage 8, handing results to others.)
- **Stage, then release.** Files are copied into a staging folder and checked, the delivery receipt is witnessed, the
  T63 checkpoint is recorded and published **after** it (so the checkpoint covers the delivery receipt), and only then
  is the staging folder renamed into place. If anything fails before the rename (G7, OBS, PRODUCTION anchor), the
  staging folder is deleted and nothing is delivered.
- **Report, do not block.** Unverified entries already in the delivery root do not stop this run's delivery
  (otherwise planting one file would block every delivery). `m2k run` prints a warning; `m2k check-delivery` lists them.
- **CI release path included.** `tools/task_result.py` adjudicates exactly like the Gate (OBS key + log + Root pin +
  revocations) and requires the Gate's own `gate.adjudication` PASS.

## Required behaviour
1. **`deliver_to` must not overlap the workspace.** In `Kernel.run`, next to the existing "ledger inside the workspace"
   check, refuse (`KernelError`, before anything is recorded) when `deliver_to` is inside the workspace or the
   workspace is inside `deliver_to`. `m2k run` therefore exits non-zero with no `task.submitted`.
2. **Delivery receipt event.** `delivery.result` is recorded through the witnessed path (`_record`) and its payload is
   `{target, candidate, files: {rel: sha256}, landed, post_verified}`; `files` uses the same per-file digest as the
   frozen candidate (`verify.freeze_one`), `target` is the final folder. Add `delivery.result` to `obs.DECISIVE`, so
   `adjudicate(..., obs_public_key=…)` requires its receipt whenever the event exists. (Runs recorded before T47 then
   FAIL a later `m2k adjudicate` with the OBS key; that is expected, as after T64.)
3. **Stage, then release** (`_finish` / `_deliver`, top-level task, kernel PASS, adjudicator PASS, `deliver_to` set):
   1. Refuse (`delivery.refused`, as today) when `deliver_to/<tid>`, `deliver_to/<tid>.m2k-receipt.json` or the staging
      folder `deliver_to/.m2k-staging-<tid>` already exists.
   2. `delivery.intent` (unchanged), copy the candidate into the staging folder, freeze it; it must equal the candidate
      file by file and in digest. Otherwise delete the staging folder and record `delivery.refused` (no
      `delivery.result`).
   3. Record `delivery.result` (witnessed, item 2). Then the T63 checkpoint: `anchor.checkpoint`, anchor line, publish —
      unchanged logic, but now **after** `delivery.result` for this case. Runs without a delivery keep today's order.
   4. If the `delivery.result` receipt is missing (OBS failed) or the anchor rule refuses release (`ANCHOR_UNAVAILABLE`,
      T63 rule), delete the staging folder and record `delivery.refused` with the reason. Nothing is delivered and the
      run reports per the T44 rule.
   5. Write the receipt file (item 4) via a temporary name and rename, then rename the staging folder to
      `deliver_to/<tid>`. A failure here is `delivery.refused` (and the run is not PASS); whatever is left is reported
      by `check-delivery`.
   - With `obs=None` (`--untrusted`, direct kernel use in tests): same staging, no receipt file. `m2k run` prints
     `警告：本次交付没有回执（未配置 OBS），m2k check-delivery 会把它列为未经核验。`
   - After a delivery attempt, `m2k run` does a **structural** scan of the delivery root (no hashing, no ledger reads):
     if any entry is not a `<name>/` + `<name>.m2k-receipt.json` pair, print
     `警告：交付目录里有 N 项未经核验的产出（m2k check-delivery <dir> 查看）。`
4. **Receipt file** (`m2k/delivery.py`). Canonical JSON, UTF-8:
   `{schema: "m2k.delivery/1", ledger_id, event: {seq, task_id, kind, op_id, payload, ts, prev_hash, hash}, receipt}`,
   where `event` is the `delivery.result` event exactly as the ledger hashed it and `receipt` is its OBS receipt
   envelope. Expose the ledger's event hash as a public function in `ledger.py` (`event_hash`, same computation as
   today; keep `_event_hash` working) so the checker recomputes `hash` from the event fields.
5. **Checker** — `check_delivery(root, obs_public_key, ledger=None, obs_log=None, **adjudication_inputs)` in
   `delivery.py`, returning one result per entry of `root` (OK or a list of problems). Rules:
   - A delivery is a folder `<name>/` with a sibling `<name>.m2k-receipt.json`. Anything else is **unverified**, with
     a reason: a folder without a receipt file, a receipt file without its folder, a staging leftover, any other file,
     a symlink or special file anywhere (never followed).
   - **Standalone checks** (only the OBS public key): the schema is right; recomputing `event_hash` over `event` gives
     `event.hash`; `event.kind == "delivery.result"`; `event.task_id == <name>`; the payload's `target` ends in
     `<name>` (compare the last path component only, so a moved delivery still checks); `post_verified` is true; the
     receipt verifies with the key and repeats the request built from `event` and `ledger_id` exactly; the files on
     disk equal `payload.files` exactly — each **missing**, **extra** and **changed** file is named.
   - **With the ledger and OBS log** (in addition): the ledger has the same `ledger_id` and an event at `event.seq`
     with the same hash; the OBS log line at the receipt's `obs_seq` holds the identical receipt; the last
     `gate.adjudication` of the task is PASS for the same candidate; and
     `adjudicate(ledger, <name>, check_chain=False, obs_public_key, obs_log, root_pin, revocations)` is PASS now (so a
     manifest revoked after the run makes the delivery unverified — T64 decision). Verify the ledger hash chain
     **once** per call, read and parse the OBS log **once** per call (`obs.log_snapshot`, shared with every
     adjudication), and read only each task's own events (T45 rule: no whole-table reads).
6. **CLI.**
   - `m2k check-delivery DIR`: full check with the ledger, `[obs]` and `[trust]` from the config.
     `m2k check-delivery DIR --standalone --public-key B64`: standalone checks only, no config needed.
     One line per entry: `OK <name> (<n> files)` or `未经核验 <entry>: <problem>; …`. Exit 0 only when every entry is
     OK (an empty root is OK and says so).
   - Move the config → adjudication-inputs loading (`root_pin`, `revocations`, `obs_public_key`, `obs_log`) used by
     `cmd_adjudicate` into one helper and use it in `cmd_adjudicate`, `check-delivery` and `tools/task_result.py`.
     A missing `[obs] log` is then a clear error, not a `KeyError`.
   - `m2k status` shows `delivery_receipt` (the `obs_seq` of the `delivery.result` receipt) when there is one.
7. **CI release path** (`tools/task_result.py`). New required `--config`. Refuse (exit 1, `::error::`, nothing copied)
   unless the config has `[trust]` and `[obs]`; adjudicate with the helper's inputs (check_chain on); and require the
   task's last `gate.adjudication` to be PASS for the same candidate as the last `candidate.frozen`. Everything else
   in the tool is unchanged. In `.github/workflows/m2k-task.yml`, the only change is adding
   `--config /root/m2k/m2k.toml` to the `task_result.py` call.
8. **Docs.** `README.md`: next to the `deliver_to` row, one or two lines on the receipt file and `m2k check-delivery`
   (standalone vs full).

## Acceptance
`python -m unittest tests.test_delivery tests.test_obs tests.test_anchor tests.test_release tests.test_trust_required tests.test_pipeline tests.test_adjudicate tests.test_task_tree -v`
and the full suite. Real witness processes, temporary Roots, `ScriptedModel`, temporary bare git repositories for the
anchor remote (skip if `git` is missing). New tests in `tests/test_delivery.py` must show:
- **Happy path** (OBS, TEST_ONLY, `deliver_to` set): the folder and the receipt file exist; `delivery.result` has an
  `obs.receipt`; `anchor.checkpoint` comes after `delivery.result`, and the final folder does **not** exist yet when
  the anchor is published (patch `anchor.publish` to assert it); full and standalone `check-delivery` are OK; exit 0;
  `m2k adjudicate` PASS after the delivery.
- **Planted / changed output**, each reported by both modes and naming the file: an extra file in the delivery folder;
  a changed file; a deleted file; a folder `deliver_to/fake/` with no receipt; a stray file in the root; a staging
  leftover; a real receipt file copied next to a fake folder (name and files differ); a receipt whose event payload
  was edited (hash mismatch); a receipt re-signed with another OBS key; a symlink inside a delivery (skip where
  symlinks cannot be created).
- **Valid receipt, run no longer trusted:** revoke the run's release manifest after the delivery (T64) → full check
  reports the adjudication failure; standalone stays OK (it cannot know).
- **Ledger mismatch:** the receipt names a seq whose ledger event differs, or the OBS log line at `obs_seq` differs →
  full check reports it.
- **G7 failure** (patch the copy so a file lands changed): no staging folder, no final folder, no receipt file,
  `delivery.refused`, no `delivery.result`, `m2k run` exits 2.
- **OBS dies at `delivery.result`:** nothing released, staging removed, UNKNOWN, exit 2.
- **PRODUCTION publish fails:** staging removed, nothing released, `ANCHOR_UNAVAILABLE`, exit 2. **TEST_ONLY** same
  failure: warning, delivered with a receipt file.
- **Overlap refused:** `deliver_to` inside the workspace, and workspace inside `deliver_to` → refused, no
  `task.submitted`.
- **Warnings:** a run whose delivery root already holds an unverified entry still delivers and prints the
  `未经核验` warning; an `--untrusted` delivery prints the no-receipt warning and `check-delivery` lists it.
- **`tools/task_result.py`:** a forged ledger tail with a real OBS setup → refused (adjudication FAIL); a ledger whose
  last `gate.adjudication` is not PASS → refused; no `[obs]` in the config → refused; a normal witnessed PASS → allowed.
- Wrap `Ledger.events`: a full `check-delivery` over two deliveries makes **zero** calls with `task_id=None`; the OBS
  log is read once.

Existing tests may change only where they assert the old event order around the checkpoint and delivery, or the old
`delivery.result` payload; say which ones in the handoff note. Do not weaken any assertion.

## After the merge (reviewer, not this card)
- No config changes: `trust/test/m2k-task.toml` and the three demo configs are untouched, so **no manifest needs
  re-signing**. If the implementer finds that a demo task contract has `deliver_to` overlapping its workspace, list it
  in the handoff note (do not change files outside the repo).
- A `workflow_dispatch` smoke run of `m2k-task` with a task that sets `deliver_to = "/root/m2k/out"`: the CI release
  path still pushes on PASS with the stricter `task_result.py`, and `status.json` shows `delivered_to` and
  `delivery_receipt`.
- Then the stage 5 attack drill (T46, DeepSeek via m2k), including "put files into the delivery folder bypassing the
  Gate" and "forge a receipt".

## Out of scope
- Subtask merges (internal releases, already adjudicated since T44).
- Making CI release from the delivery folder instead of the workspace; deleting or archiving old deliveries.
- Requiring that a receipt's `obs_seq` is covered by a published checkpoint (`check-ledger --remote` covers the OBS
  log as a whole).
- A cloud notary; any new key or signature type (the OBS receipt is the signature).

## Review notes (written by reviewer)

- **PASS (Claude, 2026-10-17), merged as #48** (head f35504b). ci / card-scope green; GLM-5.3 GO. Linux, Python 3.13: full
  suite 627 OK (8 skipped). `m2k/` +219 / 240. No config changed, so no manifest re-signed.
  Later (non-blocking): a symlinked delivery root makes the run-end structural scan in `_run` raise instead of warn; a
  receipt or `.tmp` file can stay behind after a folder-rename failure (reported by `check-delivery`, as allowed).

## Handoff note (written by implementer, ≤ 10 lines)

- 基于 main `474d40f`，分支 `task/T47-delivery-receipt`；实现暂存逐文件校验、OBS 交付回执、回执之后的锚点与最终重命名，失败不交付。
- 新增 `check-delivery` 独立/完整核验与 `status.delivery_receipt`；完整核验每次仅核验账本链一次、读取/解析 OBS 日志一次，零次 `Ledger.events(None)`。
- `m2k/` 新增 219/240 行；CI 导出复用 OBS/Root/吊销输入并要求同候选 Gate PASS；工作流仅增加 `--config` 参数。
- `<python>`（3.13.11）：基线相关 195 项 OK；卡片验收 225 项 OK（2 skipped），全套 627 项 OK（11 skipped）。
- 新增 30 项交付测试，使用真实 OBS 进程、临时 Root、ScriptedModel、临时裸 git 仓库；Windows 目录联接不跟随实测通过，POSIX 特殊文件/符号链接因本机条件跳过。
- 既有测试仅调整 `tests/test_obs.py` 的新增交付回执数量、事件时序及 G7 载荷断言，和 `tests/test_release.py` 的检查点/交付时序断言；保留其他检查。
- 仓库外演示配置、密钥目录、锚点克隆未改，未开 PR；远端 CI/card-scope、GLM-5.3 GO、审阅批准及合并后工作流烟测待审阅者继续。
