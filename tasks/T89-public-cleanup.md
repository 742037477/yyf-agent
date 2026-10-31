# T89: 公开前清理——去掉本机路径、Key 文件位置、账号名和私人项目名；补 LICENSE、.gitignore；收紧 ci.yml

- **Assignee:** Claude 独立完成（用户 2026-10-31：两张卡都不经 Codex；纯文本卡，不贴 `glm-review`，不派独立复审，以 CI 与第 5 节验收为准）
- **Branch:** `task/T89-public-cleanup`
- **Line budget:** ≤ 0 added lines in `m2k/`（`m2k/` 一行不改）
- **Files you may change:** `tasks/T*.md`、`tasks/README.md`、`README.md`、`docs/AGENT_SETUP.md`、`docs/ARCHITECTURE.md`、
  `docs/SETUP.md`、`docs/WORKFLOW.md`、`examples/delivery_check/README.md`、`m2k.example.toml`、`LICENSE`（新建）、
  `.gitignore`、`.github/workflows/ci.yml`
- **Status:** DONE (merged #67, 2a2b314)

## 背景（用户 2026-10-31 拍板）
m2k 已封版，要以新的干净公开仓库 `https://github.com/742037477/yyf-agent` 发布：MIT 许可证、版权人 `yyf`；
本私有仓库保持私有，先在这里清理、合并，再由 Claude 把清理后的 main 导出成**一个不带历史的提交**推到新仓库。
公开前审查（结论“处理后可公开”）：无真机密；要处理的是当前文件里的个人环境信息、缺 LICENSE、`.gitignore` 不全、`ci.yml` 未显式只读。

## 1. 文本脱敏（实测 2026-10-31：36 个文件，约 140 处）
要去掉的四类（**完整匹配清单不入库**，只存在 Claude 的工作目录里，避免清单本身把这些名字带进公开仓库）：
1. 所有者电脑上的工作目录路径（G 盘下的仓库、worktree、实验、演示、密钥目录，以及用户主目录、本机 Python 解释器路径）；
2. Key 文件的位置（D 盘的 DeepSeek、GLM Key 文件或配置文件路径）；
3. 所有者的 Windows 账号名；
4. 私人旧项目及其目录名（旧建设项目的 private / public / governance 仓库名、离线密钥目录名、另一套审计账本目录名）。

替换规则：
- 路径一律换成尖括号占位符（占位符本身要能读懂，不另加解释）：
  `<repo>`（本仓库的本机克隆）、`<worktrees>`、`<experiments>`、`<demo-dir>`、`<dev-keys>`、`<anchors-repo>`、`<python>`（Python ≥ 3.11 解释器）、
  `<deepseek-key-file>`、`<glm-key-file>`、`<user-home>`、`<owner-account>`（所有者的 Windows 账号）、`<old-project>`（旧项目，名称不公开）。
  路径的子目录、文件名部分照原样保留（例如 `<worktrees>\T87-demo-run`、`<dev-keys>\m2k.claude.toml`），只换掉前缀。
- 只换这些字符串，**不改写其他内容**：结论、数字、日期、拍板记录、Review notes、Handoff note 原样保留；不顺手润色、不重排。
  Files 列表、Line budget、Status 行一字不动（`tests/test_card_check.py` 会解析全部卡）。
- 下列**通用写法保留不动**（是框架默认值或测试数据，不是个人信息）：`m2k/`、`tests/`、`tools/` 里的全部内容；
  README 的 `C:\m2k-acl-backup` 与 `C:\Users\<账号>`；任务卡里 `C:\m2k-tools`、`C:\m2k-t76-accept…`、`C:\m2k-t78-shortname` 这类临时测试目录、
  CI 跑机路径（`RUNNER~1`、`runneradmin`）、签发盘 `H:\` 和单独的盘符；ARCHITECTURE 里“U 盘 / H 盘签发、G 盘密钥仅测试用”的设计描述。
- `m2k.example.toml` 的 isolation 示例值改成通用示例：`data_dir = 'D:\m2k\svc-data'`、`tools_dir = 'C:\m2k-tools'`、
  `protected = ['D:\m2k\keys', 'D:\m2k\private', 'D:\m2k\demo']`、`code_dir = 'D:\m2k\m2-kernel'`、`owner_user = "alice"`；
  注释里的 `api_key_file` 示例改成 `"D:/keys/codex-config.toml"`。
- `docs/WORKFLOW.md` 里本机 Python 那句改成“本机测试用 Python ≥ 3.11 的解释器（`<python>`）”，项目路径用 `<repo>`。

## 2. 仓库地址与说明
- `docs/SETUP.md`、`docs/AGENT_SETUP.md`、`examples/delivery_check/README.md` 里的 `git clone` 地址改成 `https://github.com/742037477/yyf-agent`
  （原来带 `.git` 的那处保留 `.git` 后缀），clone 后的目录名相应改成 `yyf-agent`（文中若有 `cd m2-kernel` 一并改）。
  任务卡里的旧仓库地址（历史记录）只按第 1 节处理，不改成新地址。
- `README.md` 标题下加两行：项目原名 m2-kernel，命令与 Python 包仍叫 `m2k`；许可证 MIT（见 `LICENSE`）。
  再在合适位置加一句：公开仓库只带不用任何 secrets 的 `ci.yml`；`docs/WORKFLOW.md` 里的 GLM 审查、`m2k-task` 等工作流属于原私有开发仓库，公开版未包含。

## 3. LICENSE 与 .gitignore
- 新建 `LICENSE`：标准 MIT 全文，版权行 `Copyright (c) 2026 yyf`。
- `.gitignore` 追加（保留原有各行）：`.env`、`.env.*`、`*.pem`、`*.key`、`.credentials.yaml`、`auth.json`、`keys/`、`*.sqlite`、`*.sqlite-wal`、`*.sqlite-shm`。
  追加后 `git ls-files -ci --exclude-standard` 必须为空（不能让已入库文件变成被忽略）。

## 4. 收紧 `ci.yml`（只改权限，不改测试内容）
- 顶层加 `permissions: { contents: read }`（写成 YAML 块形式）；两个 job 的每个 `actions/checkout` 加 `persist-credentials: false`。
- 其他工作流文件不动（私有仓库还要用）；导出公开快照时由 Claude 删掉它们（见第 6 节）。

## 5. 验收
1. 本机清单（仓库外文件）：`git grep -nIE -f <清单文件>` **零输出**。
2. `git grep -nIE "[A-Za-z]:\\\\"` 剩下的每一处都属于第 1 节“保留不动”的范围；Handoff note 写剩余处数。
3. `python -m unittest discover -s tests -t .` 与 `python -m m2k accept --offline` 全过；`git diff --stat origin/main -- m2k/ tests/ tools/` 为空。
4. `git ls-files -ci --exclude-standard` 为空。

## 6. 合并后（Claude 做，不在本卡分支上）
- 从清理后的 main `git archive` 导出当前树，删掉 `.github/workflows/` 里除 `ci.yml` 以外的文件，作为**一个提交、不带历史**推到 `742037477/yyf-agent` 的 `main`；新仓库不配置任何 secrets。
- 再由 Claude 在全新克隆里跑全套测试、`python -m m2k accept --offline`、`examples/delivery_check` 演示和 `tools/quickstart.py init`（不填真 Key，只验证到 `trust status` 通过为止）。

## Handoff note（Claude，2026-10-31）
- 脚本按第 1 节规则替换 24 个文件 141 处（任务卡 21 张 + ARCHITECTURE、WORKFLOW、delivery_check README）；`m2k.example.toml`、clone 地址、README 说明手改；新增 `LICENSE`，`.gitignore` 加 10 条，`ci.yml` 加 `permissions: contents: read` 与两处 `persist-credentials: false`。
- 验收（Linux，Python 3.13.18，无 bubblewrap）：本机清单 14 条模式 `git grep` 零命中；盘符剩余处全部属于“保留不动”范围（README 3、`m2k.example.toml` 5 处通用示例、T18/T75/T76/T78 的默认值与临时测试目录、本卡自身）；
  `m2k/`、`tests/`、`tools/` 零改动；`git ls-files -ci --exclude-standard` 为空；全套 1028 项 OK（skipped=18）；`m2k accept --offline` 28/28；`m2k.example.toml` 用 tomllib 解析通过。
- 未做：Windows 上没跑（本卡不改代码）；第 6 节的公开快照在合并后另做。

