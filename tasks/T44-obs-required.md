# T44: OBS required — reconcile receipts with the OBS log; the Gate, not the kernel, decides what `m2k run` reports

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T44-obs-required`
- **Line budget:** ≤ 130 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/obs.py`, `m2k/adjudicate.py`, `m2k/kernel.py`, `m2k/cli.py`, `m2k.example.toml`, `README.md`, `.github/workflows/m2k-task.yml`, `trust/test/m2k-task.toml`, `trust/test/release.json`, `tests/test_obs.py`, `tests/test_adjudicate.py`, `tests/test_release.py`, `tests/test_trust_required.py`, `tests/test_fingerprint.py`, `tests/test_trust_cli.py`, `tests/test_task_tree.py`, `tests/test_anchor.py`, `tasks/README.md`
- **Status:** DONE (#45)

## Goal
Stage 5, card 5. The old roadmap item "T44" was split in three (owner decision 2026-10-17): **T44** (this card),
**T63** (signed chain heads anchored outside the machine) and **T64** (release-manifest rollback detection, manifest
revocation, the adjudicator checking the manifest). Card ids are numbers only because `card-scope` matches `T\d+`.

After T42/T45, four gaps remain, and this card closes all four:
1. OBS is optional. A trusted run without `[obs]` only prints a warning, and the Gate then adjudicates from the ledger
   alone.
2. Nothing compares the receipts in the ledger with the OBS log. A receipt that the witness never logged, or a witnessed
   ledger event that later disappeared (truncated ledger, a re-run under a fresh ledger), goes unnoticed.
3. `m2k run` can show `✅ PASS` and exit 0 with nothing delivered: when the witness dies exactly at the verdict, or when
   any Gate refuses delivery (`_finish` only appends a reason; `report.verdict` stays PASS). Without `deliver_to` the
   adjudicator never runs at all, so the kernel's own PASS is shown unchecked. (T45 review; GLM said the same.)
4. A subtask whose verdict could not be witnessed still merges into the parent; the parent then ends UNKNOWN and
   nothing is delivered (T45 review). Merging is a release into the parent, so it must pass the same adjudication.

Owner decision (2026-10-17): **what `m2k run` reports follows the Gate / adjudicator, not the kernel.** The kernel's
verdict is a claim; if nothing was released, the run does not say PASS.

## Required behaviour
1. **OBS required with `[trust]`.** In `cmd_run`, right after the T43 release-manifest self-check and before the sandbox
   is chosen, the models are built or the Worker probe runs: a config with `[trust]` and no `[obs]` is refused with
   `拒绝启动：配置了信任链但没有 OBS 独立旁证（[obs]）。` plus one line naming the fix (`m2k obs keygen --out <dir>`, add the
   printed `[obs]` block, re-sign the release manifest). Append `startup.refused` `{task, code: "OBS_REQUIRED", reason}`
   under `m2k.startup` exactly as T43 does, and exit 2. `--untrusted` runs keep today's warning and `obs: null`.
   - `m2k trust status` prints one more line, `obs: ok` / `obs: missing` / `obs: key mismatch`, and exits non-zero
     unless it is `ok` (the CI workflow already runs `trust status` before the task).
   - `m2k trust init-dev --out D` also writes an OBS key (`D/obs/obs-key.pem`, same code path as `m2k obs keygen`) and
     prints the `[obs]` block next to the `[trust]` block, so the printed `sign-release` line signs a config that will
     start.
2. **Reconcile the ledger with the OBS log** (`obs.py`). A function that takes the ledger, a task id, the OBS log path
   and the OBS public key and returns a list of problems (empty = consistent):
   - the log itself passes `check_log(log, public_key)`;
   - **ledger → log:** every `obs.receipt` of the task is in the log: the line whose `seq` equals the receipt's
     `obs_seq` holds the identical receipt envelope;
   - **log → ledger:** every log line whose request names this task id has this ledger's `ledger_id`
     (else `OBS witnessed <task> in another ledger <id[:12]>`), its `ledger_seq` is an event of this task with the same
     hash and kind (else the ledger event is missing or changed), and the ledger holds that line's receipt.

   Read only this task's events (`ledger.events(task_id)`, T45 rule: no whole-table reads). The OBS log is read and
   parsed **at most once per Gate decision**, including the merged subtasks adjudicated inside it.
3. **`adjudicate(..., obs_public_key=None, obs_log=None)`.** With both, every reconciliation problem is a FAIL reason,
   for the task and for each merged subtask. Without `obs_log`, behaviour is unchanged. `ObsClient` keeps the log path
   it was started with, and the kernel passes it to every adjudication. `m2k adjudicate` passes the `[obs] log` from
   the config.
4. **The Gate decides what is reported.**
   - `_finish` of the top-level task always runs the adjudicator (with the OBS key and log when configured) and
     records `gate.adjudication`, also when the contract has no `deliver_to`. The ledger still has exactly one
     `verdict` event (T45 rule).
   - `RunReport.verdict` stays the kernel's verdict (what the ledger says). A new field holds what the run reports:
     **PASS only when** the kernel said PASS, the adjudicator said PASS, and, if delivery was requested, the delivery
     landed and was post-verified. Otherwise it is the kernel's FAIL or UNKNOWN, or **UNKNOWN** when the kernel said PASS
     but something after it refused.
   - `print_report` marks the run with that field. When it differs from the kernel's verdict it prints one line such as
     `❔ <task>: 未交付（内核判定 PASS，裁决器 FAIL：<first reason>）`. The exit code follows that field
     (PASS 0, FAIL 1, UNKNOWN 2), and so does the anchor note `m2k run` appends after the run.
   - `m2k status` shows the last `gate.adjudication` verdict next to the kernel's verdict.
5. **Subtask merges pass the adjudicator.** In `_attempt`, a subtask is merged only when, in addition to today's
   checks, `adjudicate(ledger, child_id, check_chain=False, obs_public_key=…, obs_log=…)` is PASS. Otherwise nothing is
   merged, the attempt's problem is `adjudication <verdict>: <first reason>`, and it counts as UNKNOWN. Once OBS has
   failed in this run, no new subtask attempt starts: the parent ends UNKNOWN with `OBS_UNAVAILABLE`, nothing merged,
   nothing delivered.
6. **CI config and workflow.**
   - `trust/test/m2k-task.toml` gets an `[obs]` table: `key = "obs/obs-key.pem"`, `log = "obs/obs.jsonl"`, and the
     `public_key_b64` of a new **fixed** CI OBS key. The config is pinned by the release manifest, so the public key
     cannot be generated per run.
   - Generate that key with `m2k obs keygen --out <dev-keys>\ci-obs` on this computer. **Never commit the
     private key, copy it into the repo, or print it.** In the handoff note, give the path of `obs-key.pem` so the owner
     can paste it into a new repository secret `M2K_TEST_OBS_KEY`.
   - `m2k-task.yml`: install that secret as `/root/m2k/obs/obs-key.pem` (root only, `umask 077`, same pattern and same
     "secret missing" error as `M2K_TEST_ISSUER_KEY`). The witness runs as root; commands run as `m2kw`, so the agent
     cannot read the OBS key in CI. In the summary step, run `m2k obs check` against the log with the public key and
     add `obs.jsonl` to the uploaded artifact. No other change to the workflow.
   - Re-sign `trust/test/release.json` over the new config bytes with the **TEST_ONLY Root of `trust/test`**
     (`--code dev`), exactly as T43 did. Find the Root private key on this computer by matching
     `trust/test/root-pin.json` (fingerprint `6357ec2addf1fd69`; candidates: `<offline-keys>`, the old
     project's `control/trust`). Never commit, copy or print it. If you cannot find it, stop and ask; never make a new
     Root. `sign-release` refuses to overwrite: sign to a scratch path outside the repo, then replace the committed
     file. Keep LF line endings.
7. **Docs.** `m2k.example.toml`: the `[obs]` block is no longer commented out and says it is required with `[trust]`
   (keep the one-line same-user limit). `README.md`: one or two lines where OBS is set up.

## Acceptance
`python -m unittest tests.test_obs tests.test_release tests.test_trust_required tests.test_fingerprint tests.test_trust_cli tests.test_task_tree -v`
and the full suite. Real witness processes and `ScriptedModel`, as in `tests/test_obs.py`. New tests must show:
- `m2k run` with `[trust]` and no `[obs]` (under a valid manifest) exits 2 with `OBS_REQUIRED`, one `startup.refused`,
  no `task.submitted`, no model call and no probe. `--untrusted` without `[obs]` still runs.
- `trust status` reports `obs: ok` / `missing` / `key mismatch` with the matching exit code. `init-dev` output plus
  the printed `sign-release` command gives a config that `m2k run` accepts.
- **Witness dies at the verdict** (deliver_to set): the ledger has one PASS `verdict`, `obs.failed`, a non-PASS
  `gate.adjudication`, no delivery; `m2k run` prints `未交付` and exits 2. The same task **without** `deliver_to` also
  exits 2.
- **Any Gate refusal reports non-PASS**: e.g. the delivery target already exists → exit 2, not 0.
- **Happy path** with and without `deliver_to`: exit 0, `gate.adjudication` PASS.
- **Reconciliation**, each FAIL with a reason naming it: a receipt in the ledger that is not in the OBS log (delete
  its log line and re-chain the log so `check_log` alone is clean; use a key you control for the re-chained rows); a log
  line for this task whose ledger event was changed or removed; the same task id witnessed under another ledger id
  (run the task in a fresh ledger with the same OBS log).
- **Subtask** (helpers from `tests/test_task_tree`): the witness dies while a subtask's verdict is witnessed → that
  subtask is **not** merged (no `merge_subtask` effect), no later attempt starts, the parent is UNKNOWN with
  `OBS_UNAVAILABLE`, nothing delivered.
- Wrap `Ledger.events` as in T45: a full OBS task with one subtask, plus the Gate, still makes **zero** calls with
  `task_id=None`. Count OBS log reads: one per Gate decision.

Existing tests that start `m2k run` with `[trust]` may change only to add an `[obs]` table (and re-sign their
manifest). Do not weaken any existing assertion.

The reviewer will re-run the T45 benchmark (scratch script, not committed) with a 50,000-line OBS log: "run with OBS"
must stay within 0.5 s of "run without OBS".

Also show in the handoff note that the new `trust/test/release.json` verifies against `trust/test/m2k-task.toml`
(scratch script calling `verify_release`, as in T43), and give the CI OBS public key fingerprint.

## After the merge (reviewer, not this card)
- The owner adds the secret `M2K_TEST_OBS_KEY` from the path in the handoff note.
- Claude dispatches Codex to update the three local demo configs (`<demo-dir>\m2k.dsh.toml`,
  `<dev-keys>\m2k.claude.toml`, `<dev-keys>\m2k.codex.toml`): one shared OBS key and log in
  `<dev-keys>\obs\`, an `[obs]` table in each, and each manifest re-signed with the dev Root
  (`<dev-keys>\root.pem`) to a new file, with `[trust] release` pointing at it; old files kept as `*.pre-T44.bak`.
  Then `m2k trust status` must say `release manifest: ok` and `obs: ok` for all three.
- A `workflow_dispatch` smoke run of `m2k-task` (as for T43) shows `obs: ok`, receipts, `gate.adjudication` PASS or a
  readable non-PASS, and `m2k obs check` clean.

## Out of scope
- Signed chain heads and external anchoring of the OBS log and the ledger (T63).
- Release-manifest versions and rollback detection, revoking manifests, the adjudicator checking the manifest, OBS
  witnessing the startup self-check (T64).
- A cloud notary.

## Review notes (written by reviewer)

- 实现前澄清 2026-10-17（用户更正，以此为准）：Gate 对整份 OBS 日志做链与格式核对（不带公钥的 check_log 部分），再验最后一行的签名及其 obs_seq/obs_prev 与行 seq/prev 的绑定，并验本任务及被合并子任务的请求行、其账本 obs.receipt 指向行的签名。每张回执签了前一行原始字节哈希 obs_prev，无私钥者改行后重链会使末行已签 obs_prev 不匹配。测试覆盖：重链后的其他任务行改动也报“OBS 日志被改动”；本任务行改动、其他密钥签发的回执均报出。
- 实现前澄清 2026-10-17（用户）：性能口径改为一次 Gate 决定（含被合并子任务）在 50,000 行 OBS 日志上的对账耗时，本机 Python 3.13 测 3 次中位数，目标 ≤0.5 s；若纯链核对加 JSON 解析已超标，照实写入交接说明，不增加复杂缓存或索引。
- 实现前澄清 2026-10-17（用户）：obs.serve 启动时带公钥的全量验签保留 T42 原行为，不在本卡修改；交接说明记录启动耗时，后续由 T63 结合外部锚定检查点处理。
- 2026-10-17 Claude 批准：T44 在 verdict 后记录 gate.adjudication，test_anchor 的锚点断言改为核对 Gate 之后的实际账本头。

- **PASS (Claude, 2026-10-17), merged as #45.** ci / card-scope green; GLM-5.3 GO. Linux, Python 3.13: full suite 546 OK
  (7 skipped). `m2k/` +125. Independently verified `trust/test/release.json` (Root 6357ec2addf1fd69, LF and CRLF) and the
  CI OBS public key fingerprint `6b24a8db231e85f0`. Out-of-card but correct: the witness runs with cwd = framework code
  root, so a workspace `m2k/` package can no longer shadow `python -m m2k` in CI.
  Accepted: Gate reconciliation 0.74 s on a 50,000-line OBS log (0.63 s is chain + JSON parse); witness start-up 6.2 s.
  Carried to T63: both costs (verify only after the last anchored checkpoint). Later hardening (GLM, non-blocking):
  malformed `request` rows raise KeyError in `reconcile`; `m2k adjudicate` with `[obs]` lacking `log`; `trust status`
  says `obs: ok` before the log exists.

## Handoff note (written by implementer, ≤ 10 lines)

- 基于 main `6e934b1`、分支 `task/T44-obs-required`；已实现强制 OBS、双向对账（含末行验签/位置绑定）、Gate 报告与合并前裁决；`m2k/` 新增 125/130 行。
- 使用 `<python>`（3.13.11）：Acceptance 119 项通过；全套 546 项 OK（9 skipped）；锚点定向用例通过。
- 按 Claude 批准更新锚点断言：PASS/FAIL/UNKNOWN 均核对唯一 verdict、内核结果、Gate 后实际账本头和锚点 note；原有链检查、记录计数与其他断言保留。
- CI OBS 私钥：`<dev-keys>\ci-obs\obs-key.pem`，供所有者设置 `M2K_TEST_OBS_KEY`；未复制进仓库或输出内容。
- CI OBS 公钥指纹：`6b24a8db231e85f0`（SHA-256 `6b24a8db231e85f0d506ae74699615ac6bd0ed0db0bfd806f94a33fc90781a03`）。
- 新 `trust/test/release.json` 经 `verify_release` 核验 LF/CRLF 均 PASS：TEST_ONLY、code=dev、Root `6357ec2addf1fd69`；config SHA-256 `16f3522ee20907602dcdbfa1f1200c7619b622fbf1ad09aeb051727c68e55276`。
- 50,000 行有效 OBS 日志、父任务含一个已合并子任务（含末行验签/绑定）：Gate 三次 0.7353/0.7484/0.7226 s，中位 0.7353 s，超过 0.5 s，交复审决定；纯链/格式/JSON 中位 0.6262 s，未加缓存或索引。
- 同日志旁证启动至首张回执三次 6.2081/6.1524/6.3126 s，中位 6.2081 s；obs.serve AST 与 main 相同，原全量验签中位 5.7904 s，留 T63。
- 三份本地演示配置/清单未动，未开 PR；CI/GLM/审阅者裁定及合并后设置 secret、演示配置更新和 workflow_dispatch 均待所有者/审阅者执行。
