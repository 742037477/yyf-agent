# T69: Anchor publishing integrates the remote branch before pushing; git errors keep their stderr; small defensive fixes collected from the T46/T63/T64/T65 reviews

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T69-small-fixes-2` (start from the latest main)
- **Line budget:** ≤ 160 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/anchor.py`, `m2k/cli.py`, `m2k/kernel.py`, `m2k/obs.py`, `m2k/trust.py`, `tests/test_anchor.py`, `tests/test_anchor_attack.py`, `tests/test_trust_cli.py`, `tests/test_release.py`, `tests/test_obs.py`, `tests/test_delivery.py`, `tests/test_trust_required.py`, `tests/test_stage5_attack.py`, `tests/test_adjudicate.py`, `tasks/T46_check.py`, `tasks/README.md`
- **Status:** DONE (#51)

## Goal
On 2026-10-18 a real dsh demo on the owner's computer passed, but warned `外部锚点未发布：git push failed (exit 1)`.
A read-only check found the cause: CI runs push their checkpoints to the same `ledger-anchors` branch, and
`anchor.publish` commits on top of a stale local branch and pushes without integrating the remote, so GitHub rejects
the push as non-fast-forward (local was ahead 1, behind 12). It will fail on every later local run, and in PRODUCTION
a failed publish means no delivery. The ledger only recorded `git push failed (exit 1)` because `anchor.git()` drops
git's stderr.

This card fixes that, and clears a set of small, non-blocking defects that reviewers recorded on earlier cards. None
of them changes a security decision; each turns a crash, a misleading label or a lost error message into a clear,
recorded outcome.

**Rule for every item below:** first write a test that reproduces the defect on current main. If an item cannot be
reproduced (it may already have been fixed), do not change code for it; say so in the handoff note.

## Required behaviour
1. **Publishing integrates the remote branch** (`anchor.publish`, `push=True` only). Before writing the anchor file
   into the clone: `git fetch origin +refs/heads/ledger-anchors:refs/remotes/origin/ledger-anchors`, then bring the
   local branch onto it with `git rebase refs/remotes/origin/ledger-anchors` (local, unpushed anchor commits are
   replayed; every ledger has its own file, so this never conflicts in normal use). If the rebase fails, run
   `git rebase --abort` and raise `ValueError` naming the rebase failure (with stderr, see 2). After the commit, push
   normally. If the push is rejected as non-fast-forward (someone pushed in between), repeat fetch + rebase + push
   **once**; a second rejection raises. **Never force-push**, never `reset --hard`, never rewrite commits that are
   already on the remote. The existing checks stay in front (branch must be `ledger-anchors`, clean working tree, the
   anchor file must extend the published one — compare against the file **after** the rebase). `push=False` keeps
   today's behaviour (no network). The existing local divergence on the owner's computer (ahead 1, behind 12) must be
   repaired by this code path on the next run without any manual step.
2. **git errors keep their stderr** (`anchor.git`). On failure the `ValueError` message is
   `git <subcommand> failed (exit N): <stderr>` where `<stderr>` is the last 300 characters of stderr, decoded with
   `errors="replace"`, stripped, newlines joined with ` | `, and with any credentials in URLs masked
   (`https://user:secret@host` → `https://***@host`; also mask any `Authorization:`/`extraheader` value). Every caller
   that records the message in the ledger (`anchor.failed`, `anchor.warning`, remote check problems) therefore keeps
   it. Success output is unchanged.
3. **Own anchor file with a malformed record** (`anchor.remote_check`). The T65 notary read
   (`record["receipt"]["body"]["notary"]`, and `record["v"]`) runs unguarded inside the outer `try`, so one malformed
   record ends the whole check with a bare `KeyError` text such as `'notary'`. Parse each published record
   individually: a record that is not a JSON object or lacks `v`/`receipt.body.notary` where needed adds one problem
   `<file>: malformed anchor record at line <n>` and the loop continues with the next record and the next file.
4. **A trust-chain failure while loading the setup refuses the start properly** (`cmd_run`). Today a `TrustError`
   from `trust.load_setup(...)` or `trust.verify_release(...)` (for example a charter signed by another Root:
   `CHARTER_SIGNATURE`) escapes as `SystemExit` with exit 1 and no ledger record. Catch `trust.TrustError` there: print
   `拒绝启动：信任链核对失败（<code>）：<message>` to stderr, append `startup.refused` with `code = e.code` and the
   message, exit **2**, and — as for every refusal — no `task.submitted`, no model call, no probe.
5. **OBS refusal codes** (`cmd_run`, the `release.checked` witness). Today every `ObsRefused` other than
   `root changed` is reported as `RELEASE_ROLLBACK`, including a malformed request. Map by the witness's reason:
   starts with `root changed` → `ROOT_CHANGED` (unchanged); starts with `release rollback`, `two manifests share`
   or `revocations rollback` → `RELEASE_ROLLBACK` (unchanged); anything else → `OBS_REFUSED`, with the witness's reason
   verbatim. Only the `RELEASE_ROLLBACK` case prints the re-sign hint. `ObsError` that is not a refusal stays
   `OBS_UNAVAILABLE`.
6. **`m2k trust status` labels** (`cmd_trust`).
   - A release manifest that fails for any reason other than code/config change prints
     `release manifest: invalid (<code>)` instead of `missing`; a manifest file that does not exist still prints
     `missing`. Exit code unchanged (1).
   - When the OBS key matches but the OBS log file does not exist yet, print `obs: ok (log not yet created)` (exit
     code unchanged); `obs root:` then reports `not yet recorded` as today.
7. **Delivery rename failure leaves nothing behind** (`Kernel` delivery release). If writing or renaming the portable
   receipt, or renaming the staged folder, raises `OSError`, remove what this run created (the staging folder, the
   `.tmp` receipt, and the final receipt file if it was already renamed into place) before recording
   `delivery.refused`. Never remove anything this run did not create.
8. **No private helper across modules.** `trust._read_json` is used from `cli.py`; expose it as `trust.read_json`
   (keep `_read_json` as an alias if other modules or tests use it) and use the public name in `cli.py`.

## Out of scope
- Local separate-user isolation (T66/T68/T67) and anything about which user runs git.
- The adjudicator's handling of `anchor.checkpoint`, `verify_chain` from a checkpoint, the CI workflow token handling,
  the Windows 32K command-line limit, the m2k-task workflow's "empty ledger has no head": later cards.
- Any change to what TEST_ONLY or PRODUCTION decides when publishing fails (TEST_ONLY warns, PRODUCTION refuses).

## Acceptance
```
python -m unittest tests.test_anchor tests.test_anchor_attack tests.test_trust_cli tests.test_release tests.test_obs tests.test_delivery tests.test_trust_required -v
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
Use local bare repositories for git (no GitHub). New tests must show:
- **Diverged clone publishes:** a bare remote with a `ledger-anchors` branch; clone A publishes ledger X; clone B (made
  before A's push) has an unpushed commit for ledger Y and then publishes again with `push=True` → succeeds, the remote
  has both files, history is linear, the remote's existing commits are unchanged (same hashes), and
  `remote_check`'s append-only history check reports nothing.
- **Race:** a push rejected once as non-fast-forward (simulate by pushing to the remote between the rebase and the
  push, e.g. with a patched `git` wrapper) succeeds on the single retry; rejected twice → `ValueError`, nothing forced.
- **stderr kept:** a failing git call's message contains git's own error text; a URL with `user:secret@` in stderr is
  masked.
- **Malformed own record:** one problem `malformed anchor record at line <n>`, and later records/files are still
  checked (e.g. a later notary change is still reported).
- **Charter signed by another Root:** `m2k run` exits 2, `startup.refused` with `CHARTER_SIGNATURE`, no
  `task.submitted`.
- **OBS refusal mapping:** a malformed `release.checked` refusal → `OBS_REFUSED`; rollback and root change keep their
  codes (existing tests unchanged).
- **trust status:** `invalid (<code>)` for a manifest with a bad signature; `missing` when the file is absent;
  `obs: ok (log not yet created)` with no log.
- **Delivery:** a patched `Path.rename` failing on the staged folder leaves no receipt, no `.tmp` and no staging
  folder, and records `delivery.refused`.

## After the merge (reviewer, not this card)
- No config changes, so no manifests are re-signed (three demo configs and `trust/test/m2k-task.toml` unchanged).
- Claude dispatches Codex to re-run the Claude Code and Codex demos (`<dev-keys>\m2k.claude.toml`,
  `<dev-keys>\m2k.codex.toml`), one after the other, each in a new time-stamped folder under `<demo-dir>`.
  Expected: PASS, the `Root 指纹：fa61f0d84958eaa2` line, **no** `外部锚点未发布` warning (the first run repairs the
  diverged local anchor clone by rebasing), `m2k --config <cfg> check-delivery <deliver_to>` OK. Then
  `git -C <anchors-repo> status --short --branch` shows no `ahead`/`behind`.
- A `workflow_dispatch` smoke run of `m2k-task` (CI publishes to the same branch; it must still pass).

## Review notes (written by reviewer)

- **PASS (Claude, 2026-10-18), merged as #51** (head bf8c58e). ci / card-scope green; GLM-5.3 GO twice. Independent review by a fresh Codex session (gpt-6-astra, ultra): first CHANGES REQUESTED (item 8: `adjudication_inputs` in `m2k/cli.py` still called `trust._read_json`), fixed by Claude in bf8c58e (plus a test that `cli.py` no longer names it), then PASS. Full suite 678 OK (11 skipped), offline acceptance 28/28, `tasks/T46_check.py` OK. `m2k/` +73 / 160. Claude spot-checked `m2k/anchor.py` and `m2k/cli.py`. The card's Files list was put on one line on the branch because `tools/card_check.py` reads only the first line.
- GLM non-blocking (later small card): with `push=True` the pre-write fetch needs an existing `ledger-anchors` branch on the remote (bootstrap must create it first); the non-fast-forward retry matches git's English stderr (a localized git skips the retry; consider `LC_ALL=C` for git calls); v2 records without a notary are now flagged in every published file, not only this ledger's; `tools/card_check.py` should read a wrapped Files list.

## Handoff note (written by implementer, ≤ 10 lines)

基线 main 35628d6；1–8 均先用新增/加强测试在未修改的 main 实现上复现，再作对应修复；无未复现项。
1：push=True 写文件前 fetch+rebase，失败 abort，非快进仅重试一次；分叉、竞态、重整后前缀与远端历史保留测试通过。
2–3：git 保留脱敏 stderr 尾部（URL/Authorization/extraheader）；坏记录逐行报错并继续检查后续记录和文件。
4–5：信任链失败在模型/探测前以 exit 2 记录 startup.refused，授权书提前校验；OBS 拒绝按原因分类，保留原原因。
6–8：status 区分 invalid/missing/未建日志；交付失败清除本次路径并保留他人文件；公开 read_json 并保留旧别名。
验收使用 <python>（3.13.11）：专项 226 项 OK（skip=2），离线 28/28，T46 CHECK OK（21 项、17 组防御开关）。
全套 unittest discover 678 项 OK（skip=11），T46 内置全套同结果；git diff --check 通过；m2k/ 新增 72/160 行。
git 测试仅临时本地裸仓库，离线验收在临时目录运行；未改真实锚点克隆、演示配置、已有 OBS 日志和账本。
卡外问题：tools/card_check.py 的 parse_card 只读 Files 首行，漏读续行测试文件并误报越界；完整名单核对无越界，留待审阅者处理，未改卡外工具。
