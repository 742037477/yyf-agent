# T88: 部署文档与一键配置脚本——让懂技术的人、或别人的 Agent 照着文档把 m2k 装起来跑通第一个任务

- **Assignee:** Codex `gpt-6.1-sol`（max）实现；GLM-5.3 必审 + Codex `gpt-6-astra`（ultra）独立复审
- **Branch:** `task/T88-setup-guide`
- **Line budget:** ≤ 0 added lines in `m2k/`（不改框架代码；脚本放 `tools/`，不计入预算）
- **Files you may change:** `tools/quickstart.py`（新建）、`tests/test_quickstart.py`（新建）、`docs/SETUP.md`（新建）、
  `docs/AGENT_SETUP.md`（新建）、`README.md`、`tasks/T88-setup-guide.md`、`tasks/README.md`
- **Status:** DONE (merged #66, 31e36ac)

## 背景（用户 2026-10-31：封版后的小破例）
用户想把 m2k 给两类人用：①懂技术的朋友自己部署；②做 Agent harness 产品的人，让他们自己的 Agent 照文档部署并接入。
现在的 README 是开发记录，不是上手手册。Claude 在全新目录实测过上手流程，坑如下（文档和脚本要把它们消掉）：
- `trust init-dev` 打印 `[trust]`、`[obs]` 两张表，要人手工拼进 `m2k.toml`；输出目录非空时拒绝，第二次运行会失败。
- `trust sign-release` 必须在配置**最终定稿后**签；之后再改配置，要把 `[trust] release` 指向一个新文件，并显式给 `--version N+1` 重签，
  否则启动报 `release manifest: config changed`。
- Windows / macOS 没有 bubblewrap，必须写 `[sandbox] backend = "none"`（命令不隔离，记账）；Linux 装了 bubblewrap 才有隔离。
- 任务 ID 不可复用，重跑要改 `id`。账本默认在 `%LOCALAPPDATA%\m2k\` 或 `~/.local/state/m2k/`。
- 外部 Agent（`kind = "cli"`）的 token 用量只认 `codex-jsonl`、`claude-json` 两种格式，其他 CLI 记为未知，`max_tokens` 预算无法据此执行。

## 1. `tools/quickstart.py`（约 150 行，只用标准库 + 本仓库 `m2k`，Windows / macOS / Linux 都能跑）
两个子命令，都通过 `m2k.cli.main([...])` 或 `python -m m2k` 调现有命令完成，不复制框架逻辑：

### `init <部署目录>`
- 部署目录必须在仓库之外、且不存在或为空，否则拒绝（退出 2）。
- 参数：
  - `--worker api --worker-base-url URL --worker-model NAME (--worker-key-env NAME | --worker-key-file PATH)`，
    或 `--worker codex`、`--worker claude-code`（照 `examples/codex_demo/m2k.codex.toml`、`examples/claude_code_demo/m2k.claude.toml` 的
    `[models.worker]` 写法），或 `--worker cli --worker-command '<JSON 数组>' [--worker-prompt stdin|arg]`（自家 harness 的通用接法）；
  - `--reviewer-base-url URL --reviewer-model NAME (--reviewer-key-env NAME | --reviewer-key-file PATH)`（审查者始终是 API 模型）；
    审查者与 Worker 同一模型时必须另给 `--allow-same-model`；
  - `--accept-no-sandbox`：没有可用的 bubblewrap 时必须显式给出才写 `backend = "none"`，否则拒绝并说明原因；有 bubblewrap 时写 `"auto"`。
- 步骤：在 `<部署目录>/keys` 跑 `trust init-dev`；写 `m2k.toml`（模型、sandbox、`[trust]`、`[obs]`，`ledger` 指向 `<部署目录>/state/ledger.sqlite`，
  路径用相对配置目录的写法）；跑 `trust sign-release ... --version 1 --out keys/release-1.json`；跑 `trust status` 并要求退出 0；
  把 `examples/demo_calc/` 复制成 `<部署目录>/tasks/demo_calc/`；最后打印下一步要跑的命令（`m2k --config ... ping`、`run`、`status`、`check-delivery`）。
- **Key 只写环境变量名或文件路径，绝不读取、打印或写入 Key 的内容**；不校验 Key 文件内容，只检查文件存在。

### `resign <部署目录>`
- 用户改完 `m2k.toml` 后运行：读当前 `[trust] release` 指向的清单版本 N，把 `release` 改成 `keys/release-<N+1>.json`，再用 `--version N+1` 签，
  跑 `trust status` 要求退出 0。不改配置里的其他任何内容。

## 2. `docs/SETUP.md`（中文，给人看，约 200 行）
1. **你会得到什么、得不到什么**：三五行，链接 `POSITIONING.md`；明说这是 TEST_ONLY 测试信任链，同一系统用户下没有隔离，不适合跑不可信 Agent。
2. **前置条件**：Python ≥ 3.11、`git`、`pip install cryptography`；仓库获取方式：`git clone https://github.com/742037477/m2-kernel`（用户 2026-10-31 决定公开仓库；本卡实现时仓库可能仍是私有，文档按公开写）；可选 bubblewrap（Linux）。
3. **五步上手**：clone → `quickstart.py init`（给出 DeepSeek 做 Worker + GLM 做审查、和 Codex 做 Worker 两个完整示例命令）→ 设置 Key
   （PowerShell、bash 两种写法）→ `m2k ping` → `m2k run` 第一个任务 → 看结果（`status`、`log`、`report`、`live`、`check-delivery`）。
   每一步写“成功时应看到什么”。
4. **写自己的任务契约**：用 demo_calc 讲每个字段（引用 README 的契约表，不重复全文）；验收文件要受保护；任务 ID 不可复用。
5. **改配置后重签**：`quickstart.py resign`；手工做法一段。
6. **接入方式**：API 模型（OpenAI 兼容）做 Worker；外部 Agent CLI 做 Worker（Codex、Claude Code 现成写法）；指向 AGENT_SETUP 的“接入自家 harness”。
7. **平台差异**：Linux（bubblewrap）、Windows / macOS（`none`）各自意味着什么。
8. **常见报错**：表格，**报错原文照框架实际输出**（实现时逐条触发并复制），至少包括：没有 `[trust]`、`release manifest: config changed`、
   任务 ID 已存在、没有隔离后端、模型连不上 / Key 错、Worker 与审查者同一模型、`check-delivery` 的“未经核验”。
9. **安全须知**：Key 不进配置文件和仓库；`keys/` 里的 `root.pem` 就是测试 Root 私钥，谁拿到谁能签清单；部署目录不要放进任务工作区；
   同用户 Agent 能读到 OBS 私钥（见 POSITIONING）。
10. **重来 / 卸载**：删部署目录和账本文件即可；换 Root 要换 OBS 和账本（链接 README “换 Root”）。

## 3. `docs/AGENT_SETUP.md`（英文，给别人的 AI Agent 照着执行，约 150 行）
写成可执行的检查清单，每步给出确切命令和“成功的判据”（退出码、输出里应出现的字样）：
- **Ask the human first（不要自己决定）**：用哪个 Worker（API / codex / claude-code / 自家 CLI）和审查模型；Key 放在哪个环境变量或文件；
  没有 bubblewrap 时是否接受不隔离（`--accept-no-sandbox`）；部署目录放在哪。
- **Hard rules**：never print, copy, log or commit API keys or anything under `keys/`; never run `m2k run --untrusted`; never edit `m2k.toml`
  without running `quickstart.py resign` afterwards; never put the deployment folder or ledger inside a task workspace; stop and report the exact
  error text instead of working around a refusal.
- 步骤：环境检查 → `init` → `trust status` → `ping` → 跑 demo_calc → `check-delivery` → 报告给人（只报结果和路径，不报 Key）。
- **Integrating your own agent harness**：`kind = "cli"` 的要求——非交互运行、从 stdin（或最后一个参数）读任务说明、以工作区为当前目录、
  干完正常退出；`env` 白名单只放必要变量；用量只识别 `codex-jsonl` / `claude-json`，其他格式记为 unknown（所以 token 预算不能约束它，
  靠 `timeout_s` 和外部计费兜底）；可选 `.m2k/ask.json` 提问或申请授权（格式链接 README 的 T79 一节）；`isolation` 字段写厂商自己声明的隔离，
  m2k 只记录不核验；`m2k ping` 会先试启动一次。给一个最小 `[models.worker]` 例子。
- 最后一段 “What m2k does not guarantee”，三五条，链接 POSITIONING。

## 4. README
- “快速开始”一节开头加两行：给别人部署看 `docs/SETUP.md`，让 Agent 部署看 `docs/AGENT_SETUP.md`。其他不动。

## 5. 测试 `tests/test_quickstart.py`
- `init`（api 预设 + `--accept-no-sandbox`）在临时目录生成的配置能被 `trust status` 通过（退出 0）；配置里只有环境变量名，没有任何 Key 值；
  `release-1.json` 存在；demo 任务已复制。
- `--worker codex` 生成 `kind = "cli"` 且命令与 `examples/codex_demo` 一致；`--worker cli --worker-command` 解析 JSON 数组，非法时退出 2。
- 同一模型不给 `--allow-same-model` 时拒绝；没有 bubblewrap 又不给 `--accept-no-sandbox` 时拒绝；部署目录非空或在仓库内时拒绝且不改动。
- `resign`：改一行模型配置后 `trust status` 不过 → `resign` 后通过，`release` 指向 `release-2.json`、版本 2，其他配置行不变。
- 不调真实模型、不联网。

## Out of scope
- `m2k/` 的任何改动（发现框架的毛病只在 Handoff note 写一行）；PRODUCTION 信任域、正式 Root 离线签发、隔离、安装包。
- 改 `examples/` 下现有示例。

## Acceptance
```
python -m unittest tests.test_quickstart -v
python -m unittest discover -s tests -t .
```
**真机验收（Windows，用户电脑）**：在 `<worktrees>\T88-deploy\`（仓库外）照 `docs/SETUP.md` 从零做一遍：
Worker 用 DeepSeek（`deepseek-flash`，Key 文件 `<deepseek-key-file>`），审查用 GLM（`glm-5.3`，Key 文件 `<glm-key-file>`，
端点 `https://open.bigmodel.cn/api/coding/paas/v4`）；DeepSeek 的 base_url 与模型名照 R0 实验配置
`<experiments>\R0\m2k.r0.toml`（只读，不改）；`ping` 通过、demo_calc 判 PASS 并交付、`check-delivery` 全 OK。花费约几分钱。
**Key 绝不打印、复制、提交或写进日志。** 把 SETUP 里每一步的实际输出与文档对照，不一致就改文档。常见报错表逐条实际触发。完成后删掉该目录。
复审另外核对：文档里每条命令照抄能跑；报错原文与框架实际输出一致；没有把 TEST_ONLY 写成可用于生产；AGENT_SETUP 的 Hard rules 齐全。

## Review notes (written by reviewer)

- 2026-10-31 Claude: merged #66 (31e36ac). GLM-5.3 GO. Independent Codex review (gpt-6-astra, ultra) round 1 returned four blocking points: private machine paths and key-file paths in the new public docs; `resign` rewrote the config before signing and did not restore it on failure; the `release` replacement could turn a valid triple-quoted string into invalid TOML; the custom-CLI JSON example failed when copied into Windows PowerShell 5.1.
- 2026-10-31 Claude: rework 1 (02d2862) fixed all four — placeholder paths; resign restores the original config bytes and removes the new manifest on any failure, then a plain retry succeeds; only a single-line `release` in `[trust]` is replaced and the result is compared with tomllib, other forms exit 2 untouched; tested PowerShell 5.1 and bash forms. Claude checked the rework line by line; quickstart 13 / full suite 1028 passed; CI green.
- 2026-10-31 Claude: real-machine acceptance by the implementer: DeepSeek worker + GLM reviewer deployed from scratch, ping OK, demo_calc PASS and delivered (6 model calls), check-delivery all OK. Known framework leftover (not fixed): `m2k ping` prints WARN for {"ok": false} but exits 0. The card itself still lists private paths in its Acceptance section; they are removed when the public snapshot is cleaned (next card).

## Handoff note (written by implementer, ≤ 10 lines)

- 新增 quickstart init/resign：TEST_ONLY、API/CLI 预设、Key 来源引用、空目录与隔离校验；m2k/ 零改动。
- 新增 11 项离线测试通过；全套 1026 项 OK（15 skipped）；Python 3.11 语法与本地 card-scope 通过。
- 对照 d6f9367：原全套 1015 项 OK（15 skipped）；未改旧测试或卡外代码。
- SETUP/AGENT_SETUP、README 入口与任务索引已更新；成功输出与 9 类常见报错已逐条实际核对。
- Windows 真验：R0 原端点/模型、原 Key 文件路径，双 ping OK；calc-001 PASS 并交付（6 calls / 5022 tokens），完整 check-delivery 全 OK。
- status/log/report/live、重签 v2～v5 通过；真实 Key 未打印、复制或写入配置/日志，R0 配置未改。
- 框架遗留：本机 HTTP 响应 {"ok":false} 可使 ping 打印 WARN 仍退出 0；未修。
- T88-deploy 删除命令被自动审批拒绝（blocked by policy），目录保留；未改用其他工具绕过。
- Linux/macOS 未真机运行；CI、GLM 与独立复审待确认；未开 PR、未推 main。
- 返工 1：对照 fd5bf35 复现后修复 resign：签名/status 失败恢复原配置字节并删新清单，恢复 Root 后可直接重试 v2；仅接受 [trust] 单行 release，写前解析并核对其余全部 TOML 值不变。
- 返工 1：两份公开指南私人路径已泛化；PowerShell 5.1/Git bash 文档原命令均在未设置 Key 的临时部署实跑 init=0，无模型调用；指定 rg 仍命中卡外 docs/WORKFLOW.md、docs/ARCHITECTURE.md，按范围未改。
- 返工 1：quickstart 13 项、全套 1028 项 OK（15 skipped）；本地 card-scope、Python 3.11 语法与 diff 检查通过，m2k/ 零改动；CI 与独立复审待确认。
