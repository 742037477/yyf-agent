# T42: independent OBS witness — every decisive ledger fact carries a receipt signed by a separate process

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T42-obs-witness`
- **Line budget:** ≤ 180 added lines in `m2k/`
- **Files you may change:** `m2k/obs.py` (new), `m2k/kernel.py`, `m2k/adjudicate.py`, `m2k/cli.py`, `m2k.example.toml`, `tests/test_obs.py` (new), `tasks/README.md`
- **Status:** DONE (#42)

## Goal
This is stage 5, card 3. The T39 and T40 cards call it "OBS evidence (T41)", but T41 went to DeepSeek Harness. See the
stage 5 decisions in `docs/ARCHITECTURE.md`.

This card closes the gap carried forward from T39. Someone who can write the ledger can append a forged tail
(frozen candidate, test, review, verdict). The hash chain stays valid, so the adjudicator, which honours the last
event of each kind, believes it.

After this card, an **OBS witness** runs as its own process, with its own Ed25519 key and its own hash-chained log.
The kernel hands every decisive event to the witness as it records it, and records the signed receipt it gets back.
The Gate adjudicates with the OBS public key **taken from the configuration, not from the ledger**. A decisive fact
without a valid receipt is not evidence.

This first OBS is local. The architecture states the limit plainly: if the agent runs as the same OS user without
isolation, an attack aimed at the framework can read the OBS key. A stronger notary (another user, another machine,
GitHub Actions) comes later. The receipt format and the Gate check stay the same; only `strength` changes.

## Required behaviour
1. New `m2k/obs.py`, standard library plus the existing `trust` helpers (`sign`, `open_envelope`, `canonical`,
   `generate_keypair`, `load_private_key`, `fingerprint`). Never hand-roll crypto.
   - **The witness:** `serve(key_path, log_path, stdin, stdout)` reads one JSON request per line:
     `{task_id, kind, ledger_id, ledger_seq, ledger_hash}`. For each request it:
     1. checks the shape: strings, a positive int, and 64-hex hashes;
     2. refuses a `ledger_seq` that is not greater than the last one it witnessed for the same `ledger_id`
        (no replay, no rollback);
     3. appends one line to its log;
     4. answers with a signed receipt envelope.

     Any refusal is answered `{"error": "..."}`, and nothing is logged.
   - **The log:** JSONL, append-only. Each line is `{v: 1, seq, ts, prev, request, receipt}`, where `receipt` is the
     complete signed receipt envelope and `prev` is the sha256 of the previous raw line (the genesis value for the
     first line), the same scheme as `anchor.py`. The receipt body's `obs_seq` and `obs_prev` equal this line's `seq`
     and `prev`.
   - **The receipt body:** `{schema: "m2k.obs.receipt/1", notary: <fingerprint of the OBS public key>,
     strength: "local", obs_seq, obs_prev, ts, task_id, kind, ledger_id, ledger_seq, ledger_hash}`.
   - **`ObsClient(command, public_key_b64, timeout_s=10)`:** starts `command` once, with pipes and a minimal environment
     (no M2 keys, no `[obs]` paths beyond those in `command`). `witness(event, ledger_id)` returns the verified receipt
     **envelope**, or raises `ObsError`. Verified means the signature matches the configured public key and the body
     repeats the request exactly. `close()` ends the process. Worker and acceptance commands must never inherit the
     witness's pipes.
   - **`check_log(log_path, public_key_b64=None)`:** returns a list of problems: a broken `prev` chain, a non-increasing
     `seq`, a malformed line. With a public key, also verify every receipt's signature, its `notary` against the key's
     fingerprint, its `obs_seq`/`obs_prev` against this line's `seq`/`prev`, and its task_id/kind/ledger_id/ledger_seq/
     ledger_hash against this line's `request`. An empty list means these checks passed.
2. **Kernel.** `Kernel(..., obs=None)`; `None` keeps today's behaviour, so existing tests pass unchanged. With an
   `ObsClient`:
   - `task.submitted` records `obs: {notary, strength}`.
   - Right after appending each event of kind `task.submitted`, `credential.issued`, `candidate.frozen`,
     `test.result`, `audit.result` or `verdict`, for the parent and for every subtask, the kernel witnesses it and
     appends `obs.receipt` `{receipt: <envelope>}`.
   - If witnessing fails, the kernel appends `obs.failed` `{kind, reason}`, and the task's verdict can no longer be
     PASS: it ends **UNKNOWN**, with reason `OBS_UNAVAILABLE: …`. Nothing is delivered. The kernel may stop the worker
     early; it must never retry by re-sending an already witnessed seq.
   - The delivery Gate calls `adjudicate(..., obs_public_key=<the configured key>)`.
3. **`adjudicate(ledger, task_id, check_chain=True, obs_public_key=None)`.** With a key, every decisive event it relies
   on needs an `obs.receipt` that:
   - verifies with that key;
   - repeats this task id;
   - names the event's exact `ledger_seq` and `ledger_hash`.

   The decisive events are the last `task.submitted`, `candidate.frozen`, `test.result`, `audit.result` and `verdict`,
   plus `credential.issued` when there is a trust chain. A missing or invalid receipt is a FAIL:
   `no valid OBS receipt for <kind> at seq N`. Merged subtasks are adjudicated with the same key. Without a key,
   behaviour is unchanged.
4. **CLI and configuration.**
   - `m2k obs keygen --out DIR`: refuses a non-empty DIR; writes `obs-key.pem` and prints the `[obs]` block to paste.
   - `m2k obs serve --key K --log L`: the witness process. Used by `ObsClient`; not meant for people.
   - `m2k obs check --log L [--public-key B64]`: prints the problems and exits 0 only when there are none.
   - `[obs]` in `m2k.toml` has `key`, `log` and `public_key_b64`; paths are relative to the config file. With `[obs]`,
     `m2k run` starts the witness. It refuses to start when the key or the log lies inside the task workspace, and when
     the key does not match `public_key_b64`. Without `[obs]`, it prints `警告：未配置 OBS 独立旁证，交付只凭账本裁决。`
     and records `obs: null`. (Making OBS mandatory is T44.)
   - `m2k status` shows the number of OBS receipts and any `obs.failed`.
   - `m2k.example.toml` gets a commented `[obs]` block that explains the same-user limit in one line.

## Acceptance
`python -m unittest tests.test_obs -v` and the full suite. Use `ScriptedModel`, temporary folders, and a real witness
process (`[sys.executable, "-m", "m2k", "obs", "serve", ...]`). The tests must show:
- **Happy path:** a task with OBS records one receipt per decisive event, adjudicates PASS with the key, and is
  delivered.
- **Forged tail:** after a task that FAILed, append a forged `candidate.frozen`, `test.result`, `audit.result` and PASS
  `verdict` to the ledger. The chain stays valid, but `adjudicate(..., obs_public_key=...)` is FAIL and names the
  missing receipts. Without the key it would have passed; assert that too, to show what OBS adds.
- **Wrong notary:** receipts signed by another key are FAIL.
- **Witness dies mid-task:** the verdict is UNKNOWN with `OBS_UNAVAILABLE`, and there is no delivery.
- **Witness refuses replay:** an equal or lower `ledger_seq` for the same `ledger_id` is refused.
- **Edited log:** edit a line and recompute every following `prev` so the chain looks intact. Assert that `check_log`
  without a public key finds no problem, while checking with the public key reports the edit.
- **`m2k run` refuses** an OBS key or log inside the workspace, and a key that does not match `public_key_b64`.

## Out of scope
- Anchoring the OBS log head externally, cross-checking ledger receipts against the OBS log, and making OBS mandatory
  (`T44`).
- Published release manifests and owner confirmation of config changes (`T43`).
- A cloud notary.
- External CLI agents (`kind = "cli"`) need no special case: they go through the same kernel events.

## Review notes (written by reviewer)

- 2026-10-17 Claude（实现前澄清）：OBS 日志行存完整回执信封，check_log 可验签；预算 180。
- 2026-10-17 Claude：PASS → squash 合并 #42。GLM GO；ci / card-scope 绿；Linux 全套 498 项通过。
- 留给 T44（不阻断）：OBS 失败时先记原判定再追加 UNKNOWN（结果对，时间线有两条 verdict，应先定 UNKNOWN 再记）；_record/adjudicate 每次全表读账本取 ledger_id，应缓存；obs.py 为压行数用分号挤写，下次动时展开；cmd_run 放宽的 except 应收窄到 [obs] 配置几行。

## Handoff note (written by implementer, ≤ 10 lines)

- 基于 main `71e61af` 实现；按 Claude 澄清同步完整回执日志、可选验签与 180 行预算。
- 独立进程以最小环境运行；Ed25519 回执先落只追加日志，再返回；恢复日志后仍拒绝重放与回退。
- 父子任务六类关键事件即时取回执；Gate 按配置公钥裁决，旁证中断最终 UNKNOWN / OBS_UNAVAILABLE，无交付。
- CLI 增加 keygen / serve / check、配置路径及密钥核对、未配置警告与 status 旁证计数/失败信息。
- 真实旁证进程验证伪造尾部、错误公证方、六处中断、超时、重启防重放及重算日志链后的验签差异。
- 可信基线裁决/指纹 19 项通过；使用 `<python>`（3.13.11）。
- `python -m unittest tests.test_obs -v`：19 项通过；`python -m unittest discover -s tests -t .`：498 项（489 通过、9 跳过），无失败。
- `m2k/` 新增 180 行；仅修改卡内文件和本卡，未动 CLAUDE.md；未开 PR、未推 main，CI 与 GLM/Claude 审阅待执行。
