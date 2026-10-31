# T63: Signed checkpoints of the ledger and OBS heads, published to the `ledger-anchors` branch; rewrites and force pushes are found

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T63-external-anchor` (start from main **after T44 is merged**)
- **Line budget:** ≤ 230 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/anchor.py`, `m2k/kernel.py`, `m2k/cli.py`, `m2k/obs.py`, `tools/anchor_push.py`, `m2k.example.toml`, `README.md`, `.github/workflows/m2k-task.yml`, `trust/test/m2k-task.toml`, `trust/test/release.json`, `tests/test_anchor.py`, `tests/test_obs.py`, `tests/test_release.py`, `tests/test_trust_required.py`, `tests/test_fingerprint.py`, `tests/test_trust_cli.py`, `tasks/README.md`
- **Status:** DONE (#46)

## Goal
Stage 5, the second of the three cards split from the old T44 (owner decision 2026-10-17; see `tasks/T44-obs-required.md`).

OBS receipts stop a forged ledger tail. They do not stop an attacker who holds the OBS key (the documented same-user
limit of a local OBS) from rewriting the ledger **and** the OBS log into a new, self-consistent pair, or from deleting
both and starting again. Only a record published **before** the rewrite exposes it. T09 built anchor files and
`tools/anchor_push.py`, but nothing signs the anchors, `m2k run` never publishes them, and the `ledger-anchors` branch
was only created on 2026-10-17 (an empty orphan branch, commit `67c4e0e`).

After this card, every trusted run ends with an **OBS-signed checkpoint** of the ledger head and the OBS chain, appended
to the local anchor file and pushed to the `ledger-anchors` branch on GitHub. A checker compares the published anchors
with the local ledger and OBS log, and asks GitHub whether that branch was ever force-pushed or deleted.

Threat model, stated plainly: during a local demo the agent runs as the owner's Windows user and can read the owner's
git credentials. It can push to, force-push or delete `ledger-anchors`. The design does not try to stop that. It makes
it **visible**: GitHub's repository activity log records `force_push` and `branch_deletion` per branch, and a token
cannot delete those entries. Deleting the whole repository makes every anchor disappear, which the checker also
reports.

Owner decisions (2026-10-17):
- External anchors live on the GitHub branch `ledger-anchors`, with the force-push check. (Not a GitHub Actions notary:
  a token with repository write access can exfiltrate workflow secrets, so it adds no strength against this threat.
  A public transparency log is left for stage 8.)
- Failure policy by trust domain: **PRODUCTION** must publish the checkpoint before anything is delivered and must
  pass the remote check before a run starts; otherwise the run is refused or ends UNKNOWN with nothing delivered.
  **TEST_ONLY** records the failure, prints it in red and carries on. (Same idea as TEST_ONLY allowing `code = "dev"`.)

## Required behaviour
1. **Checkpoint.** At the end of every top-level run with OBS, after `gate.adjudication` and before any delivery, the
   kernel records an `anchor.checkpoint` event through the witnessed path. Its OBS receipt names the checkpoint event's
   own `ledger_seq`/`ledger_hash` (so the whole ledger chain up to it) and carries `obs_seq`/`obs_prev` (so the whole
   OBS chain before it). That receipt **is** the signature; do not add a new key or a new receipt type.
2. **Anchor line v2** (`anchor.py`): `{v: 2, ts, ledger_id, seq, hash, note, receipt, prev}`, where `seq`/`hash` are the
   checkpoint event's and `receipt` is its OBS receipt envelope. `check(path, ledger, obs_public_key=None,
   obs_log=None)` keeps every T09 check and, for v2 lines, with a key: the receipt verifies and names exactly this line's
   `ledger_id`/`seq`/`hash`; with the OBS log too: the log line at `obs_seq` holds the identical receipt and the line
   before it hashes to `obs_prev`. v1 lines stay accepted as legacy (unsigned) lines.
3. **Publishing from `m2k run`.** Config gets an `[anchor]` table: `file` (local anchor file) and `repo_dir` (a dedicated
   clone on branch `ledger-anchors`), paths relative to the config, both outside every workspace (refused otherwise).
   The top-level `anchor_file` key keeps working when there is no `[anchor]` table. After the checkpoint, `m2k run`
   appends the v2 line and, when `repo_dir` is set, publishes it with the `tools/anchor_push.py` logic (move what is
   needed into `anchor.py`; the tool stays a thin CLI over it): only an append-only extension of the published file,
   one commit, push. Every outcome is a ledger event: `anchor.published` `{commit, seq}` or `anchor.failed` `{reason}`.
   - **PRODUCTION:** `[anchor]` with `repo_dir` is required at startup (refuse like T44's `OBS_REQUIRED`, code
     `ANCHOR_REQUIRED`). A failed publish means no delivery: `delivery.refused` with `ANCHOR_UNAVAILABLE: …`, and the
     run reports UNKNOWN (T44 rule: the run reports PASS only when the Gate released).
   - **TEST_ONLY:** `[anchor]` is optional. A failed publish prints `警告：外部锚点未发布：…` and the run goes on as
     usual.
4. **Remote check** (`anchor.py` + `m2k check-ledger --remote`): fetch `ledger-anchors` in `repo_dir`; the published
   `anchors/<ledger_id[:12]>.jsonl` must be a byte prefix of the local anchor file, and every published line passes
   `check` with the configured OBS key and log. **The OBS log is the spine:** across *all* published anchor files, take
   the v2 line with the highest `obs_seq` whose receipt is signed by the configured OBS key; the local OBS log must hold
   that identical receipt at that `obs_seq`. Otherwise report `OBS log replaced or truncated` — this catches deleting
   the ledger and the OBS log together and starting fresh, which a per-ledger check cannot see. Then ask GitHub's repository activity API
   (`GET /repos/{owner}/{repo}/activity?ref=ledger-anchors&activity_type=…`) for `force_push` and `branch_deletion`;
   any entry is a problem naming the actor and time. Owner/repo come from `repo_dir`'s `origin` URL. The token comes
   from `GITHUB_TOKEN` if set, otherwise from `git credential fill` for `github.com`; it is never printed or recorded.
   Standard library only (`urllib`). An unreachable API is a problem, not a pass.
5. **Startup.** When `repo_dir` is set, `m2k run` runs the remote check right after T44's OBS check, before the sandbox,
   the models and the probe. PRODUCTION: any problem refuses the start (`startup.refused`, code `ANCHOR_CHECK`, exit 2).
   TEST_ONLY: print the problems as a warning, record `anchor.warning` `{task, problems}` under `m2k.startup`, and run.
6. **CI.** `m2k run` in the workflow must not hold a GitHub token. `trust/test/m2k-task.toml` gets `[anchor] file =
   "anchors.jsonl"` without `repo_dir`. After the run, the summary step clones `ledger-anchors` into the runner
   (outside the workspace) and publishes the anchor file with `tools/anchor_push.py --push`, using the workflow token
   (the workflow already has `contents: write`; same push form as the existing branch push). Then it runs
   `m2k check-ledger --remote` with that clone (pass `GITHUB_TOKEN` to that step only) and writes the result to the job
   summary. Remove the old unsigned `m2k anchor --out …` line. Re-sign `trust/test/release.json` exactly as in T44
   (same Root, same rules: never commit, copy or print the private key; stop and ask if not found).
7. **Witness start-up cost** (found while doing T44): `obs.serve` verifies every receipt signature of the whole log
   before it answers, about 5.8 s at 50,000 lines. Every receipt signs `obs_prev`, the hash of the previous raw line, so
   the whole hash chain plus the signature of the **last** line (with its `obs_seq`/`obs_prev` equal to that line's
   `seq`/`prev`) already exposes any keyless edit anywhere in the log. Start-up uses that check. `m2k obs check
   --public-key` keeps verifying every line. The reviewer measures start-up at 50,000 lines (target ≤ 0.5 s).
8. **Docs.** `m2k.example.toml`: the `[anchor]` table with one comment line on the domain rule. `README.md`: replace the
   one-time "create the branch" paragraph with the clone command (`git clone -b ledger-anchors …`) and one line on what
   `check-ledger --remote` proves.

## Acceptance
`python -m unittest tests.test_anchor tests.test_obs tests.test_release tests.test_trust_required tests.test_fingerprint tests.test_trust_cli -v`
and the full suite. Use temporary bare git repositories as the remote (skip if `git` is missing) and patch the
activity-API function; never call GitHub in tests. New tests must show:
- A TEST_ONLY run with `[anchor]` publishes one v2 line; `check-ledger --remote` is clean.
- **Rewrite both:** after the run, rebuild the ledger and the OBS log from scratch so they are self-consistent and pass
  `verify_chain` and `check_log` with the key (re-witness with the same OBS key). `check-ledger --remote` reports the
  published checkpoint as not matching. **Delete both** and start over with a fresh ledger and a fresh OBS log under the
  same OBS key, then run one new task: `check-ledger --remote` reports `OBS log replaced or truncated`.
- **Force push:** the patched activity API returns a `force_push` (and, separately, a `branch_deletion`) on
  `ledger-anchors` → reported. API unreachable → reported.
- A published file that is not a prefix of the local file (local anchors rewritten) → reported.
- **PRODUCTION** (temporary PRODUCTION Root and charter, pinned code, as in `tests/test_release.py`): no `repo_dir` →
  exit 2 `ANCHOR_REQUIRED`; publish fails (remote unreachable) → no delivery, `ANCHOR_UNAVAILABLE`, exit 2; startup with
  a remote-check problem → exit 2 `ANCHOR_CHECK`, no `task.submitted`.
- **TEST_ONLY** with the same failures: warning, `anchor.failed` / `anchor.warning` recorded, delivery as usual.
- A v2 line whose receipt was signed by another key, or whose `obs_seq` line in the OBS log differs → reported.

In the handoff note: run one real **read-only** call of the activity API for `742037477/m2-kernel`, ref
`ledger-anchors`, with the owner's git credential on this computer, and say what it returned (count and types only).
That shows the token path works and gives the owner a first look at retention.

## After the merge (reviewer, not this card)
- Claude dispatches Codex: dedicated clone `<anchors-repo>` on `ledger-anchors`; `[anchor]` with `repo_dir` in the
  three demo configs; manifests re-signed with the dev Root as after T44; `m2k check-ledger --remote` clean for each.
- A `workflow_dispatch` smoke run of `m2k-task` shows the anchor published and `check-ledger --remote` clean.

## Out of scope
- Release-manifest versions, rollback detection at startup, manifest revocation, the adjudicator checking the
  manifest (T64).
- Branch protection (needs GitHub Pro), a GitHub Actions notary, a public transparency log.
- Faster `verify_chain` or `anchor.check` on very large ledgers.

## Review notes (written by reviewer)

- 2026-10-17 Claude 批准预算 150 → 230：澄清新增全量主线核对与 --this-ledger-only。
- 实现前澄清 2026-10-17（用户，以此为准）：主线核对覆盖所有已发布 v2 行中由配置 OBS 公钥签发的回执；每张回执必须与本地 OBS 日志同一 obs_seq 的回执完全相同。缺失、不同或同一 obs_seq 有不同回执均报 `OBS log replaced or truncated`，注明 obs_seq 和已发布文件；不加载历史账本，本地 OBS 日志只读一次、解析一次。
- 实现前澄清 2026-10-17（用户）：`check-ledger --remote` 默认做全量主线核对；新增 `--this-ledger-only`，只核对本账本已发布文件的前缀、签名、OBS 位置和分支强推/删除记录。CI 汇总用此选项，并用一行注释说明：CI 环境一次性、OBS 私钥仅 root 可读且 Agent 不可达；全量主线核对用于长期本机及正式环境。`m2k run` 启动核对始终全量，不受此选项影响。
- 实现前澄清 2026-10-17（用户）：补测同一 OBS 密钥在两个全新日志各运行并发布，全量核对报错而 `--this-ledger-only` 通过；旧检查点 obs_seq=10，删除重建日志并增长至 11 以上、发布新检查点后，仍必须报出旧检查点不匹配。

- **Round 1 (383b90c): CHANGES REQUESTED (Claude).** `remote_check` read only the branch tip, so an ordinary (non-force)
  push deleting or shortening an old anchors file hid the "delete ledger + OBS log and restart" attack; a kernel FAIL was
  reported UNKNOWN on `ANCHOR_UNAVAILABLE`.
- **PASS (Claude, 2026-10-17), merged as #46** (head 29a47b4). ci / card-scope green; GLM-5.3 GO twice. Linux, Python 3.13:
  full suite 576 OK (7 skipped). `m2k/` +229 / 230. History is checked append-only in one `git log --numstat`; FAIL stays
  FAIL. `trust/test/release.json` verified. Witness start-up at 50,000 lines 6.3 s → 0.47 s.
  Later (GLM, non-blocking): malformed `[anchor]` gives a traceback; the CI summary step passes the token to every `m2k()`
  call; `git()` drops git's stderr; the adjudicator does not yet know `anchor.checkpoint`.

## Handoff note (written by implementer, ≤ 10 lines)

- 基于 main `7769589`，分支 `task/T63-external-anchor`；实现 Gate 后、交付前 OBS 签名检查点及追加发布，PRODUCTION 启动拒绝/发布失败 UNKNOWN，TEST_ONLY 红色警告并记账。
- 主线核对全部同钥已发布 v2 回执（含序号冲突）；OBS 日志一次读取/解析。CI 用 `--this-ledger-only`，运行启动始终全量核对；覆盖旧 obs_seq=10、重建增长至 13 后仍报错。
- 本机 `<python>`（3.13.11）：Acceptance 148 项通过；全套 569 项 OK（9 skipped）；测试仅临时裸仓库、活动 API 打桩。
- `m2k/` 新增 220/230 行，只改卡允许文件及本卡；预算批准和实现前澄清已记入 Review notes。
- 同一份 50,000 行有效 OBS 日志、同钥/同输入启动至首回执：main `7769589` 中位 6.2976 s；候选三次 0.4547/0.4661/0.4738 s，中位 0.4661 s（≤0.5 s）；进程内启动中位 0.3385 s。
- `trust/test/release.json` 用原 TEST_ONLY Root `6357ec2addf1fd69` 重签、code=dev，`verify_release` 对 LF/CRLF 配置均 PASS；未复制或输出 Root 私钥。
- 所要求的活动 API 只读真调用 1 次：返回 1 条；类型及数量为 `branch_creation: 1`。
- CI 汇总脚本语法通过，远端工作流未触发；演示配置未动、未推送真实 `ledger-anchors`、未开 PR；CI/GLM 和审阅者裁定待执行。
- 返工 1（对照 `383b90c`）：先复现普通推送删除/截短漏报；新增单次历史扫描，报告删除/重命名的提交号，两种核对范围均检查已发布文件缺失。
- `ANCHOR_UNAVAILABLE` 只将内核 PASS 降为 UNKNOWN，FAIL 保持 FAIL、退出 1；示例 `repo_dir` 已注释，其他非阻断意见未处理。
- Python 3.13：定向 155 项通过，全套 576 项 OK（9 skipped）；测试仅临时裸仓库及 API 桩，新增删除/截短/重命名/空文件/缺失与 FAIL 回归；`m2k/` 新增 229/230 行，待 CI/审阅者裁定。
