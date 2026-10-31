# T64: Release manifests get a slot and a version; OBS refuses rollbacks; manifests can be revoked; the adjudicator checks the manifest

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T64-release-rollback` (start from main **after T63 is merged**)
- **Line budget:** ≤ 220 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/trust.py`, `m2k/obs.py`, `m2k/cli.py`, `m2k/kernel.py`, `m2k/adjudicate.py`, `m2k.example.toml`, `README.md`, `trust/test/release.json`, `tests/test_release.py`, `tests/test_obs.py`, `tests/test_adjudicate.py`, `tests/test_trust_cli.py`, `tests/test_trust_required.py`, `tests/test_fingerprint.py`, `tests/test_anchor.py`, `tasks/README.md`
- **Status:** DONE (#47)

## Goal
Stage 5, the last of the three cards split from the old T44 (see `tasks/T44-obs-required.md`). It closes what T43
carried forward:
- **Rollback.** A T43 manifest proves "the Root signed this config", not "this is the newest config". Putting an older
  config back together with its own older manifest still starts.
- **Revocation.** There is no way to withdraw a manifest.
- **The adjudicator** never looks at the manifest a run started under.

Revoking alone cannot fix rollback, because the revocation list can itself be rolled back. The fix is a monotonic
counter that something outside the agent's reach remembers: the OBS witness already refuses a lower `ledger_seq` per
ledger, and after this card it also refuses a lower manifest version per slot and a lower revocation-list version per
Root. Deleting the OBS log to make it forget is what T63's external anchors expose.

Owner decisions (2026-10-17):
- **One version line per config ("slot")**, not one per Root: changing one config re-signs only that config's
  manifest. `sign-release` proposes the current version + 1.
- **A detected rollback refuses the start in both trust domains.** Normal work never needs a lower version or a
  revoked manifest: to go back to an older config, the owner re-signs its content under a **higher** version. (The
  TEST_ONLY leniency of T63 is only for availability problems such as an unreachable remote.)
- Defaults stated by the reviewer and accepted: PRODUCTION requires a revocation list; a manifest revoked **after** a
  run makes a later `m2k adjudicate` of that run FAIL ("results from this config are no longer trusted").
- Accepted residual risk: replacing one config file with *another* slot's current config plus its manifest still
  starts. Both are owner-approved configs; binding a manifest to a file path would break moving a release to another
  computer.

## Required behaviour
1. **Manifest v2** (`trust.py`). Body `{schema: "m2k.release/2", trust_domain, manifest_id, slot, version, code, config,
   issued_at}`. `slot` matches `[a-z0-9][a-z0-9_-]{0,31}`; `version` is an int ≥ 1. `verify_release` accepts only
   `m2k.release/2` (all manifests are re-signed anyway; no downgrade path to `/1`).
2. **`sign-release --slot S [--version N]`.** `--slot` is required. Without `--version`, read the manifest currently
   named by the config's `[trust] release`: if it verifies under this Root and has the same slot, use its version + 1,
   otherwise refuse and ask for `--version`. Print `slot S version N`. Never overwrite `--out` (T43 rule).
3. **Revoking manifests.** The Root-signed revocation list gains `revoked_releases` (manifest ids; a missing key means
   `[]`, so existing lists stay valid). `sign-revocations` gets `--release ID …` in the same style as its existing `--charter` / `--credential` options. A revoked manifest
   fails `verify_release` with the new code `RELEASE_REVOKED`. PRODUCTION requires `[trust] revocations` (startup
   refusal `REVOCATIONS_REQUIRED`, same pattern as T44/T63).
4. **OBS remembers versions** (`obs.py`). A witness request of kind `release.checked` carries one more field,
   `release: {root, domain, slot, version, manifest_id, revocations_version}` (`revocations_version` is an int or
   null). The receipt body repeats it, `check_log` accepts and verifies it, and a restarted witness rebuilds its memory
   from the log. The witness refuses, logging nothing, when for the same `root`+`domain`:
   - `version` is lower than the highest version it has witnessed for that `slot` → `release rollback`;
   - the same `slot`+`version` arrives with a different `manifest_id` → `two manifests share slot S version N`;
   - `revocations_version` is lower than the highest it has witnessed → `revocations rollback`.

   Equal values are accepted (every run under the same manifest sends the same request). All other request kinds stay
   exactly as T42 defined them.
5. **Startup** (`cmd_run`). Start the OBS witness right after T44's OBS check (earlier than today) and, after T63's
   remote check and before the sandbox, the models and the probe, record `release.checked` under `m2k.startup` through
   the witness. A refusal from the witness is `startup.refused` with code `RELEASE_ROLLBACK` and the witness's reason,
   plus one line on the fix (`sign-release` with a higher `--version`); exit 2 in **both** domains. A witness that is
   unavailable at this point is `startup.refused` `OBS_UNAVAILABLE`, exit 2. The refusal reason from the witness must
   reach the user and the ledger verbatim; do not turn it into "witness unavailable".
6. **The ledger records the manifest.** The top-level `task.submitted` records `release: {envelope, config_sha256,
   checked_seq}`: the whole manifest envelope, the CRLF-normalized config digest that was checked (T43), and the seq of
   this run's `release.checked` event.
7. **The adjudicator checks the manifest.** `adjudicate(..., root_pin=None, revocations=None)`; the Gate passes the
   kernel's trust setup, `m2k adjudicate` loads them from the config. With a pin, a top-level task is FAIL unless:
   the envelope verifies under the pin and domain; `body.config == config_sha256`; `body.code` is `"dev"` (TEST_ONLY
   only) or equals the recorded framework code digest; the manifest id is not in the current `revoked_releases`; and
   the event at `checked_seq` is a `release.checked` for the same `manifest_id` with a valid OBS receipt (when an OBS
   key is given). Each failure names itself. Without a pin, behaviour is unchanged.
8. **CI and docs.** Re-sign `trust/test/release.json` as `--slot ci --version 1` with the `trust/test` Root (same rules
   as T44: the key is `<old-project-private>\root-test-only.pem`, fingerprint `6357ec2addf1fd69`; never commit,
   copy or print it). `m2k.example.toml` and `README.md`: the `sign-release --slot` command and one line on "to go
   back, re-sign the old config with a higher version".

## Acceptance
`python -m unittest tests.test_release tests.test_obs tests.test_adjudicate tests.test_trust_cli tests.test_trust_required tests.test_fingerprint tests.test_anchor -v`
and the full suite. Real witness processes, temporary Roots, `ScriptedModel`. New tests must show:
- **Rollback refused, both domains:** run under slot `a` v2, then put back the v1 config and v1 manifest → exit 2
  `RELEASE_ROLLBACK`, `release rollback` in the message and in `startup.refused`, no `task.submitted`, no model call,
  no probe. The same after restarting the witness (memory rebuilt from the log).
- **Going back the right way works:** the v1 config content re-signed as v3 → runs.
- **Slots are independent:** slot `b` v1 runs after slot `a` v2.
- **Same slot+version, different manifest** → refused.
- **Revocations:** a revoked manifest → `RELEASE_REVOKED`; the revocation list replaced by an older version → refused
  (`revocations rollback`); PRODUCTION without `[trust] revocations` → `REVOCATIONS_REQUIRED`.
- **`sign-release`** proposes version + 1, refuses without `--slot`, refuses a missing or foreign current manifest
  without `--version`, refuses to overwrite.
- **Adjudicator:** PASS on a normal run; FAIL with a named reason when the recorded envelope is swapped for another
  validly signed manifest, when the config digest differs, when the manifest is revoked after the run, and when
  `checked_seq` points at a missing or unwitnessed event.
- The T42/T45/T44 OBS tests still pass unchanged: ordinary witness requests are unaffected.

## After the merge (reviewer, not this card)
- Claude dispatches Codex to re-sign the three demo manifests as slots `dsh`, `claude`, `codex`, version 1 each, with
  the dev Root (old files kept as `*.pre-T64.bak`); `m2k trust status` shows `release manifest: ok` for all three.
- A `workflow_dispatch` smoke run of `m2k-task` shows `release.checked` witnessed and the Gate PASS.
- Then the stage 5 attack drill (T46, DeepSeek via m2k) can be written.

## Out of scope
- Binding a manifest to a config file path or a machine.
- Witnessing anything else at startup; a cloud notary.

## Review notes (written by reviewer)

- 2026-10-17 Claude 批准预算 140 → 220：澄清新增 Kernel release 参数与 m2k adjudicate 的缺失记录处理。
- 实现前澄清 2026-10-17（用户，以此为准）：`sign-release` 自动递增只核对当前清单的 Root 签名、信任域、`m2k.release/2` 格式和相同 slot，不比较 code/config；旧 `/1` 清单拒绝并要求显式 `--version`。
- 实现前澄清 2026-10-17（用户）：`revocations_version=null` 不参与比较且不清除已见最高数字；只有数字与同一 Root + 信任域的最高数字比较。补测同 Root 下有吊销单 v3 与无吊销单的 TEST_ONLY 配置交替启动均可运行；PRODUCTION 仍要求吊销单。
- 实现前澄清 2026-10-17（用户）：新增 `Kernel(..., release=None)`，CLI 传入清单信封、规范化配置摘要和 `release.checked` seq；仅非 None 时记录 `task.submitted.release` 并由 Gate 传 root_pin / revocations。底层直调行为不变，卡外测试不改。`m2k adjudicate` 配置有 `[trust]` 时始终传 pin；缺少 release 记录判 FAIL，原因 `no release manifest recorded (run predates T64 or bypassed the CLI)`。补测 CLI 正常运行 Gate PASS 及卡内篡改 FAIL。

- **PASS (Claude, 2026-10-17), merged as #47** (head db18dcd). ci / card-scope green; GLM-5.3 GO. Linux, Python 3.13: full
  suite 597 OK (7 skipped). `m2k/` +199 / 220. `trust/test/release.json` verified (`m2k.release/2`, slot `ci` v1, Root
  6357ec2addf1fd69). Later (non-blocking): the adjudicator reads all `m2k.startup` events to find `release.checked`
  (grows with runs); `cmd_adjudicate` uses `trust._read_json`; a malformed `release.checked` error is labelled
  `RELEASE_ROLLBACK`.

## Handoff note (written by implementer, ≤ 10 lines)

- 基于 main `f074bd2`，分支 `task/T64-release-rollback`；实现 v2 清单、按 slot 版本/吊销单版本防回滚、清单吊销及启动旁证，`m2k/` 新增 199/220 行。
- CLI 传入 `Kernel.release`，Gate/独立裁决核验信封、配置/代码摘要、当前吊销单及本次 `release.checked` 回执；底层直接构造 Kernel 保持兼容，卡外测试未改。
- `<python>`（3.13.11）：基线定向 163 项通过；候选 Acceptance 184 项通过，全套 597 项 OK（9 skipped）。
- 新增真实旁证进程/临时 Root/ScriptedModel 用例：双域回滚及重启、v3 恢复、独立 slot/冲突、吊销及数字/null 交替、具名裁决失败；合并子任务仍每次 Gate 只读 OBS 日志一次。
- `trust/test/release.json` 已用原 Root `6357ec2addf1fd69` 重签：`m2k.release/2`、TEST_ONLY、slot=ci、version=1、code=dev；LF/CRLF 配置经 `verify_release` 均 PASS，私钥未复制或输出。
- 清单 config SHA-256：`b6acada979def01f811d8ba886ed4e21fe100f53d337907d9eec26cc03827208`；测试仅临时裸仓库及 API 桩，不访问 GitHub、不调用真实模型。
- 三个指定演示/密钥/锚点目录未动，未推送真实 `ledger-anchors`，未开 PR；远端 CI/GLM 及审阅者裁定待执行。
