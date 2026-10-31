# 协作流程（固定规范）

## 角色

| 角色 | 由谁担任 | 负责 | 不负责 |
| --- | --- | --- | --- |
| 架构 / 最终验收 | Claude | 分阶段计划、拆卡写卡、经 MCP 向 Codex 派活、开 PR、每个 PR 的最终审阅与合并决定 | 任何功能实现与代码修改，包括合并冲突（用户决定 2026-10-17） |
| 实现 | Codex（gpt-6.1-sol，推理 max） | 按任务卡写代码和测试、推送分支、按审阅意见返工与解决冲突 | 卡外的任何修改、流程文档、开 PR、自判完成 |
| 首轮代码审查 | GLM-5.3（GitHub 自动） | 对照任务卡审 PR，给出 GO / NO_GO 与阻断问题 | 提卡外需求；最终合并决定 |
| 机械脚本 | DeepSeek-V4-Flash（经 m2k 运行） | 补测试、样例、小工具等边界清楚的小卡 | 核心逻辑 |
| 产品验收 | 用户 | 阶段结束时跑 `m2k accept`，按表格判断行为对不对 | 读代码 |

框架运行时同样采用这套分工：Worker 用 DeepSeek，独立审查者用 GLM-5.3。

攻击场景分工（用户决定 2026-10-13）：Codex 只写防御实现和不含攻击载荷的测试；需要构造真实攻击代码的测试与
`m2k accept` 攻击场景（伪造字节码、注入文字、越权读写等）写成单独的卡，由 DeepSeek 经 m2k（`m2k-task` 工作流）执行，
GLM-5.3 审查，Claude 最终复核。原因：Codex 的服务方安全机制会中断含攻击代码的任务（T10 先例）。

## 一张卡的生命周期

```
Claude 写卡 tasks/Txx-*.md（推到 main）
  → Claude 经 MCP 派给 Codex：“按 AGENTS.md 做 tasks/Txx”（规则见下方“Codex 派活”；MCP 不可用时由用户转述）
  → Codex 在分支 task/Txx-xxx 上实现并推送分支（不自己开 PR）
  → Claude 开 PR，标题以 “Txx:” 开头
  → GitHub 自动检查（全部免费 / 极低成本）：
       ci         Windows + Linux 全部测试 + 离线场景验收
       card-scope 只改了卡里允许的文件、新增行数不超预算
       glm-review GLM-5.3 对照任务卡首轮审查，在 PR 里留言
  → Claude 审阅：PASS → 立即 squash 合并（用户决定 2026-10-13：审阅通过即自动合并，不再等人工确认）；
    打回 → 在 PR 里列出必须修改的点
    自动合并的前提：ci 与 card-scope 全绿；GLM 若为 NO_GO，Claude 已逐条说明为何不阻断；
    审阅结论同时写进 PR 评论和任务卡的 Review notes
  → 最多返工 2 次；第 3 次仍不过，卡退回 Claude 重新拆分
```

DeepSeek 小卡：首选在 GitHub 上跑（见下）；命令在 bubblewrap 隔离环境中执行。本地运行需要 Linux（装好 bubblewrap），
Windows / macOS 本地只能写 `[sandbox] backend = "none"`（接受命令不隔离的风险，记入账本）。
也可以在 GitHub 上跑（不需要本地电脑）：把 `tasks/Txx.toml` 写进 `.github/m2k-task-trigger` 推到 main，
工作流 `m2k-task` 用 DeepSeek 干活、GLM-5.3 审查；PASS 后自动把卡允许的文件推到卡的分支，Claude 开 PR，后面同上。
Key 防护：Key 只在 root 专属文件里，m2k 从工作区外的干净代码副本运行，所有命令以无 sudo 的 `m2kw` 用户执行，账本在工作区外（见 T04）。

## Codex 派活（用户决定 2026-10-17）

Claude 通过用户电脑上的 Codex MCP 服务（Claude 桌面端本地 MCP `codex`，工具 `codex_start_task` / `codex_task_status` /
`codex_reply_to_task` / `codex_answer_questions` / `codex_cancel_task`）直接给 Codex 派活，不再由用户转述。固定策略：
- 每张卡（或每次返工）用 `codex_start_task` 开新会话，不沿用旧会话；
- 项目 `<repo>`；模型按任务复杂度混用（用户 2026-10-29）：复杂实现 `gpt-6-astra`、大部分中等实现 `gpt-6.1-sol`、简单实现与调查及脚本 `gpt-6.1-terra`；独立复审 `gpt-6-astra`（推理 `ultra`）；
- 只读核查用 `read-only`，需要提交推送的用完全访问；指令开头先让 Codex 切到最新 `main` 或卡的分支；
- 本机测试用 Python ≥ 3.11（系统默认 `python` 是 3.10，用 `<python>`）；
- Codex 提问时 Claude 只回答卡内已定的事项，卡外的决定转给用户。
- 派活后 15～20 分钟内 Claude 用定时提醒查一次 Codex 状态（Codex 提问不会通知 Claude）；有提问当场答，还在跑就再设提醒（用户决定 2026-10-31）。
- 可自行处理、不必停下的情况（用户决定 2026-10-31）：只是旧测试的断言要跟着卡里写明的新行为同步时，Codex 按最保守的方式改（只改受新行为直接影响的断言，
  不删其他断言），在 Handoff note 逐条写明，由复审核对。要改变 `m2k/` 的行为或安全语义、改卡外文件、超行数预算时，仍须停下提问。

## 省 Claude 额度（用户决定 2026-10-17）

Claude 的额度最宝贵：Claude 是大脑，其他 AI 是执行者、辅助审查者和真实攻击运行者。
- 复审：Codex 起一个新会话做独立复审（检出 PR 分支到临时 worktree、跑全套测试、对卡逐条核对，回 ≤ 20 行中文结论并点名抽查位置），
  不改不推；Claude 读它的结论和 GLM 的结论，只抽查 `m2k/` 里点名的关键改动，不自己跑全套测试。合并决定仍由 Claude 做。
- 收尾杂务交给 Codex：合并后的卡状态、Review notes（Claude 给结论原文）、`tasks/README.md`、冒烟分支与删分支、交接文档草稿；
  Codex 直接提交到 main，提交信息带 `[direct-ok]`。
- 不交出去：写任务卡（Claude 亲自把控卡的质量和范围）、DeepSeek 演练卡的代码骨架、合并决定、需要用户拍板的问题。

## 任务说明里的代码骨架（用户决定 2026-10-17）

- DeepSeek 经 m2k 做的卡（攻击演练、测试、验收场景）：Claude 可以在 `tasks/Txx.toml` 的 goal 里给出本地验证过的代码骨架和逐步指令，
  DeepSeek 补全、运行、修到验收通过。原因：DeepSeek 只看得到最近 12 轮、容易反复读代码而不动手（T46 第 1 段首次 40 轮未写文件）。
  骨架先在本地临时模块跑通，并用验收脚本的防御开关确认测试真能拦住攻击。
- Codex 做的核心逻辑卡：只写规格，不写代码。
- 补偿审查独立性：GLM 照常审查；验收脚本逐个关掉防御，证明测试真的生效；Claude 在卡的审阅说明里注明哪些部分是自己起草的。

## 防发散规则（写给所有实现者）

1. 一张卡一个目标；只改卡上列出的文件；遵守行数预算。CI 会机械检查，超出即红。
2. 卡外发现的问题只在交接说明里写一行，不修。
3. “完成”由 CI 绿灯 + Claude 审阅通过决定，不由实现者自己宣布。
4. GLM 的“非阻断建议”在本 PR 中一律不处理。
5. 不写报告、证据包、状态文档；不改 AGENTS.md。
6. 每张卡在 Codex 里开新会话。

## 分支与提交

- `main` 只接受 squash 合并的 PR；直接推送会被 `main-guard` 检查标红（私有仓库的分支保护需要 GitHub Pro，这是免费替代）。
- 分支名 `task/Txx-<短名>`；PR 标题 `Txx: <一句话>`；合并后删除分支。
- 任务卡本身由 Claude 直接提交到 main（提交信息带 `[direct-ok]`）。

## 一次性设置（用户在 GitHub 网页上操作）

1. 仓库 Settings → General → Pull Requests：只勾选 “Allow squash merging”，勾选 “Automatically delete head branches”。
2. Settings → Secrets and variables → Actions → New repository secret：`GLM_API_KEY` = GLM 的 Key。
3. 同一页面的 Variables 标签（可选）：`GLM_BASE_URL`。默认是 GLM Coding Plan 端点 `https://open.bigmodel.cn/api/coding/paas/v4`（已用 model-ping 工作流验证当前 Key 只能用这个）；换成按量付费 Key 时改为 `https://open.bigmodel.cn/api/paas/v4`。
4. （可选，约 4 美元/月）升级 GitHub Pro 后可开启真正的分支保护：要求 PR、要求 ci 与 card-scope 通过。
