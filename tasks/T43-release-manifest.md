# T43: Startup self-check — code and config must match a Root-signed release manifest

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T43-release-manifest`
- **Line budget:** ≤ 100 added lines in `m2k/`
- **Files you may change:** `m2k/trust.py`, `m2k/cli.py`, `m2k/fingerprint.py`, `tests/test_release.py`, `tests/test_trust_required.py`, `tests/test_fingerprint.py`, `tests/test_trust_cli.py`, `.github/workflows/m2k-task.yml`, `trust/test/m2k-task.toml`, `trust/test/release.json`, `m2k.example.toml`, `README.md`, `tasks/README.md`
- **Status:** DONE (#44)

## Goal
Today the Root-signed charter pins the framework code (PRODUCTION only; TEST_ONLY writes `"dev"`), but nothing pins the
config file at startup. T40 only notices a config change *during* a task. Between two tasks, an agent can rewrite
`m2k.toml` (swap `[models.reviewer]`, replace the `[obs]` public key, switch `[sandbox] backend` to `"none"`), and the
next `m2k run` starts normally under the new config.

After this card, `m2k run` with `[trust]` checks, before anything else runs, that the code and the exact config file
match a **release manifest signed by Root**. Changing the config means the owner signs a new manifest with Root. That
signature is the owner's confirmation (owner decision 2026-10-17). Offline USB signing for PRODUCTION; one command with
the test Root for TEST_ONLY.

Owner decisions (2026-10-17):
- The owner's confirmation is a Root signature on the manifest. There is no separate owner key.
- TEST_ONLY may write `code = "dev"` (code changes with every commit during development). **The config is always
  checked**, in both domains. PRODUCTION must pin both code and config.

## Required behaviour
1. **Manifest format** (`trust.py`): a Root-signed envelope (same `{schema, body, signature_b64}` format as the
   charter) with body `{schema: "m2k.release/1", trust_domain, manifest_id, code, config, issued_at}`.
   - `code` is `release_digest(code_dir)`, or `"dev"` (TEST_ONLY only).
   - `config` is the SHA-256 hex of the config file bytes with every `\r\n` replaced by `\n`. This keeps a Windows
     checkout with CRLF and a Linux runner with LF on the same digest.
   - `verify_release(pin, domain, release_env, code_dir, config_bytes)` raises `TrustError` with distinct codes:
     `RELEASE_SIGNATURE` (bad signature, wrong schema, or wrong domain), `RELEASE_CODE` (code digest differs, or
     `"dev"` used under PRODUCTION), and `RELEASE_CONFIG` (config digest differs).
2. **Signing command**: `m2k trust sign-release --root-key <pem> --domain <D> [--code dev] --out <file>` signs the
   config named by the global `--config` (default `m2k.toml`). Without `--code dev` it pins the current code digest.
   PRODUCTION with `--code dev` is refused with exit 2, the same way `sign-charter` refuses a dev release.
   Like the other `sign-*` commands, it never overwrites an existing `--out`.
3. **Config key**: `[trust] release = "<path>"`, resolved like the other `[trust]` paths. When `[trust]` is present
   it is required. A missing key is refused with a message that names the exact `sign-release` command to run.
   `m2k trust init-dev` prints a `release = "<out>/release.json"` line in its `[trust]` table. It then prints one
   line saying: after adding the table to `m2k.toml`, run `m2k trust sign-release --root-key <out>/root.pem --domain
   TEST_ONLY --code dev --out <out>/release.json`. (init-dev cannot sign it itself: the config is not final yet.)
4. **Startup self-check in `cmd_run`**: read the config file **once**. Parse and hash the same bytes, so the file
   cannot change between parsing and checking. Run the self-check immediately after the existing "no `[trust]`"
   handling, **before** the sandbox is chosen, the models are built, the Worker probe runs, or the ledger is used for
   the task. On failure:
   - print `拒绝启动：发行清单核对失败（<code>）：<reason>`, plus one line on how to fix it (re-sign with
     `m2k trust sign-release` if the owner made this change);
   - append one event `startup.refused` `{"task": <contract task id>, "code": <TrustError code>, "reason": …}` under
     the task id `m2k.startup`. Never use the contract's own task id: that would block a later legitimate run with the
     same id;
   - exit 2. No model call, no probe, no `task.submitted`.
   `--untrusted` runs have no Root and no manifest check. They keep their existing warning.
5. **Fingerprint before the probe** (T40 follow-up): compute `framework_fingerprint` right after the self-check,
   before the Worker probe, and add the release manifest file to its `files`. A config change or manifest swap
   during the probe or the task is then caught by T40's existing checks.
6. **`m2k trust status`** prints one more line, `release manifest:` with `ok`, `code changed`, `config changed`, or
   `missing`, and exits non-zero unless it is `ok`. The `m2k-task` workflow already runs `trust status` before the
   task, so CI fails early with a readable reason.
7. **CI workflow (`m2k-task.yml`)**: replace the two heredocs that write `/root/m2k/m2k.toml` with a committed file,
   `trust/test/m2k-task.toml`. Its bytes are the current heredoc content, plus `release = "trust/release.json"` in
   `[trust]`. Add the committed manifest `trust/test/release.json`, signed by the **TEST_ONLY Root of `trust/test`**
   with `--code dev`. Install both from the pristine checkout (same place and permissions as the other public trust
   files). The workflow's behaviour must not change otherwise.
   - The Root private key is not in the repo. Look on this computer for the key whose public key matches
     `trust/test/root-pin.json` (candidates: `<offline-keys>`, the old project's `control/trust`).
     Never commit it, copy it into the repo, or print it. If you cannot find it, stop and ask; do not make a new Root.
   - Commit `trust/test/*.toml` and `release.json` so they keep LF line endings (the digest normalizes CRLF anyway).

## Acceptance
`python -m unittest tests.test_release tests.test_trust_required tests.test_fingerprint tests.test_trust_cli -v` and
the full suite. New tests in `tests/test_release.py` (temporary keys from `trust.dev_setup` / `generate_keypair`; never
the real test Root):
- A valid TEST_ONLY manifest with `code = "dev"` lets a scripted `m2k run` reach PASS. The same with a pinned code
  digest also passes.
- Each of the following is refused with exit 2, the right code, one `startup.refused` event under `m2k.startup`, no
  `task.submitted`, and no model call (patch `build_model` and the probe to prove it): one byte of the config changed
  (e.g. the reviewer model name); the `[obs]` public key swapped; `[trust] release` removed; the manifest signed by
  another Root; a manifest from the other domain; `code = "dev"` under PRODUCTION; a pinned code digest that no longer
  matches. Afterwards, the same task id still runs normally under a correct manifest.
- The config with CRLF line endings verifies against a manifest signed over the LF version.
- `sign-release` refuses PRODUCTION with `--code dev` and refuses to overwrite `--out`.
- `trust status` reports `ok` / `config changed` / `code changed` / `missing` with the matching exit code.
- The framework fingerprint is computed before the probe runs. Its `files` include the manifest.
Existing tests that start `m2k run` with a `[trust]` section (`test_trust_required.py`, `test_fingerprint.py`,
`test_trust_cli.py`) may change only to sign a manifest for their config. Do not weaken any existing assertion.

Also show, in the handoff note, that `trust/test/release.json` verifies: run `m2k trust status` against a copy of
`trust/test/m2k-task.toml` whose `[trust]` paths point at the committed public files. Leave out `issuer_key`
(use a scratch issuer key), or use a scratch script that calls `verify_release` directly.

## Out of scope
- Rollback: re-installing an *older* config together with its own valid manifest is still accepted. Detecting it
  needs the external anchor (T44).
- Revoking manifests in the revocation list, the adjudicator checking the manifest, and OBS witnessing of the
  self-check (T44).
- The local demo configs `<demo-dir>\` and `<dev-keys>\`: the reviewer re-signs them after the merge.

## Review notes (written by reviewer)

**PASS (Claude, 2026-10-17), merged as #44.** CI and card-scope green; GLM-5.3 GO. Linux, Python 3.13: full suite 526 OK
(7 skipped). `m2k/` +93 lines. The config is read once; the same bytes are parsed, checked and pinned in the fingerprint.
Independently verified: `trust/test/release.json` verifies against `trust/test/root-pin.json` (Root `6357ec2addf1fd69`,
TEST_ONLY, `code = dev`). `trust/test/m2k-task.toml` equals the old workflow heredocs plus one `release` line.
Non-blocking: `trust status` labels any non-code/config failure as `missing`; the fix hint prints Windows paths with
doubled backslashes; `sign-release` with a missing config shows a traceback.
Carried to T44: rollback to an older config with its own valid manifest; manifest revocation; the adjudicator
checking the manifest.

## Handoff note (written by implementer, ≤ 10 lines)
- 基线 `main@c89f14c`；实现分支 `task/T43-release-manifest`，仅修改卡内文件，`m2k/` 新增 93 行。
- 已实现 Required behaviour 1–7：Root 清单签发/启动自检/拒绝记账/status；配置只读一次，配置与清单原字节进入试启动前指纹。
- 使用 `<python>`（3.13.11）：Acceptance 定向 59 项通过；全套 526 项通过（9 skipped）。
- 第 7 条：CI 配置逐字节等于原两段 heredoc 加 `release` 一行；配置/清单从干净 checkout 安装，权限均 600，Git 中均为 LF。
- 调用 `verify_release` 核验 `trust/test/release.json` + `trust/test/m2k-task.toml`：LF/CRLF 均通过，TEST_ONLY、`code = dev`。
- 清单 config SHA-256：`1afa5c0c077ca7dddec875c44194a6fba7875d31c2aeb7b6563b617445ddbd77`。
- 所用 Root 签名私钥对应公钥指纹：`6357ec2addf1fd6933575d36a39a0287b554fd5158e61d0244d8058e3240f71f`（短指纹 `6357ec2addf1fd69`）；未复制或输出私钥。
- 未运行真实模型的 `m2k-task`；卡保持 OPEN，待 GitHub CI、GLM 与审阅者裁定；不开 PR。
- 本地 `tools/card_check.py --base origin/main --title 'T43: Check Root-signed code and config manifests before startup'` 通过：范围合规，新增 93/100 行。
