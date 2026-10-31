# T40: framework fingerprint — the code, config and trust files a task runs under are pinned and re-checked

- **Assignee:** Codex
- **Branch:** `task/T40-framework-fingerprint`
- **Line budget:** ≤ 90 added lines in `m2k/`
- **Files you may change:** `m2k/fingerprint.py`, `m2k/kernel.py`, `m2k/trust.py`, `m2k/cli.py`, `m2k/adjudicate.py`, `tests/test_fingerprint.py`, `tasks/README.md`
- **Status:** DONE (#41)

## Goal
This is stage 5, card 2 (see `docs/ARCHITECTURE.md`, stage 5 decisions). A worker command that runs without
isolation can edit M2 itself while a task is running. It could change the framework code, change the config (for
example to swap the reviewer or switch isolation off), or replace the trust files.

Today nothing notices: TEST_ONLY charters use `release = "dev"`, which skips the code check, and the config is not
covered at all. After this card, every task is pinned to the exact framework it started under. Any change during the
task stops it before the next action, before the test and before delivery.

## Required behaviour
1. New `m2k/fingerprint.py` with `framework_fingerprint(code_dir, files) -> dict`:
   - `code`: `trust.release_digest(code_dir)`;
   - `files`: `{name: sha256 of the file bytes}` for each given file (the config file, root pin, charter, revocations,
     issuer key), or `None` for a missing file;
   - `digest`: sha256 of the canonical JSON of `code` and `files`.

   Standard library only; never read or print file contents beyond hashing them.
2. `m2k run` computes the fingerprint before it opens the ledger. The files are the config file and every path in
   `[trust]`. It passes the fingerprint to `Kernel(..., framework=...)`. The new parameter defaults to `None`, which
   is the old behaviour, so existing callers and tests keep working.
3. With a fingerprint, the kernel:
   - records it in `task.submitted` as `framework`;
   - puts `framework_sha256` into the task credential (and into child credentials) when there is a trust chain;
   - recomputes it wherever the chain is verified: the `trust_chain` gate before every action, and before test and
     delivery. Without a trust chain, it recomputes before test and before delivery only.
4. On a mismatch, the check fails with code `FRAMEWORK_CHANGED`, which names the parts that changed (code, or a file
   name). The worker stops with `TRUST_BROKEN`, the run is not PASS, and nothing is delivered.
5. `adjudicate` FAILs a task whose credential `framework_sha256` differs from the submitted `framework.digest`.
6. `m2k status` shows the framework digest.

## Acceptance
`python -m unittest tests.test_fingerprint -v` and the full suite. Use `ScriptedModel` and temporary copies; never
modify the real `m2k/` files in a test. To simulate tampering, give `Kernel` a fingerprint computed over a temporary
copy of the code and files, and change that copy:
- in the middle of a task, from a scripted step (a callable step can edit the copy);
- between the action and the test.

Each must stop with `FRAMEWORK_CHANGED`, with no delivery.

## Out of scope
- Deciding whether a release or config is *allowed* (that is T43: published releases and owner confirmation of config
  changes).
- OBS evidence (T41).

## Review notes (written by reviewer)
- 2026-10-17 Claude: PASS → squash 合并 #41。GLM GO；ci / card-scope 绿；全套 479 项通过（变基到含 T41 的 main 后，cli.py 两边保留，由 Codex 完成）。
- 留给 T43（不阻断）：cmd_run 在 Worker 试启动前读配置、试启动后才算指纹，试启动期间改动的配置会被当作原样固定；应改为在试启动前计算。账本里记录的是绝对路径（不含内容）。

## Handoff note (written by implementer, ≤ 10 lines)
- 基于可信 main `c76305b` 独立实现于指定分支；T41 已先行推送，本分支不混入其改动。
- 新增代码/文件字节指纹及规范 JSON 摘要，缺失文件记 None；只记录摘要与路径，不记录配置或密钥内容。
- CLI 开账本前计算指纹；提交事件、父子签名凭证绑定摘要，status 展示，裁决器拒绝凭证摘要不一致。
- 有链动作前及验收/交付边界复查；无链只在验收/交付边界复查，保留子任务原有召回检查。
- 变化以 FRAMEWORK_CHANGED 列明 code/文件名，Worker 为 TRUST_BROKEN，结果非 PASS，无交付。
- 对照：原基线验链/子凭证/裁决 28 项通过；framework=None 的同类临时副本变化保持原行为 PASS。
- `python -m unittest tests.test_fingerprint -v`：12 项通过，覆盖临时副本改动、删除、父子凭证、CLI 开账本顺序与只读裁决。
- `python -m unittest discover -s tests -t .`：476 项通过（Windows / Python 3.13.11，9 项跳过）；真实 m2k 文件未被测试改写。
- 范围检查通过；`m2k/` 新增 71 行（预算 90）。未开 PR、未合并，CI 和审阅结论留待审阅者确认。
- 变基到含 T41 的 main，cli.py 冲突两边保留（先警告后指纹）；全套 479 项通过。
