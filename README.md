# M2 Kernel

> 公开仓库 `yyf-agent`，项目原名 m2-kernel；命令与 Python 包仍叫 `m2k`。许可证：MIT（见 [LICENSE](LICENSE)）。

m2k 是给 AI Agent 产出用的“审计与可核验层”：交付前跑所有者的验收和独立审查，只交付判 PASS 的冻结候选。
过程写进只追加、哈希链接、有 OBS 旁证回执和外部锚点的账本；交付后可凭回执核验文件有没有被改。
定位、边界与实验结论见 [docs/POSITIONING.md](docs/POSITIONING.md)，离线演示见 [examples/delivery_check/](examples/delivery_check/)。
本仓库与架构的对应关系和分阶段计划见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 三次实验一览

| 实验 | 问题 | 关键数字 | 判定 |
| --- | --- | --- | --- |
| [T82](tasks/T82-value-experiment.md) 价值对照 | 比跑完自己跑验收多拦下什么？ | 多拦下 0 次，确认误报 3 例；逐题 C/A 时间中位：Codex 2.21 倍、dsh 2.82 倍。 | 规则 2，调方向。 |
| [T84](tasks/T84-r0-scale-benchmark-run.md) 规模化基准 | 便宜模型加验证能否低成本追上贵模型？ | N=100 按隐藏测试完成 ① 89 / ② 72 / ③ 52；标价花费 $2.80 / $1.16 / $2.79。 | 三条规则全部不过。 |
| [T85](tasks/T85-r0-decisive-experiment.md) 决定性实验 | 看不到的验收加退回重做是否成立？ | N=100 按 T 完成 ① 93 / ②\* 88 / ③\* 84；规则 1 过、规则 2 不过。 | 放弃便宜模型规模化，回到审计 / 可核验层。 |

① 为 gpt-6-astra 单干，② 为 DeepSeek-V4-Flash 拆分不验证，③ 为 DeepSeek-V4-Flash + m2k；②\* 自测重做，③\* 所有者验收重做。
完整方法、预设规则、评分口径与限制见 [POSITIONING 的三次实验](docs/POSITIONING.md#5-三次实验)。

## 一条任务经过什么

```
提交契约 → Worker 循环（观察 → 思考 → G3 Gate → 行动 → 反馈）→ G4 冻结候选
        → TEST（验收命令）→ 独立 AUDIT（另一次模型调用，看不到 Worker 对话）
        → 判定 PASS / FAIL / UNKNOWN → 只有 PASS 才过 G6 交付 → G7 交付后核验
```

- 模型只产出“提案”（JSON），提案本身不产生任何效果；只有通过全部 Gate 的提案才会执行。
- 每一步都先写入只追加、哈希链接的账本（SQLite），预算用量从账本推导，无法被重置。
- FAIL = 已证实失败；UNKNOWN = 无法判定（审查回复无效、验收超时等），两者都不交付。
- 受保护文件（如验收脚本）Worker 写不了；即使绕道用命令改了，TEST 前的哈希核对也会判 FAIL。

## 快速开始（Windows / macOS / Linux，Python ≥ 3.11，唯一依赖 `cryptography`，用于 Ed25519 签名）

给别人部署看 [docs/SETUP.md](docs/SETUP.md)。
让 Agent 部署和接入自家 harness 看 [docs/AGENT_SETUP.md](docs/AGENT_SETUP.md)。

```bash
pip install -e .            # 或者不安装，直接用 python -m m2k
m2k demo                    # 离线演示：脚本模型跑完整链路，不需要 API
python -m unittest discover -s tests -t .   # 运行全部测试
```

账本默认保存在 Windows 的 `%LOCALAPPDATA%\m2k\ledger.sqlite`，其他系统的 `$XDG_STATE_HOME/m2k/ledger.sqlite`（未设置时为 `~/.local/state/m2k/ledger.sqlite`），必须位于任务工作区之外。

接真实模型：

在 `m2k.toml` 中，`[models.reviewer]` 默认必须使用与 `[models.worker]` 不同的模型，跨厂商审查更强。
如果只能使用同一个模型，可在 `[models.reviewer]` 显式设置 `allow_same_model = true`，审查者仍以新会话审查，看不到 Worker 对话；TEST_ONLY 和 PRODUCTION 均允许，配置变更须由所有者通过 `trust sign-release` 重签。
每次同源审查都会在启动时以黄色提示，写入账本，并在 `m2k status` 和 `m2k report` 中显示。

```bash
copy m2k.example.toml m2k.toml      # macOS/Linux 用 cp；按注释填 base_url / model
set OPENAI_API_KEY=...              # PowerShell: $env:OPENAI_API_KEY="..."
m2k trust init-dev --out keys       # 在任务工作区外生成测试链和 OBS；把打印的 [trust]、[obs] 表替换进 m2k.toml
m2k trust sign-release --root-key keys/root.pem --domain TEST_ONLY --code dev --slot dev --version 1 --out keys/release.json
m2k run examples/demo_calc/task.toml
m2k status calc-001                 # 汇总
m2k log calc-001                    # 账本事件
m2k check-ledger                    # 校验哈希链
m2k ping                            # 先确认模型能连通（花几个 token）
```

再跑同一个例子要改 task.toml 里的 `id`（calc-002…），任务 ID 不可复用。

## 事后核验演示

用脚本模型与真实本地 OBS 回执生成交付，再指出改文件、删文件、加文件、改回执及无回执目录；不需要 API Key，也不联网。
完整说明及 Windows 实际输出见 [演示 README](examples/delivery_check/README.md)。

```bash
python examples/delivery_check/make_demo.py
```

## 改需求：开新版本（T48）

任务已有最终 verdict 后，所有者可修改契约，用 `m2k run --amends <old-task-id> <new-contract.toml>` 开新版本。
新契约必须使用新的任务 ID 和新的空工作区；框架核对旧工作区仍与最后冻结的候选一致，再复制其中的文件，最后叠加新 seed，所有者文件优先。
预算是整个任务所有版本的总额，包含子任务及改派尝试的已用额度；要允许更多消耗，就在新契约中写更大的预算。
旧版本结论不会沿用，新版本重新验收、独立审查和裁决，通过后才能交付。版本链是线性的，每个版本最多只能有一个后继。
T48 之前运行的任务没有记录完整契约，不能用 `--amends` 续接。

**影响分析（T49）**：只有所有者编写的子任务能复用，模型规划的拆分全部重跑。按新契约顺序，只有前面的子任务全被复用，当前子任务才可能复用；首个重跑项之后全部重跑。
子任务自身内容（预算和改派次数除外）、父任务目标、约束、保护文件或验收超时改变时会重跑；旧版本未成功合并、来源子任务不能再裁决为 PASS、子任务验收文件或写入范围内的文件变化时也会重跑。
规则刻意保守：子任务验收可能读取任意文件，因此新 seed 相比旧版本最后候选的任何文件变化，只有“父任务验收文件且当前子任务不用”的情况允许复用；其他所有者文件改变或新增也会重跑。
复用项不运行、不预留预算，已用额度仍计入跨版本累计；修订版本只对重跑项求子任务预算之和，其余子任务规则仍全部检查。父任务自己的验收和最终独立审查始终重跑。
用 `m2k status <任务 ID>` 查看 `reused` 子任务 ID，用 `m2k log <任务 ID> --full` 查看提交事件 `amends.impact` 中的来源与重跑原因，以及按顺序记录的 `subtask.reused`。

## 挂起与所有者批准（T79）

原生 Worker 可用 `ask_owner` 提问或申请授权：`kind` 为 `question`、`scope`、`budget`，`text` 为 1–2000 字符的非空文字。
可选 `request` 只记录申请内容：范围用 `{"writes": ["src/"], "tools": ["run_command"], "network": true}`；预算用 `{"budget": {"max_model_calls": 40}}`。
外部 Agent 用相同格式写 `.m2k/ask.json` 后正常退出（文件最多 20000 字节；旧文件会在启动前删除）。请求本身不改变权限或预算。
请求会记为 `owner.request`；子任务提问会立即挂起父任务。顶层单任务的 Worker 预算用完也会挂起等待所有者，子任务和规划预算仍按原规则处理。
挂起时仍冻结候选并运行验收，判定 UNKNOWN，不再审查、修复或交付；`m2k run` 退出码为 **3**。用 `m2k status <任务 ID>` 查看 `awaiting` 请求和后继 `answered_by`。

所有者写好新 ID、新空工作区和所需权限/总预算的新契约，然后在交互终端执行：
```
m2k answer <旧任务 ID> <新契约.toml> --text "回答或补充说明"
```
命令展示待答请求、范围和预算增加，必须亲手输入 `YES` 才开新版本；其他输入取消。`--text` 可省略，提供时会作为 `Owner's answer: …` 写入新契约约束，Worker 和审查者均可见，并使所有子任务重新运行。
处理待答请求、扩大写入范围、增加工具、开启联网或提高任一总预算上限都需要此批准；普通 `run --amends` 会拒绝这些变更。框架记下 `amends.approval`，预算增加另记带 OBS 回执的 `budget.raised`（配置了 OBS 时）；有效预算仍受授权书 caps 限制。
审批和后继检查在同一提交事务中核对；情况变化要重新确认。只读裁决器重新计算审批需求并核对预算追加及其旁证。
当前是隔离前的本机验证：以同一 Windows 用户运行、专门攻击框架的 Agent 仍可能伪造批准；只有所有者能写的 inbox 留待隔离后实现。

## 预授权（T80）

所有者可在任务契约中预先列出可追加的权限。只适用于没有子任务、没有 `plan` 的顶层任务；没有 `[preauth]` 的契约保持原有行为、摘要与契约记账格式。

```toml
[task]
# 其余任务字段照常填写；含 [preauth] 时可用 writes 限定初始写范围。
writes = ["src/"]

[preauth]
writes = ["tests/", "output.txt"]
tools = ["run_command"]
network = false
budget_percent = 20
```

`writes` 是可额外写的相对路径列表，目录路径以 `/` 结尾；已经覆盖的路径无需追加，当前写范围为空则整个工作区可写。
`tools` 只能列框架的 `DEFAULT_TOOLS` 工具名；`network` 默认为 false；可选 `budget_percent` 必须是 1–100 的整数。
这些键都可省略；未知键、错误类型、越出工作区的路径，以及与子任务或规划组合使用都会拒绝。

原生 Worker 的 `ask_owner` 范围或预算请求全部落在预授权内时，框架记带 OBS 回执的 `authority.granted`（配置 OBS 时）、重签凭证，并在同一对话继续。
请求包含任何未授权内容，或是一般问题，仍按 T79 以待授权结束。放行额度核对和记账在同一写事务中进行；凭证签发或写账被拒绝时，当前权限与凭证保持原样。
Worker 耗尽模型调用、轮数或 token 预算时，有余量则由框架申请该项允许的最大上限；仍须服从授权书 caps，保护文件与验收文件也继续受保护。

每项预算的自动追加总量，在同一任务所有版本中累计，不超过**第一版所有者预算 × 百分比向下取整**。
例如第一版 100 000 tokens、20%，所有版本自动追加合计最多 20 000 tokens；下一版本不会继承上一版本临时放宽的权限或预算。

外部 Agent 只自动放行写路径与 `max_tokens` 请求；工具和联网请求仍待所有者批准。
放行后在同一工作区重新启动 Agent，最多三次，**不保留对话**；所有启动的已知 token 消耗累计，退出后检测到已经超支不会再追加。
目录观察器跨重启保留基线。`m2k run` 打印每次放行，`m2k status` 的 `granted` 列出序号、申请方和字段变化；只读裁决器逐条重放并检查最终契约与凭证。
后续版本扩大 `[preauth]` 本身也必须经 `m2k answer` 批准。

## 实时观看运行（m2k live）

在 `m2k run` 运行时，另开终端执行 `m2k --config m2k.toml live --task calc-001`；运行结束后同样可以回放。
也可用 `--ledger <账本路径>` 指定账本，`--port 8765` 指定端口，`--no-browser` 只打印本地地址。
它只读取账本；账本尚未创建时会等待。页面默认实时跟随最新的任务，也可以选任务回放（0.5×–8×），点账本事件看原始内容；没有连上服务时只能看“演示剧本”。
动物表示模型：鲸鱼 DeepSeek/DSH、螃蟹 Claude、龙 Codex/GPT/OpenAI、狐狸 GLM，其他模型为机器人。
11 盏灯依次表示配置与契约、信任链、发行清单、OBS 启动、锚点核对、框架指纹、授权书、Root 记忆、命令隔离、审查独立、Worker 试启动；外部 Agent 的内部过程对框架不可见。
“重新裁决”会在临时账本副本上重新运行只读裁决器，用于发现运行结束后追加的无旁证事件，结果只供显示。

## 框架观测外部 Agent（T73）

外部 Agent 的 `[models.worker] kind = "cli"` 默认每 5 秒由框架检查工作区的文件大小和修改时间；
可用 `watch_s` 调整间隔，`0` 关闭。变化写为 `workspace.changed`，退出、失败或超时后还会比较一次，
`m2k live` 可以实时观看。这些是过程观测，没有 OBS 回执；最终留下什么仍以冻结候选为准。

所有者可在配置中加入 `[watch] paths = ["~/.codex/config.toml", "~/.claude/settings.json"]`。
路径必须已存在；相对路径以配置所在目录为准，`~` 会展开。框架在每次 Worker 运行前后比较内容哈希，
包括修复和子任务尝试，符号链接及 junction 只记录链接本身。变化写为 `outside.changed`（配置 OBS 时有回执），
跳过验收并判 FAIL；无法完整核验则验收结果为 UNKNOWN，均不交付，也不提供修复。只读裁决器同样拒绝这些变化。
监视范围不能与工作区、账本、OBS 密钥和日志目录、锚点文件或仓库重叠，总文件数最多 20 000。

请选择 Agent 的配置文件等窄路径，别监视整个主目录：Codex 和 Claude Code 每次运行都会在那里写会话日志，
整目录监视会导致每个任务都失败。新增或修改 `[watch]` 后需要重签发行清单。
监视只揭示改动，不能阻止改动；读取机密和网络上传仍不可见，清单外的文件变化也不在核验范围内。

## Windows 账号与目录隔离（m2k isolation）

在配置中加入 [m2k.example.toml](m2k.example.toml) 的 `[isolation]` 表，然后运行：

```text
m2k --config m2k.toml isolation check
```

命令用 Windows Authz 查询账号及其组的有效读、写、执行权限，逐项打印中文表格。
检查两个账号存在且不是管理员、框架私有目录不可被 Agent 读取、代码/配置/工具不可被 Agent 写入，
以及服务账号、Agent 和当前用户所需的访问权限。`code_dir` 默认指包含 `m2k` 包的文件夹；当前用户用自身 SID 检查，
不借助 Administrators 组授权。账号尚未创建时也可运行：相关检查不通过，另列 Users、Authenticated Users、
Everyone 在私有目录上的读写信息，信息行不影响退出码。

`svc_user`、`agent_user`、`data_dir`、`tools_dir`、`protected` 必填；`agent_read`、`agent_write` 为可选的主动授权。
路径相对于配置文件目录解析，主动授权不得等于或位于 `data_dir` 或任一 `protected` 目录内。
路径不存在时会注明：要求访问的检查不通过，要求禁止访问的检查通过；无法查询权限时不会当作已隔离。
全部检查通过退出 0，任一检查不通过退出 1，配置错误或非 Windows 退出 2。
`check` 不创建账号、不修改权限/所有者/文件、不打开账本。

管理员终端可以执行以下命令；每次先列出计划，精确输入 `YES` 才更改：

```text
m2k --config m2k.toml isolation install
m2k --config m2k.toml isolation grant
m2k --config m2k.toml isolation revoke
m2k --config m2k.toml isolation uninstall
```

`install` 在任何账号或管理目录更改前，先用 `icacls /save` 备份已有目录，并生成独立还原脚本。
新账号密码由管理员隐藏输入两遍，至少 12 位，最多尝试三次；不会写入命令行、日志或文件。
已有账号只核验不是管理员，不改密码。`owner_user` 默认是运行命令的用户，受保护目录保留其完全控制；
`data_dir` 只给 Administrators、SYSTEM、服务账号完全控制，所有者只在 `ledger` 有读取/执行、`queue/inbox` 有修改权限。
`code_readers` 只增加代码读取/执行权限，不能包含服务或 Agent 账号；代码目录可以用 `code_dir` 明确指定，
从 worktree 运行时也应指向要管理的实际代码目录。备份默认存入 `C:\m2k-acl-backup`，可通过 `backup_root` 指定。
管理目录必须在 NTFS；系统目录、用户个人目录根、盘符根、备份目录及相互嵌套的管理目录会被拒绝。

`grant` 添加 `agent_read` 的读取/执行与 `agent_write` 的修改权限，不能授权到私有、代码、工具或备份目录内部。
`revoke` 同时处理当前配置和 `grants.jsonl` 记录过的路径，移除 Agent 的显式授权。
两者之后都显示 `check`；撤销后配置仍列出主动授权时，该项可能不通过并退出 1。继承或其他组原有权限仍由 `check` 如实反映。
退出 0 表示操作与后续检查均通过，1 表示取消、操作失败或检查未通过，2 表示配置/预检拒绝，未执行更改。

如何复原：在管理员 PowerShell 中运行安装输出的完整命令，例如：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\m2k-acl-backup\20261027-120000\restore-acl.ps1'
```

脚本先列出目录并检查备份文件，要求 `YES` 后逐项还原；之后单独询问删除本次创建的账号，输入 `DELETE` 才删除，直接回车保留。
`uninstall` 会选择最新的首次安装备份并运行同一脚本，由脚本询问 `YES`。
若安装中途失败，更改可能只完成一部分，请使用输出的还原命令。
还原恢复已保存的 ACL，安装后新建的文件继续继承恢复后的父目录权限；不会删除目录或文件，原先不存在的 data/tools 目录需手动移除。
删除账号不会移除 `C:\Users\<账号>` 个人目录。备份只保存 ACL、配置路径和操作日志，不含密钥或密码；隔离安装期间应一直保留。

管理员可以绕过 ACL，`owner_user` 按设计受信任。ACL 只约束文件，不能覆盖网络访问或其他进程；
本卡不启动其他账号的进程、不保存密码。进程与 Job Object 属于 T77，接入 `m2k run` 属于 T68。
检查报告只反映运行当时的权限状态。

## 信任链

`m2k run` 默认要求信任链。在任务工作区之外用一个命令生成完整的 TEST_ONLY 链，
把打印的 `[trust]`、`[obs]` 表替换进 `m2k.toml`；输出目录必须不存在或为空，不会覆盖已有文件。
配置 `[trust]` 必须有 OBS；已有链可用 `m2k obs keygen --out <dir>` 生成旁证配置，加入后重签发行清单：

```text
m2k trust init-dev --out keys
# 把打印的 [trust]、[obs] 表加入 m2k.toml，完成全部配置，再签发行清单：
m2k trust sign-release --root-key keys/root.pem --domain TEST_ONLY --code dev --slot dev --version 1 --out keys/release.json
m2k trust status
```

单独的 `keygen` / `sign-charter` / `sign-revocations` / `sign-release` 命令仍然可用。正式 Root 只能在离线 USB 环境生成和使用，绝不由本命令生成，也不存放在这台电脑上。

`[trust] release` 必须指向 Root 签名的发行清单。Root 签名就是所有者对代码和配置的确认；启动时先核对，失败只在 `m2k.startup` 下记一条 `startup.refused`，不占用任务 ID、不调用模型或试启动 Worker。
清单固定配置文件的 SHA-256（CRLF 统一为 LF，其余字节完全一致）。TEST_ONLY 的代码可用 `--code dev`；省略该参数就固定当前代码摘要。PRODUCTION 必须固定代码与配置，用真 Root 离线执行 `sign-release --domain PRODUCTION`，不允许 `--code dev`。
发行清单按 slot 独立递增版本；PRODUCTION 必须配置 `[trust] revocations`。要回到旧配置，请用更高的 `--version` 重签旧配置内容。
改配置后，先把 `[trust] release` 指向一个新文件，再用全局 `--config` 指定最终配置并执行 `trust sign-release`；签名命令不会覆盖已有输出。自动递增会读取配置当前指向的有效同 slot 清单；首次签发或改指向新文件时须显式给出 `--version`。试启动前的框架指纹同时包含配置与清单，任务中途被替换也会停止。

正式签发时先断开网络，关闭所有代理及相关进程，运行 `python tools/sign_preflight.py`：先处理所有 FAIL，并人工确认 LOOK 中列出的计划任务和启动项，再插入 USB 密钥；用 `m2k trust sign-charter`（或 `m2k trust sign-revocations`）配合 `--root-key H:\...` 签发，拔出密钥后再恢复网络。检查脚本只读取和列出信息，不会自动关闭进程或修改配置。

仓库里的 `trust/test/` 是一条真实可用的 TEST_ONLY 链（T19）：`root-pin.json` 是旧项目的测试根公钥（指纹 `6357ec2addf1fd69`），`charter.json` 由这把测试根在所有者电脑上签发（release `dev`），`issuer-pin.json` 是框架签发者的公钥，`caps.toml` 是授权书上限。签发者私钥只在所有者电脑和 GitHub secret `M2K_TEST_ISSUER_KEY` 里；`m2k-task` 始终带着 `[trust]` 跑任务，缺少这个 secret 就失败。这条链只用于测试。正式运行需要一份新的授权书：用真正的 Root 离线签发（先跑 `sign_preflight`，再插 U 盘，执行 `sign-charter --domain PRODUCTION`，release 为当前代码的摘要）。

有子任务时，框架在每个子任务开始时给它签一张自己的子凭证：工具、预算、联网、写入范围和有效期都不能超过父凭证，也必须与父任务方案里这一项完全一致。每个子任务动作前都会验整条链 Root → 授权书 → 父凭证 → 子凭证 → 子任务契约；撤销父凭证，所有子任务一起停，撤销某个子凭证，只停那一个。

`trust status` 校验章程、代码版本、撤销名单、发行者密钥及发行清单，显示 `release manifest: ok / code changed / config changed / missing`，清单未通过时退出非零。`m2k-task` 使用已提交的 `trust/test/m2k-task.toml` 和同一 TEST_ONLY Root 签署的 `trust/test/release.json`，启动前先检查。配置了 `[trust]` 后，`m2k run` 提交任务时由框架签发任务凭证（不超过授权书上限），每个动作执行前、验收前、交付前都重新验证整条链；凭证中途被撤销或过期，后续动作全部被拒、不交付。没有 `[trust]` 时拒绝启动。

所有者可逐次明确使用 `m2k run --untrusted <task>` 无信任链运行：账本记为 `trust: null`，终端警告且 HTML 报告顶部显示红色未授权标识；已配置 `[trust]` 时不能同时使用该参数。

## 换 Root（合法做法）

OBS 按信任域记住首次见到的 Root；同一 OBS 下换 Root 会以 `ROOT_CHANGED` 拒绝启动。每次正常启动和 `m2k trust status` 都以粗体显示 Root 指纹；status 还显示 `obs root: ok / not yet recorded / changed`，发现 changed 时退出非零。

换 Root 必须开启新的信任纪元：

1. 用 `m2k obs keygen --out <新目录>` 生成新的 OBS 密钥，配置新的 OBS 日志路径。
2. 将配置指向**新的账本文件**；把旧账本、旧 OBS 日志改名归档，保留历史，绝不删除。配置了本地锚点文件时，也为新纪元选择一个新文件。
3. 由新 Root 签署授权书、撤销名单及各 slot 的发行清单：`m2k --config <配置> trust sign-release --root-key <新Root.pem> --domain <域> --slot <slot> --version 1 --out <新清单.json>`。先完成配置并指向新清单，再签署；TEST_ONLY 可加 `--code dev`，PRODUCTION 按离线签发流程固定代码。
4. 新账本会自动得到自己的远端锚点文件 `anchors/<新ledger_id前12位>.jsonl`；旧锚点文件留在分支上。

必须同时更换 OBS 和账本：旧 OBS 日志记住旧 Root（`ROOT_CHANGED`），旧账本的锚点带着旧旁证的指纹（`notary changed`）。仅删除或更换 OBS 日志会丢失 Root 记忆；已发布锚点可报 `OBS log replaced or truncated`，TEST_ONLY 警告后继续，PRODUCTION 拒绝。Root、OBS 和锚点仓库一起被替换仍需靠隔离解决。

## 外部锚点

配置 `[anchor] file = "../m2k-state/anchors.jsonl"`、`repo_dir = "../m2k-anchors"`（相对配置文件目录，都放在所有任务工作区之外）。
有 OBS 的 `m2k run` 在 Gate 裁决后、交付前追加 OBS 签名的 v2 检查点，并发布到专用克隆的远端；旧顶层 `anchor_file` 和手动 `m2k anchor` 的未签名 v1 记录仍兼容。
`m2k check-ledger` 自动对照配置的锚点，或用 `--anchors FILE` 指定，指出已锚定位置的改写或截断。
锚点各行的 `prev` 是上一行原始 UTF-8 字节（含 `\n`）的 SHA-256。它证明账本仍匹配已保存的锚点；
已发布的检查点可发现账本和 OBS 日志一起改写或删除重建；同用户 Agent 可以使用 git 凭据，分支强推或删除由 GitHub 活动记录暴露。

用 `git clone -b ledger-anchors <仓库地址> <专用克隆目录>` 创建专用克隆；Windows 设置 `git -C <专用克隆目录> config core.autocrlf false`。
随后运行 `python tools/anchor_push.py --anchors <锚点文件> --repo-dir <专用克隆目录> --push`，
仅接受已发布文件的完整字节前缀扩展，提交到 `ledger-anchors` 分支。
`m2k check-ledger --remote` 对照已发布文件、签名及同一 OBS 密钥的所有历史回执位置，并查询分支强推/删除活动；API 不可达也报错。
PRODUCTION 必须配置 `repo_dir`，启动核对失败即拒绝，发布失败不交付且报告 UNKNOWN；TEST_ONLY 警告并记账后继续。
一次性 CI 可用 `check-ledger --remote --repo-dir <克隆> --this-ledger-only` 跳过跨账本主线核对；`m2k run` 启动始终全量核对。

## 任务契约（task.toml）

| 字段 | 含义 |
| --- | --- |
| `[task] id / goal` | 唯一 ID 与目标描述 |
| `workspace` | Worker 唯一能读写的目录，可用 `{task_id}` |
| `seed` | 开始前复制进（空的）工作区的初始文件 |
| `protected` | Worker 不能修改的文件，如验收脚本 |
| `tools` | 允许的工具子集：list_files / read_file / write_file / run_command |
| `constraints` | 给 Worker 和审查者看的约束 |
| `max_repairs` | 单任务失败后的最多修复次数：整数 0–5，默认 0；各轮共用累计预算 |
| `deliver_to` | PASS 后交付到 `<deliver_to>/<task_id>/`，须与工作区互不包含；旁边的 `<task_id>.m2k-receipt.json` 是 OBS 签名回执 |
| `[acceptance] command` | 验收命令，退出码 0 为通过；`{python}` 代表当前解释器 |
| `[acceptance] files` | 任务方预先提供的验收文件（工作区相对路径，必填且非空），自动保护，Worker 不可修改 |
| `[budget]` | max_rounds / max_model_calls / max_tokens / max_commands 硬上限 |

`m2k check-delivery <dir>` 用当前账本、OBS 日志和信任配置全面核验；`m2k check-delivery <dir> --standalone --public-key <B64>` 仅凭公钥核验可搬移的回执与文件。
没有匹配有效回执的产出均为“未经核验”。

子任务（第 3 阶段）：契约里写 `[[subtasks]]`，内核按顺序逐个执行。每个子任务有自己的工作区副本、写入范围、验收和独立审查；
只有 PASS 的子任务才把写入范围内的改动并回父任务。全部并回后，父任务再跑总验收和审查，通过才交付。
子任务的验收只能用父任务 `[acceptance] files` 里的文件，预算总和不能超过父任务。

召回与改派（T24）：任务方可以随时召回正在执行的子任务（`m2k recall <子任务 ID> --reason ...`，在另一个终端执行也行），
它的下一个动作就会被拒，结果保留在账本里但不采用。子任务写了 `max_reassign = 1..3` 时，失败或被召回后会改派给接手者：
接手者从父任务工作区的新副本开始，目标里附上账本记录的前任事实（做过什么、验收输出、审查理由，不含前任的自述），
预算只有方案里剩下的部分；交回和前任一模一样的失败结果，直接判 `REPEATED_FAILURE`。

由模型拆任务（T27）：`[task] plan = "model"`，并在 `[acceptance] parts` 里写明每个验收文件对应的检查命令。
规划模型（就是 Worker 模型）只提方案：选哪些文件、写入范围、预算、用你给的哪些检查，自己不能写验收命令。
方案要先过和手写方案同样的规则，加上：最多 6 个子任务、只能选 `parts` 里的检查、预算要给规划和最终审查留出余量；
不合规会被拒，最多再给一次机会，仍不合规就 FAIL，什么都不执行。通过的方案记进账本后自动执行。

```toml
[task]
plan = "model"
[acceptance]
command = "{python} check_all.py"
files = ["check_all.py", "check_a.py", "check_b.py"]
parts = { "check_a.py" = "{python} check_a.py", "check_b.py" = "{python} check_b.py" }
```

```toml
[[subtasks]]
id = "parser"
goal = "实现 src/parser.py"
writes = ["src/parser.py", "src/parser_util/"]   # 以 / 结尾表示整个目录
acceptance = "{python} -m unittest tests/test_parser.py"
acceptance_files = ["tests/test_parser.py"]
budget = { max_rounds = 6, max_model_calls = 8, max_commands = 4 }
```

## 项目状态

已封版（2026-10-31），本卡之后停止开发；原分阶段计划中未完成的部分全部冻结。
冻结方向与重启条件见 [POSITIONING 的项目状态](docs/POSITIONING.md#7-项目状态)。

公开仓库只带不用任何 secrets 的 `.github/workflows/ci.yml`（单元测试 + 离线验收 + 任务卡范围检查）。
`docs/WORKFLOW.md` 里的 GLM 审查、`m2k-task`、`model-ping`、`real-accept`、`main-guard` 等工作流属于原私有开发仓库，公开版未包含；
任务卡里的本机路径已换成 `<repo>`、`<worktrees>`、`<dev-keys>`、`<python>` 这类占位符。

命令隔离（T08）：Linux 上每条命令（Worker 的命令和验收命令）都在 bubblewrap 里执行：只有工作区可写，
系统运行库只读，看不到家目录、`/root`、账本和其他进程，默认断网（任务契约 `[task] network = true` 才联网）。
Windows / macOS 没有隔离后端：`m2k run` 会拒绝启动，请在装有 bubblewrap 的 Linux 上运行（原私有开发仓库用 GitHub 工作流 `m2k-task`，公开版未包含），
或在 `m2k.toml` 写 `[sandbox] backend = "none"` 表示接受不隔离的风险（会记入账本）。不要在不隔离时运行不可信任务。
