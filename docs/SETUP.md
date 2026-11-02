# 部署与第一个任务

m2k 把所有者验收、独立审查、冻结交付和事后文件核验串在一起。
本手册生成 **TEST_ONLY 测试信任链**，不用于生产；PASS 只覆盖当时的候选和验收。
同一系统用户运行时，没有账号隔离，Agent 能读到 OBS 私钥；不适合运行不可信 Agent。
能力、实验结论与限制见 [POSITIONING.md](POSITIONING.md)。

## 前置条件

- Python ≥ 3.11、git、能安装 `cryptography`；脚本只用标准库与仓库里的 m2k。
- API Worker 和审查者需要 OpenAI 兼容的 Chat Completions 端点及各自的 Key。
- Codex / Claude Code Worker 要先安装 CLI、登录，并确认 `codex` / `claude` 在 PATH 中。
- Linux 可安装 bubblewrap（如 `sudo apt install bubblewrap`），且系统须允许其创建命名空间。
- Windows / macOS 通常需要显式接受命令不隔离；这不会创建独立系统账号。

以下命令从仓库根目录执行，`python` 必须是上述版本。Windows 可先用
`Set-Alias python 'C:/path/to/python.exe'` 选择符合要求的解释器；将占位路径换成自己的解释器路径。

## 五步上手

### 1. 获取代码与依赖

```text
git clone https://github.com/742037477/yyf-agent
cd yyf-agent
python --version
python -m pip install cryptography
```

成功：Python 显示 3.11 或更高，pip 安装成功或显示 `Requirement already satisfied`。
仓库根目录中能找到 `tools/quickstart.py`。保留这个代码目录，后面的 `python -m m2k` 从这里运行。
不要求安装 m2k 包；如果要在任意目录使用 `m2k` 命令，可另执行 `python -m pip install -e .`。

### 2. 创建部署目录（二选一）

部署目录必须不存在或为空，且在仓库之外；脚本遇到旧部署不会覆盖。
先选目录。PowerShell：

```powershell
$env:PYTHONUTF8 = '1'
$DEPLOY = Join-Path $env:USERPROFILE 'm2k-deploy'
```

bash（Linux / macOS）：

```bash
export PYTHONUTF8=1
DEPLOY="$HOME/m2k-deploy"
```

DeepSeek Worker + GLM 审查者（下面这条命令可在两种 shell 中运行）：

```text
python tools/quickstart.py init "$DEPLOY" --worker api --worker-base-url https://api.deepseek.com/v1 --worker-model deepseek-flash --worker-key-env DEEPSEEK_API_KEY --reviewer-base-url https://open.bigmodel.cn/api/coding/paas/v4 --reviewer-model glm-5.3 --reviewer-key-env GLM_API_KEY --accept-no-sandbox
```

或者 Codex Worker + GLM 审查者（使用不同的空目录，不能在上一个部署上再 init）：

```text
python tools/quickstart.py init "$DEPLOY" --worker codex --reviewer-base-url https://open.bigmodel.cn/api/coding/paas/v4 --reviewer-model glm-5.3 --reviewer-key-env GLM_API_KEY --accept-no-sandbox
```

Claude Code 把 `--worker codex` 换为 `--worker claude-code`。两种预设直接沿用仓库示例的 command、env、usage 和隔离声明。
Linux 上 bubblewrap 可用时省略 `--accept-no-sandbox`；脚本写 `backend = "auto"`。
不可用时，只有明确接受不隔离才加该参数，脚本写 `backend = "none"`。
同一模型审查需要额外的 `--allow-same-model`；不同厂商模型更能避免共同盲点。

成功：退出 0，输出含 `slot dev version 1`、`release manifest: ok`、`obs: ok`、
`obs root: not yet recorded` 和 `TEST_ONLY deployment ready.`，最后列出下一步命令。
目录中包含 `m2k.toml`、`keys/release-1.json`、测试链和 `tasks/demo_calc/`。
账本随后写到 `state/ledger.sqlite`，不在任务工作区内。

### 3. 提供 Key，并确认连接

选择环境变量时，在**将要运行 m2k 的终端**中设置。以下输入隐藏，不回显 Key，也不把 Key 写进命令历史。
PowerShell（Codex / Claude Code Worker 只需 GLM 那行）：

```powershell
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'DeepSeek Key' -AsSecureString)).Password
$env:GLM_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'GLM Key' -AsSecureString)).Password
```

bash：

```bash
read -rsp 'DeepSeek Key: ' DEEPSEEK_API_KEY; echo; export DEEPSEEK_API_KEY
read -rsp 'GLM Key: ' GLM_API_KEY; echo; export GLM_API_KEY
```

也可在 init 时把 `--worker-key-env DEEPSEEK_API_KEY` 替换成 `--worker-key-file '已有Key文件绝对路径'`，
把 `--reviewer-key-env GLM_API_KEY` 替换成 `--reviewer-key-file '已有GLM文件绝对路径'`。
这是互斥的两种来源；Key 文件为现成的 UTF-8 纯文本文件，脚本只检查存在，配置里只保存绝对路径，不读取或复制内容。
例如在 Windows 使用已有 Key 文件（把占位路径换成自己的绝对路径，无需输入、读取或复制 Key）：

```powershell
python tools/quickstart.py init "$DEPLOY" --worker api --worker-base-url https://api.deepseek.com/v1 --worker-model deepseek-flash --worker-key-file "C:/path/to/deepseek-key.txt" --reviewer-base-url https://open.bigmodel.cn/api/coding/paas/v4 --reviewer-model glm-5.3 --reviewer-key-file "C:/path/to/glm-key.txt" --accept-no-sandbox
```

这条替代第 2 步的 init，只能选一条 init。文件来源不需要设置上面的环境变量。

```text
python -m m2k --config "$DEPLOY/m2k.toml" trust status
python -m m2k --config "$DEPLOY/m2k.toml" ping
```

成功：trust status 退出 0，含 `release manifest: ok` 与 `obs: ok`。
ping 退出 0，两行都是 `OK`（API 输出模型名和 token 数；CLI 输出 `external agent` 与 `answered`）。
ping 会调用每个 API 模型一次，或试启动外部 Agent 一次，产生少量消耗；须同时核对退出码与两行 OK。
GLM Coding Plan Key 要用示例中的 coding 端点；按量付费 Key 的端点以供应商配置为准。

### 4. 跑第一个任务

```text
python -m m2k --config "$DEPLOY/m2k.toml" run "$DEPLOY/tasks/demo_calc/task.toml"
```

成功：退出 0，含 `calc-001: PASS` 与 `delivered to:`。
交付在 `tasks/demo_calc/out/calc-001/`，旁边有 `calc-001.m2k-receipt.json`；不要移动或编辑后再冒充原始交付。
首次运行还会显示 Root 指纹；`backend = "none"` 时会警告命令不隔离并记账。
FAIL / UNKNOWN 都不交付；退出 3 表示待所有者授权，按 [README 的 T79](../README.md#挂起与所有者批准t79) 处理。
这不是固定模型调用次数的承诺；反复失败时先停下检查原因。

### 5. 看结果与核验

```text
python -m m2k --config "$DEPLOY/m2k.toml" status calc-001
python -m m2k --config "$DEPLOY/m2k.toml" log calc-001
python -m m2k --config "$DEPLOY/m2k.toml" report calc-001 --out "$DEPLOY/calc-001.html"
python -m m2k --config "$DEPLOY/m2k.toml" check-delivery "$DEPLOY/tasks/demo_calc/out"
python -m m2k --config "$DEPLOY/m2k.toml" live --task calc-001 --no-browser
```

成功：status 的 JSON 中有 PASS，log 列出事件，report 打印 HTML 路径且文件存在。
check-delivery 退出 0，输出 `OK calc-001 (2 files)` 和 `合计 1 份：通过 1 份，未经核验 0 份`。
live 是持续服务，打印本地 URL；打开网页看回放，按 Ctrl+C 停止。它也可在第 4 步运行时另开终端观看。

## 写自己的任务契约

先复制部署目录里的 `tasks/demo_calc/` 为另一份任务，修改契约和 seed；字段全集见 [README 契约表](../README.md#任务契约tasktoml)。
demo 中 `[task] id / goal` 是唯一 ID 和四则运算目标；`workspace = "ws/{task_id}"` 每次用一个新工作区；
`seed = "seed"` 复制所有者的 `check.py`；`protected = ["check.py"]` 禁止 Worker 修改它。
`deliver_to = "out"` 指定交付根目录；`tools` 限制工具，`constraints` 给 Worker 和审查者共同阅读。
`[acceptance] files = ["check.py"]` 自动保护验收文件，`command = "{python} check.py"` 用当前解释器验收，`timeout_s` 限时。
`[budget]` 限定轮数、模型调用、tokens、命令；demo 的 `max_model_calls = 15` 包含审查，要按自己的任务缩减或调整。
验收必须由所有者编写并放进 seed，列入 acceptance.files；不要让 Worker 自己决定如何判成功。
所有相对路径以**契约所在目录**为准；配置中的相对路径则以配置目录为准。
任务 ID 不可复用；重跑先把 `id` 改为 `calc-002`，工作区将自动换新，旧记录保留。

## 改配置后重签

先改完 `m2k.toml`，再执行（不需要联网）：

```text
python tools/quickstart.py resign "$DEPLOY"
```

成功：从版本 N 生成 `keys/release-<N+1>.json`，打印 `slot dev version <N+1>` 与 `release manifest: ok`，退出 0。
脚本只替换 `[trust] release` 的值，其他配置文字保持原样，旧清单也保留；不要重复 init。
仅支持 `[trust]` 表内单行的 `release = "..."` 或 `release = '...'`；其他写法退出 2，提示“请手工改成单行写法”，不写配置。
签名或 trust status 失败会恢复重签前的配置字节、删除本次新清单；修好原因后直接再跑 resign。
手工做法（示例当前为版本 1）：先仅把 `[trust] release` 改为 `"keys/release-2.json"`，然后运行：

```text
python -m m2k --config "$DEPLOY/m2k.toml" trust sign-release --root-key "$DEPLOY/keys/root.pem" --domain TEST_ONLY --code dev --slot dev --version 2 --out "$DEPLOY/keys/release-2.json"
python -m m2k --config "$DEPLOY/m2k.toml" trust status
```

必须先改指向再签最终配置；清单不覆盖已有文件，每次递增版本。不要在签完后再改配置。

## 同时跑多个任务

同一个部署可以同时启动多个 `m2k run`（每个任务用不同的 id），它们共用账本、OBS 日志和锚点文件。
写入按跨进程锁串行，OBS 按账本顺序见证；实测 150 个 run 同时启动全部交付、OBS 链完整（T90）。
API 模型的服务商通常按 Key 限并发（例如 GLM 约 8～10 路），超出部分直接 HTTP 429。在
`[models.worker]` 和 `[models.reviewer]` 里各加一行 `max_concurrent = 8`（不超过服务商上限），
同一 `base_url` 的请求就会在本机所有 m2k 进程之间排队，最多同时 8 个在途；改完要 resign。
外部 CLI Worker（Codex、Claude Code 等）自己调用模型，`max_concurrent` 管不到它们。
每个 run 另有一个 OBS 子进程；150 个并发约 300 个 Python 进程，按内存量力而行。
OBS 日志、锚点文件旁会出现 `.lock` 文件，属于正常现象，不要删除正在运行时的锁文件。

## 接入方式与平台差异

API Worker 用 `kind = "openai_compat"`，端点须支持 `/chat/completions`；审查者始终是 API 模型。
Codex 和 Claude Code 使用 `kind = "cli"`，现成命令见 [Codex 预设](../examples/codex_demo/m2k.codex.toml) 和 [Claude Code 预设](../examples/claude_code_demo/m2k.claude.toml)。
接自家 harness 时，用下面对应 shell 的命令替代第 2 步的 init；`$DEPLOY` 必须是另选的空目录，不能对旧部署再次 init。
Windows PowerShell 5.1（保留 JSON 内双引号前的反斜杠）：

```powershell
python tools/quickstart.py init "$DEPLOY" --worker cli --worker-command '[\"my-agent\", \"--non-interactive\"]' --worker-prompt stdin --reviewer-base-url https://open.bigmodel.cn/api/coding/paas/v4 --reviewer-model glm-5.3 --reviewer-key-env GLM_API_KEY --accept-no-sandbox
```

bash：

```bash
python tools/quickstart.py init "$DEPLOY" --worker cli --worker-command '["my-agent", "--non-interactive"]' --worker-prompt stdin --reviewer-base-url https://open.bigmodel.cn/api/coding/paas/v4 --reviewer-model glm-5.3 --reviewer-key-env GLM_API_KEY --accept-no-sandbox
```

两种写法均已在本机临时目录实测 init 退出 0（Windows PowerShell 5.1 / Git bash，Key 环境变量未设置，不调用模型）。
后续 ping/run 前须安装并认证自己的 `my-agent`，设置审查 Key；完整要求见 [AGENT_SETUP](AGENT_SETUP.md#integrating-your-own-agent-harness)。
外部 CLI 的 token 用量只支持 `codex-jsonl` / `claude-json`；其他格式记为未知，token 预算无法据此执行。

Linux bubblewrap 只让任务工作区可写，运行库只读，默认断网；还需系统允许命名空间，安装成功不等于可用。
Windows / macOS 的 `none` 不隔离 Worker 命令和验收命令。外部 CLI 的隔离只是厂商声明，m2k 不核验它。

## 常见报错

以下片段已在 Windows（Python 3.13）逐条实际触发；路径、系统错误号、模型名和供应商错误体可能变化。

| 情况 / 命令 | 实际输出片段 | 处理 |
| --- | --- | --- |
| 没有 `[trust]`，`trust status` | `no [trust] configured` | 用空目录 init 测试链，不绕过信任链。 |
| 签后改配置，`trust status` | `release manifest: config changed` | 完成全部修改后 resign。 |
| 任务 ID 重复，`run` | `task id 'calc-001' already exists in the ledger; use a new id` | 换新 id 和新工作区，保留旧账本。 |
| 没有可用隔离，`run` | `拒绝启动：这台电脑没有可用的命令隔离环境（bubblewrap (bwrap) is not installed or this is not Linux (win32)）。` | 安装可用的 bubblewrap，或由所有者明确接受 none 后重签。 |
| 模型连不上，`ping` | `FAIL worker: URLError: <urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>` | 检查端点与服务；此例用本机未监听端口触发。 |
| Key 错，`ping` | `FAIL worker: HTTP 401: {"error":{"code":"401","message":"令牌已过期或验证不正确"}}` | 用故意无效的非秘密字符串在 GLM 端点触发；检查 Key 来源和权限，不输出 Key。 |
| Key 环境变量缺失，`ping` | `FAIL worker: environment variable T88_UNSET_KEY is not set` | 在运行终端设置所选变量，或改用现有 Key 文件路径并重签。 |
| 同一模型，`run` | `拒绝启动：Worker 和审查者是同一个模型（openai_compat:glm-5.3），不能自己审自己。请在 m2k.toml 给 [models.reviewer] 换一个模型。` | 换审查模型，或明确允许同源审查后重签。 |
| 无回执，`check-delivery` | `未经核验 hand-made: missing receipt file` | 这份产出没有可核验的回执，不能声称已验证。 |

## 安全须知与重来 / 卸载

Key 不进配置、任务、代码仓库、命令输出或日志；只存环境变量名或已有 Key 文件路径。
`keys/root.pem` 是测试 Root 私钥，拿到它的人就能签清单；`keys/` 整体不要上传、打印、复制到任务或提交。
部署目录（含账本、OBS 和签名密钥）必须在每个任务工作区之外，不要把整个部署目录作为 workspace。
同用户 Agent 仍能读取 OBS 私钥，回执不能补救这一点；参见 [POSITIONING 的边界](POSITIONING.md#3-不管什么--证明不了)。
只清理本手册的临时测试部署时，停止 live 和任务进程后，删除部署目录即可，账本也在其 `state/` 下。
若用过其他配置，默认账本在 Windows `%LOCALAPPDATA%/m2k/ledger.sqlite`，其他系统在
`$XDG_STATE_HOME/m2k/ledger.sqlite` 或 `~/.local/state/m2k/ledger.sqlite`；删除前确认它不属于其他任务。
重来必须用空目录；换 Root 必须同时换 OBS、账本，需保留历史时按 [README 换 Root](../README.md#换-root合法做法) 归档旧纪元。
代码目录可另行删除；若曾执行可选安装，再运行 `python -m pip uninstall m2-kernel`。
