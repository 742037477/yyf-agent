# T70: card-scope reads a wrapped Files list; git runs in the C locale; first anchor publish to an empty branch; malformed `[anchor]` is a clear error; the adjudicator reads `release.checked` by seq

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T70-small-fixes-3` (start from the latest main)
- **Line budget:** ≤ 80 added lines in `m2k/` (tests and `tools/` not counted)
- **Files you may change:** `tools/card_check.py`, `m2k/anchor.py`, `m2k/cli.py`, `m2k/ledger.py`, `m2k/adjudicate.py`, `tests/test_card_check.py`, `tests/test_anchor.py`, `tests/test_anchor_attack.py`, `tests/test_ledger.py`, `tests/test_adjudicate.py`, `tests/test_trust_cli.py`, `tests/test_release.py`, `tasks/README.md`
- **Status:** DONE (#52)

## Goal
Clear the non-blocking notes the reviewers left on T63 and T69 that are small and safe to fix together. None of them
changes a security decision. Keep the **Files you may change** list of this card on one line until item 1 is merged.

**Rule for every item:** first write a test that reproduces the defect on current main. If an item cannot be
reproduced, do not change code for it; say so in the handoff note.

## Required behaviour
1. **card-scope reads a wrapped Files list** (`tools/card_check.py`, `parse_card`). T69's card wrapped its list over
   three lines and the gate read only the first, flagging five allowed test files as out of scope. The list now runs
   from `**Files you may change:**` up to the next line that starts a new bullet (`- **`) or a heading (`#`), or a
   blank line, whichever comes first; every backticked pattern in that span counts. A one-line list parses exactly as
   today. Nothing after the end of that span may be picked up (in particular not backticked text in Goal or Required
   behaviour). Add `tests/test_card_check.py` covering: one-line list, wrapped list (as in `tasks/T69-small-fixes-2.md`
   on main), list followed directly by `- **Status:**`, and a card whose later sections contain backticked paths that
   must not be allowed. Run the gate against every existing card in `tasks/` and confirm no card's pattern set
   shrinks.
2. **git runs in the C locale** (`anchor.git`). T69 detects a non-fast-forward rejection by git's English stderr, so a
   localized git would skip the retry. Every `git` call made through `anchor.git` runs with the current environment
   plus `LC_ALL=C` and `LANGUAGE=C`. Nothing else in the environment changes (credential helpers must still work).
3. **First publish to a remote without the branch** (`anchor.publish`, `push=True`). Since T69 the pre-write fetch
   fails when `origin` has no `ledger-anchors` branch yet. Check first with
   `git ls-remote --heads origin ledger-anchors`: when it returns nothing, skip the fetch + rebase and push (this push
   creates the branch); the T69 single retry still applies. When the branch exists, behaviour is exactly T69's.
4. **Malformed `[anchor]` is a clear configuration error** (`cli._anchor_path` and its callers). Today values such as
   `file = 1` or `repo_dir = []` end in a traceback. `[anchor]` must be a table whose `file` is a non-empty string and
   whose optional `repo_dir`, if present, is a non-empty string; otherwise raise `ValueError` with the message
   `[anchor] must be a table with a non-empty string file (and repo_dir, if set)`. `m2k run`, `m2k anchor` and
   `m2k check-ledger` report that message on stderr with a non-zero exit and no traceback. A well-formed `[anchor]`
   and the legacy top-level `anchor_file` behave exactly as today.
5. **The adjudicator reads `release.checked` by seq** (`adjudicate.py`, `ledger.py`). Today it lists every
   `m2k.startup` `release.checked` event to find one seq. Add `Ledger.event(seq) -> Event | None` (one `SELECT ... WHERE
   seq = ?`, same row-to-Event conversion as `events`), and use it: the event must exist, have task id `m2k.startup`
   and kind `release.checked`, otherwise the existing error (`release.checked missing or names a different manifest or
   run`) is raised exactly as today. Results of `adjudicate` are unchanged for every existing test.

## Out of scope
- Local separate-user isolation (T66/T68/T67); the CI workflow files; `verify_chain` from a checkpoint; the adjudicator
  learning `anchor.checkpoint`; the Windows command-line length limit.

## Acceptance
```
python -m unittest tests.test_card_check tests.test_anchor tests.test_anchor_attack tests.test_ledger tests.test_adjudicate tests.test_trust_cli tests.test_release -v
python tools/card_check.py --base origin/main --title "T70: x"
python -m m2k accept --offline
python tasks/T46_check.py
python -m unittest discover -s tests -t .
```
Local bare repositories only for git (no GitHub). New tests must show: item 1 as listed above; item 2 a fake `git` on
`PATH` (or a patched `subprocess.run`) sees `LC_ALL=C` and `LANGUAGE=C` and still sees the caller's other variables;
item 3 publishing with `push=True` to a bare remote that has no `ledger-anchors` branch creates it, and a second
publish then integrates as in T69; item 4 each malformed shape in each of the three commands gives the message and no
traceback; item 5 a forged `release.checked` under another task id, or a different kind at that seq, is rejected as
today, and a normal run still adjudicates PASS.

## After the merge (reviewer, not this card)
- No config changes; no manifests re-signed.
- Confirm on the next task PR that card-scope passes with a wrapped Files list.

## Review notes (written by reviewer)

- **PASS (Claude, 2026-10-18), merged as #52** (head 84e8954). ci / card-scope green; GLM-5.3 GO (the first run returned an empty reply and was relabelled). Independent review by a fresh Codex session (gpt-6-astra, ultra): PASS with no blocking or non-blocking issues; all 52 existing cards parse to identical allowed sets under the new `parse_card`. Full suite 691 OK (11 skipped), offline acceptance 28/28, `tasks/T46_check.py` OK. `m2k/` +23 / 80. Claude spot-checked `tools/card_check.py`, `m2k/anchor.py`, `m2k/adjudicate.py`, `m2k/ledger.py`.
- GLM non-blocking: `ls-remote --heads origin ledger-anchors` also matches a ref whose name merely ends in `ledger-anchors` (then behaves as before T70); `Ledger.event` raises `OverflowError` for seq ≥ 2**63 when called directly (the adjudicator guards it).

## Handoff note (written by implementer, ≤ 10 lines)

基线 origin/main 82c48ec；1–5 均先用新增测试在未修改的 main 实现上复现，再作最小修复；无未复现项。
1：Files 跨行解析遇元数据项、标题或空行截止；逐张运行 52 张现有任务卡的 gate，允许模式集合均未缩减。
2–3：anchor.git 只覆盖 LC_ALL/LANGUAGE=C 并保留其余环境；空远端首次 push 建分支、第二次整合远端，T69 单次重试回归通过。
4：[anchor] 的表、file、repo_dir 类型与空值统一报指定 ValueError；15 种坏配置 × 3 个命令均非零退出且无 traceback，合法及旧配置回归通过。
5：Ledger.event 单次按 seq 查询；裁决保留任务、种类、清单及运行上下文核对，字符串、集合和超大序号仍返回 main 原有拒绝结果。
验收使用 <python>（3.13.11）：专项 170 项 OK，离线 28/28，T46 CHECK OK（21 项、17 组开关），全套 691 项 OK（skip=11）。
git diff --check 通过；修改范围已逐文件核对，m2k/ 新增 23/80 行；CI 与审阅结论由审阅者确认。
git 测试仅使用临时本地裸仓库；主工作区未切分支，未改真实锚点克隆、演示配置、已有 OBS 日志与账本。
