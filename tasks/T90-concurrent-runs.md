# T90: 同一部署多个 run 并发——OBS / 锚点追加加跨进程锁、见证按账本顺序、模型请求按端点排队

- **Assignee:** Claude（用户 2026-11-02：代码 Claude 亲自写，审查流程不变：GLM-5.3 + Codex 独立复审）
- **Branch:** `task/T90-concurrent-runs`
- **Line budget:** ≤ 220 added lines in `m2k/` (tests not counted)
- **Files you may change:** `m2k/locks.py`, `m2k/obs.py`, `m2k/ledger.py`, `m2k/kernel.py`, `m2k/cli.py`, `m2k/anchor.py`, `m2k/models.py`, `tests/test_concurrency.py`, `docs/SETUP.md`, `m2k.example.toml`, `tasks/T90-concurrent-runs.md`, `tasks/README.md`
- **Status:** DONE (merged #68, 88e53a1)

## 背景（2026-11-02 事故）
另一台电脑上用 quickstart 部署（TEST_ONLY），同时启动 20 个 `m2k run`：0/20 交付，OBS 日志 5 处断链，之后所有新任务
`OBS_UNAVAILABLE: witness unavailable: Expecting value: line 1 column 1 (char 0)`，部署报废。
根因（代码缺陷，不是部署配错）：
1. 每个 run 起一个 `obs serve`，serve 只在启动时读一次链尾，之后用内存里的 seq/prev，追加时不加锁 → 多个写者分叉。
2. serve 的“同一账本 ledger_seq 必须递增”检查假设见证顺序 = 账本顺序；两个 run 先后记账、却可能倒序见证 → 被拒
   （云端复现：20 个里 10 个 `OBS_REFUSED`）。
3. serve 启动失败时 stderr 被丢弃，客户端把空回复当 JSON 解析，报出误导性的 `Expecting value`。
4. 锚点文件 `anchor.append` 同样是“读链尾再追加”不加锁；锚点克隆上的 git 操作并发会抢 ref 锁。
5. 服务商按 Key 限并发（GLM 约 8～10 路），并发 run 多了大量 429，m2k 没有排队。

用户要求：至少支持 150 个 run 同时跑；压测 20 / 100 / 150。

## Required behaviour
1. `m2k/locks.py`：跨进程文件锁（Windows `msvcrt.locking`，其他 `fcntl.flock`）；`mutex(path)` 用旁边的 `<name>.lock`，
   默认最多等 120 秒；`held(paths)` 取第一把空闲的锁，N 个文件 = N 个槽位。
2. `obs.serve`：启动时在锁内校验整份日志（日志坏了照旧直接退出，旧测试不变）；每次追加都在锁内先读入别的 serve
   追加的行（逐行校验链与 seq、核对末行回执签名），再从文件真实链尾续写。读入失败或等锁超时回
   `{"error", "unavailable": true}`，客户端一律按 OBS_UNAVAILABLE 处理（release.checked 也不当成 OBS_REFUSED）。
3. `ObsClient`：任何失败时若 serve 已退出（含首个请求前就退出、写 stdin 先失败），报
   `OBS witness process exited (<stderr 末行>)`；`witness unavailable:` 后面不再为空。
4. `Ledger.append_then(..., then)`：在账本旁的跨进程锁（`<ledger>.lock`）内先提交事件、再调用 `then(event)`；内核
   `_record`（配了 OBS 时）和 `cmd_run` 的 `release.checked` 用它见证 → 见证顺序 = 账本顺序；中途中断最多留下
   “有事件、无见证”（照旧 UNKNOWN），不会留下已回滚事件的见证。没有 OBS 时 `_record` 仍走 `append`。
   SQLite 等锁超时由 5 秒放宽到 120 秒。
5. `anchor.append` / `publish` / `remote_check` 加锁（顺序：锚点文件，再克隆目录，克隆锁最多等 900 秒）。
6. `OpenAICompatModel(max_concurrent=N)`：同一 `base_url`（所有模型合计）在本机所有 m2k 进程间最多 N 个请求在途，
   其余排队；不写时行为不变；非正整数报 ValueError；取槽失败报 ModelError。

## Out of scope
- 一个 run 内子任务并行（T58 / R3 调度器）；外部 CLI Worker 的并发上限；`trust status` 的浅 OBS 检查。

## Acceptance
```
python -m unittest tests.test_concurrency -v
python -m unittest discover -s tests -t .
python -m m2k accept --offline
```
压测（Claude 在云端 Linux 做，脚本不入库）：本地假 GLM 端点（在途超过 10 个即回 429），quickstart 新部署，N 个
`m2k run` 同时启动；事后 `obs check --public-key`、`check-ledger`、`check-delivery`，并在同一部署补跑一个新任务。

## Review notes (written by reviewer)
- 第 1 轮：GLM-5.3 GO（7 条非阻断）。Codex 独立复审（gpt-6-astra，Windows）打回 2 条：①见证在账本事务内、提交前，
  进程中断或 COMMIT 失败时账本回滚而 OBS 见证留下，下一事件复用 seq 被误拒为 replay；②serve 在首个请求前退出时
  写 stdin 先失败，绕过读 stderr，Required behaviour 3 未满足。返工：①改为账本旁跨进程锁内“先提交、后见证”；
  ②任何失败都检查 serve 是否已退出并带出 stderr 末行。新增 2 个测试（中断不留孤立见证；退出后再请求）。
  行数 208 → 216，预算由 Claude 放宽 210 → 220。返工后云端全套 1036 OK、离线 28/28、N=20/100/150（排队 8）全部交付。
- 第 2 轮：Codex 独立复审 PASS（Windows 全套 1036 项通过 1021、跳过 15；离线 28/28；真实中断与 COMMIT 失败各复测 2 次，
  旧提交 4/4 留孤立见证、新提交 4/4 正常；8 进程 80 次见证序号严格递增；未见锁等待环）。非阻断：账本旁锁无限等待，
  不保证公平，持锁进程长期停滞会拖住队列（ObsClient 10 秒超时兜底）。GLM 第 1 轮 GO 的 7 条非阻断不改。
  Claude 抽查 ledger.append_then、ObsClient 失败诊断、kernel._record 回执顺序：通过。squash 合并 #68 = 88e53a1。

## Handoff note (written by implementer, ≤ 10 lines)
- 修复前代码 N=20：0 交付，OBS 断链，10 个 OBS_REFUSED + 5 个 OBS_UNAVAILABLE，事后新任务报 `Expecting value`（复现事故）。
- 修复后、不排队 N=20：OBS 完整、10 交付，另 10 个是 429；N=150 不排队：OBS 完整，56 交付，其余全是 429。
- 修复后、`max_concurrent = 8`：N=20 / 100 / 150 全部交付（10.6 s / 49.6 s / 72.4 s），零 429，在途峰值 8，
  OBS 链与账本链完整，事后新任务 PASS。
- N=200（2 核 8 GB 容器）：184 交付；6 个 run 和 8 个 OBS 子进程被系统按内存杀掉、2 个见证超时，均干净拒绝，链仍完整。
- 全套测试与离线场景见 PR；Windows 只在合并到 main 后由 CI 跑（`msvcrt` 分支在 PR 上测不到，请复审重点看）。
