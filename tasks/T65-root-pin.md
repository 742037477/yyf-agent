# T65: The OBS witness remembers each trust domain's first Root; a changed Root refuses the start; the remote check reports a notary change; the Root fingerprint is shown on every run

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T65-root-pin` (start from the latest main)
- **Line budget:** ≤ 150 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/obs.py`, `m2k/cli.py`, `m2k/anchor.py`, `m2k/accept.py`, `m2k.example.toml`, `README.md`, `tests/test_obs.py`, `tests/test_release.py`, `tests/test_anchor.py`, `tests/test_trust_cli.py`, `tests/test_trust_required.py`, `tests/test_adjudicate.py`, `tests/test_fingerprint.py`, `tests/test_delivery.py`, `tests/test_stage5_attack.py`, `tasks/T46_check.py`, `tasks/README.md`
- **Status:** DONE (#50)

## Goal
T46 found **a21**: in local same-user mode the agent can build its own Root and re-sign the pin, charter, issuer key,
revocation list, manifest and config, keep the same OBS key and log, and the run starts, runs and adjudicates PASS.
Today the witness keeps its rollback memory per `root + domain` (T64), so a new Root simply starts a fresh version
line: nothing remembers which Root is the owner's.

After this card the witness remembers, per trust domain, the **first Root it ever witnessed**, and refuses any other
Root in that domain; `m2k run` turns that into `ROOT_CHANGED` and refuses the start in **both** domains. The remote
anchor check reports when this ledger's published anchors were signed by a different notary than the current OBS.
Every `m2k run` and `m2k trust status` shows the Root fingerprint prominently, so the owner can see whose Root is in
charge.

Owner decisions (docs/ARCHITECTURE.md, stage 5, 2026-10-17):
- The full replacement (Root **and** OBS **and** anchor repository) is not solved here; only isolation solves it
  (local separate-user isolation comes next, an independent OBS later). This card closes "replace only the Root".
- **Accepted residual risk (TEST_ONLY):** deleting or replacing the OBS log makes the witness forget the Root. T63's
  remote check already reports that (`OBS log replaced or truncated`); TEST_ONLY keeps warning and starting,
  PRODUCTION refuses. Do not change that policy.
- The legitimate way to change a Root is a **new trust epoch**: a new OBS key and log **and** a new ledger (see 6).
- Add an offline acceptance scenario for the owner (see 7).

## Required behaviour
1. **Root memory in the witness** (`obs.py`). For requests of kind `release.checked`, the witness keeps
   `roots[domain] = root` (the `release.root` fingerprint). The first `release.checked` for a domain sets it. A later
   `release.checked` whose `root` differs for the same `domain` is refused, logging nothing, with the reason
   `root changed: <DOMAIN> first seen <old>, now <new>` (exact prefix `root changed`). This check comes **before** the
   T64 version and revocation checks. A different domain has its own entry (TEST_ONLY and PRODUCTION never conflict).
   A restarted witness rebuilds `roots` from its log: the first Root per domain in log order wins; a later row with a
   different Root (possible only in logs written before this card) is ignored during the rebuild, not an error. All
   other request kinds and every T42/T45/T64 behaviour stay exactly as they are.
2. **A helper to read the memory** (`obs.py`): a function returning `{domain: root}` from an OBS log, verified with the
   OBS public key (reuse `check_log` / `log_snapshot`; do not write a second log parser). `trust status` uses it.
3. **Startup** (`cmd_run`). A witness refusal whose reason starts with `root changed` is `startup.refused` with code
   **`ROOT_CHANGED`** and the witness's reason verbatim, exit 2, in both domains; print both fingerprints and one line
   pointing to the README section "换 Root" (the legitimate procedure). Other refusals keep `RELEASE_ROLLBACK`
   unchanged. After `release.checked` is witnessed, print one prominent line (bold, stderr) before the run starts:
   `Root 指纹：<fp>（<DOMAIN>，OBS 已确认与首次见到的 Root 一致）`. As today, a refused start writes no
   `task.submitted`, calls no model and does not probe the worker.
4. **`m2k trust status`.** Print `Root 指纹：<fp>（<DOMAIN>）` as the **first** line (bold), right after the trust setup
   loads. After `obs: ok`, print one of `obs root: ok`, `obs root: not yet recorded` (no `release.checked` for this
   domain in the log yet, or no log), or `obs root: changed (OBS remembers <old>, config uses <new>)`; `changed` makes
   the command exit 1. Keep the existing JSON block (it already contains the Root fingerprint).
5. **Remote check reports a notary change** (`anchor.remote_check`). Today records signed by another notary are
   skipped silently. For **this ledger's own anchor file** only (`anchors/<ledger_id[:12]>.jsonl`), read the `notary`
   of every v2 record's receipt (the body, without verifying the signature, since the other key is unknown); for each
   distinct notary that differs from the current OBS fingerprint add one problem:
   `notary changed: <file> has anchors signed by <fp>, current OBS is <fp>`. Other ledgers' files on the branch are
   **not** compared (CI and local runs share `ledger-anchors` with different notaries; that is expected). The
   existing TEST_ONLY-warns / PRODUCTION-refuses handling applies unchanged.
6. **Docs** (`README.md`, `m2k.example.toml`). README section "换 Root（合法做法）": a Root change is a new trust
   epoch — (1) generate a new OBS key with `m2k obs keygen --out <new dir>` and use a new OBS log; (2) point the
   config at a **new ledger file**; archive the old ledger and old OBS log by renaming, never delete them; (3) sign the
   charter, revocation list and manifests with the new Root (`sign-release --slot <slot> --version 1`); (4) the new
   ledger gets its own anchor file automatically; old anchor files stay on the branch. Say why: the old OBS log
   remembers the old Root (`ROOT_CHANGED`), and the old ledger's anchors carry the old notary (`notary changed`).
   One comment line in `m2k.example.toml` pointing to that section.
7. **Offline acceptance scenario** (`accept.py`): `整套换 Root（沿用同一 OBS）`, expected text
   `旁证拒绝换 Root（root changed）`, placed after `s_restart_ledger` and before `s_bytecode` in the offline list. Same
   style as `s_config_rollback`: one witness, `release.checked` under Root A (accepted), then the same domain and slot
   under Root B (refused); the scenario passes only when the answers are exactly accepted then refused with a reason
   starting `root changed`.
8. **T46 attack a21 must now pass** (`tests/test_stage5_attack.py`, `tasks/T46_check.py`). Remove
   `@unittest.expectedFailure` from `test_a21_whole_root_replaced` and change it to assert the refusal: the second
   `m2k run` exits 2 with `ROOT_CHANGED`, a `startup.refused` event with that code, and no `task.submitted` for the
   swapped task. In `tasks/T46_check.py`: `MAY_BE_WEAK = set()`, and add the defence
   `"Root memory off": (3, ["a21"], [("m2k/obs.py", <your exact refusal line>, <replacement>)])` — one source edit
   that applies exactly once and switches the Root check off. Run `python tasks/T46_check.py` and make sure that
   defence makes a21 fail.

## Acceptance
```
python -m unittest tests.test_obs tests.test_release tests.test_anchor tests.test_trust_cli tests.test_stage5_attack -v
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
Real witness processes, temporary Roots, `ScriptedModel`; patch `m2k.anchor.activity` for anything that would ask
GitHub. New tests must show:
- **Witness:** a second Root in the same domain is refused (`root changed`), nothing appended to the log; still
  refused after restarting the witness; another domain with another Root is accepted; a log written with two Roots in
  one domain (build it with the pre-T65 code path or by appending properly chained, signed rows) rebuilds with the
  first Root and the witness starts.
- **CLI, both domains:** a run under Root A, then the whole Root replaced (a21 style, same OBS) → exit 2,
  `ROOT_CHANGED` in stderr and in `startup.refused`, both fingerprints shown, no `task.submitted`, no model call, no
  probe.
- **The legitimate procedure works:** after the same replacement, a new OBS key and log plus a new ledger → the run
  starts and passes, and the `Root 指纹` line names the new Root.
- **Display:** a normal run prints the `Root 指纹` line; `trust status` prints it first and reports `obs root: ok`,
  `not yet recorded` and `changed` (exit 1) in the three situations.
- **Remote check:** this ledger's own anchor file holding records from another notary → one `notary changed` problem;
  another ledger's file signed by a different notary → no new problem; the existing T63 tests pass unchanged.
- **Acceptance:** `m2k accept --offline` shows the new scenario ✅ with the exact expected text, and every other
  scenario still passes.

Existing tests that legitimately run two different Roots through **one** OBS log may now fail. Fix only by giving each
Root its own OBS log (the legitimate procedure), in the files listed above, and name each such test in the handoff
note. Do not weaken the new check to make an old test pass.

## After the merge (reviewer, not this card)
- No config changes, so no manifests are re-signed (three demo configs and `trust/test/m2k-task.toml` unchanged).
- Claude dispatches Codex (read-only is not enough: it runs `m2k`) to run `m2k --config <cfg> trust status` for the
  three demo configs (`dsh`, `claude`, `codex`): each must print `Root 指纹：fa61f0d84958eaa2` and `obs root: ok` or
  `not yet recorded` (the shared OBS log should remember only the dev Root). If it reports `changed`, stop and ask the
  owner before touching anything.
- A `workflow_dispatch` smoke run of `m2k-task`: Gate PASS and the `Root 指纹` line (6357ec2addf1fd69) in `run.txt`.
- Update the T46 card's Review notes: a21 closed by T65 (no longer an expected failure).

## Out of scope
- The full replacement together with the OBS and the anchor repository (local separate-user isolation, next).
- A Root-to-Root transfer statement signed by the old Root; a cloud notary; comparing notaries across other ledgers'
  anchor files; any change to how TEST_ONLY treats remote-check problems.
- The non-blocking items listed in the handoff of T46/T47 (e.g. the charter `SystemExit` path).

## Review notes (written by reviewer)

- **PASS (Claude, 2026-10-17, after GLM GO), merged as #50** (head 554dca9). ci / card-scope green. Independent review by a
  fresh Codex session (temporary worktree): five modules 185/185, offline acceptance 28/28, full suite 657 (646 OK, 11
  platform skips), `tasks/T46_check.py` OK with "Root memory off" making a21 fail. `m2k/` +72 / 150. Claude spot-checked
  `m2k/obs.py`, `m2k/cli.py`, `m2k/anchor.py`. GLM non-blocking (later small card): the own-file notary read is
  unguarded (a malformed record ends as a raw KeyError problem); `trust status` raises a traceback when `check_log`
  fails after `obs: ok`; `this_ledger_only` now parses other ledgers' records.
- Owner decision after this card was written (2026-10-17): the independently isolated OBS is dropped from the roadmap,
  so "an independent OBS later" above no longer applies; local separate-user isolation is the remaining step.

## Handoff note (written by implementer, ≤ 10 lines)

- 基准为 main e70677d：五模块 176 例，a21 为 expectedFailure；候选同条件五模块 185 例全部通过。
- OBS 按域固定首个 Root，先于版本检查拒绝变更；重启兼容旧多 Root 日志，root_memory 复用 check_log 验签。
- 两域启动均返回 ROOT_CHANGED、保留原始原因且无模型/探测/任务提交；指纹显示、status 三态及合法新纪元流程已验证。
- 远端核对仅比较本账本锚点的不同旁证并去重；README 说明新 OBS、新账本、本地锚点文件及归档流程。
- Python 3.13.11：卡内五模块命令、accept --offline（28/28）、tasks/T46_check.py（含 Root memory off 使 a21 失败）均通过。
- 全套 unittest discover：657 例，0 失败，11 项平台跳过；m2k/ 新增 72 行（预算 150）。
- 旧用例隔离：tests/test_obs.py::ReleaseWitnessTest.test_versions_slots_roots_domains_conflicts_and_null_on_one_live_witness 的第二 Root 改用独立 OBS 密钥及日志。
- 新测试使用临时 Root、真实旁证进程、ScriptedModel 和本地 git；未访问 GitHub/真实模型，未改演示配置、受保护目录或既有清单；CI 与 reviewer 验收待进行，未开 PR。
