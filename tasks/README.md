# 任务卡

流程：审阅者写卡 → 实现者在卡指定的分支上完成并推送、在卡底部写交接说明 → 审阅者拉分支、看 diff、跑测试，
在卡里写审阅结论（PASS 合并 / 打回并列出问题）。

一张卡只做一件事。卡里没写的文件不改，没要求的文档不写。规则见根目录 `AGENTS.md`。

| 卡 | 标题 | 执行者 | 状态 |
| --- | --- | --- | --- |
| [T01](T01-edit-file-tool.md) | edit_file 工具（精确替换） | Codex | DONE (#1) |
| [T02](T02-html-report.md) | `m2k report` 任务时间线网页 | Codex | DONE (#2) |
| [T03](T03-tools-tests.md) | tools.py 单元测试 | DeepSeek（经 m2k） | DONE (#4) |
| [T04](T04-key-guard.md) | Key 防护：命令拿不到 API Key；GitHub 上跑 m2k 任务 | Claude | DONE (#3) |
| [T05](T05-review-injection.md) | 审查防注入：Worker 写的文字指挥不了审查者 | Codex | DONE (#7) |
| [T06](T06-owner-acceptance.md) | 验收必须由任务方提供；防同名模块劫持验收 | Codex | DONE (#6) |
| [T07](T07-ledger-outside-workspace.md) | 本地运行账本移出工作区 | Codex | DONE (#5) |
| [T08](T08-command-sandbox.md) | 命令隔离执行，默认断网（Linux bubblewrap） | Claude | DONE (#12) |
| [T09](T09-ledger-anchor.md) | 账本链头外部锚定（GitHub 上的只追加锚点） | Codex | DONE (#10) |
| [T10](T10-bytecode-shadows.md) | 字节码劫持验收（.pyc/.so/.pyd 冒充模块、伪造 __pycache__）；T05 审查遗留两处 | Codex（Claude 代提交） | DONE (#8) |
| [T11](T11-model-connection-errors.md) | 模型连接中断记为 UNKNOWN，不崩溃（GLM 审查两次无声失败） | Codex | DONE (#9) |
| [T12](T12-anchor-attack-drill.md) | 攻击演练：改写账本并重算整条哈希链 → 普通校验发现不了，外部锚点发现 | DeepSeek（经 m2k） | DONE (#11) |
| [T13](T13-review-streaming.md) | GLM PR 审查改为流式回复，长审查不再被掐断（T08 等它） | Codex | DONE (#13) |
| [T14](T14-trust-core.md) | 第 2 阶段：签名信封与信任链核心库（Root → 授权书 → 凭证 → 契约） | Claude（GLM 必审） | DONE (#14) |
| [T15](T15-trust-cli.md) | `m2k trust` 命令与 `[trust]` 配置：生成测试密钥、签授权书与撤销单、查看与校验 | Codex | DONE (#15) |
| [T16](T16-trust-in-kernel.md) | 接入内核：提交时由框架签发任务凭证；G0/G1/G2 每个动作前重新验整条链 | Claude（GLM 必审） | DONE (#16) |
| [T17](T17-trust-attack-drill.md) | 攻击演练：伪造、扩权、挪用凭证，改契约，测试根冒充正式，替换公钥或授权书 → 全部被拒 | DeepSeek（经 m2k） | DONE (#17) |
| [T18](T18-sign-preflight.md) | 离线签发前的只读自检脚本（断网、无 Agent 进程、计划任务与启动项、U 盘未插） | Codex | DONE (#18) |
| [T19](T19-trust-e2e.md) | 端到端演练：用旧测试根（指纹 6357ec2addf1fd69）签授权书，在 GitHub 上跑一次真任务 | Codex（A）+ 用户（B）+ Claude（C） | DONE (#19) |
| [T20](T20-trust-required.md) | 第 2 阶段收尾：没有信任链就拒绝运行（`--untrusted` 逐次显式放行并标红）；`m2k trust init-dev` 一步生成测试链 | Codex | DONE (#20) |
| [T21](T21-task-tree.md) | 第 3 阶段：任务树骨架（契约写明子任务；子任务各自工作区、写入范围、验收与审查；通过后只并回范围内文件；父任务总验收） | Claude（GLM 必审） | DONE (#21) |
| [T22](T22-child-credentials.md) | 子凭证委派：每个子任务有自己的凭证，不能宽于父凭证和方案；验子凭证连同父凭证一起验 | Claude（GLM 必审） | DONE (#22) |
| [T23](T23-budget-reservation.md) | 预算原子预留：子任务开工前在同一事务里从父预算预留，两个同时抢最后一份只有一个成功；结束后释放，只算实际用量 | Codex | DONE (#24) |
| [T24](T24-recall-reassign.md) | 召回与改派：`m2k recall` 撤回子任务权限，之后动作全被拒、结果不采用；改派的接手者凭账本事实开工，预算只用剩下的，不许交回同样的失败 | Claude（GLM 必审） | DONE (#25) |
| [T25](T25-bounded-repair.md) | 有界修复：验收失败或审查不通过后，凭账本里的失败事实修复，次数有上限，预算不重置；作弊不给修 | Codex | DONE (#26) |
| [T26](T26-author-not-reviewer.md) | 强制作者不能审自己：Worker 和审查者是同一个模型就拒绝启动 | Codex | DONE (#23) |
| [T27](T27-model-plans.md) | 模型拆任务：`plan = "model"`，规划模型只能选任务方的检查、不能自己写验收；方案过 Gate 才执行，不合规给一次重试 | Claude（GLM 必审） | DONE (#27) |
| [T28](T28-stage3-attack-drill.md) | 第 3 阶段攻击演练：20 种攻击（子任务越权、召回后动手、伪造交接、抢预算、规划模型越权或塞指令、作弊后求修复等）全部被拒或被发现 | 大肥鱼（DeepSeek，经 m2k） | DONE (#33) |
| [T29](T29-stage3-e2e.md) | 第 3 阶段端到端实战：模型自己把真任务（记账报表小工具）拆成 2–3 个子任务，大肥鱼分头完成，GLM 逐个审查后合并，总验收通过才交付 | 大肥鱼（经 m2k） | DONE (#36) |
| [T30](T30-small-defensive-fixes.md) | 审查中记下的 10 处防御小修补：配置写错报格式错误而非崩溃、Python 装在 / 时拒绝隔离、召回未知任务给明确提示等 | Codex | DONE (#28) |
| [T31](T31-ci-minutes.md) | CI 省分钟：PR 只跑 Linux，合并到 main 后再跑 Windows；去掉重复运行；只改任务卡和文档时不跑测试 | Claude | DONE (#29) |
| [T32](T32-untrusted-context.md) | 模型写的文字（规划模型写的子任务目标、前任事实）单独放进 `context`，给审查者看时用暗号隔离，不再混进任务方的目标 | Claude（GLM 必审） | DONE (#31) |
| [T33](T33-review-on-label.md) | GLM 审查只在贴上 `glm-review` 标签时跑一次，推送不再重复审；推理强度不变 | Claude | DONE (#30) |
| [T34](T34-dsml-tool-calls.md) | Worker 能读懂 DeepSeek 自带的工具调用格式（DSML），当作同样的提议过 Gate；审查者仍只认 JSON，DSML 审查结论为 UNKNOWN | Claude（GLM 必审） | DONE (#32) |
| [T35](T35-windows-ci-minutes.md) | CI 再省分钟：合并到 main 只在改了代码时才跑；Windows 只测 Python 3.13（Windows 分钟按两倍计费） | Claude | DONE (#34) |
| [T36](T36-model-busy-retry.md) | 模型服务繁忙（429/5xx）时等一会儿再发，最多两次；拆任务时模型出错记为 MODEL_ERROR，不再误记成“方案被拒” | Claude（GLM 必审） | DONE (#35) |
| [T37](T37-cli-worker-codex.md) | 首个适配器：别家的 Agent 命令行（先接 Codex）当 Worker；M2 治理它的结论：开工前验链、不给 M2 密钥、事后冻结、任务方验收、GLM 跨厂商审查、单次交付 | Claude（GLM 必审）+ 用户在 Windows 真跑 | DONE (#37) |
| [T38](T38-agent-probe-claude-code.md) | 开工前先试启动一次 Agent 命令（参数错、找不到程序、没登录当场报出，不进账本）；接入第二家：Claude Code | Claude（GLM 必审）+ 用户经 Codex 桌面端真跑 | DONE (#38) |
| [T39](T39-read-only-adjudicator.md) | 第 5 阶段①：只读裁决器——只凭账本重算结论；Gate 只在裁决 PASS 时交付；内核自己报的 PASS 只是待核对的说法 | Claude（GLM 必审） | DONE (#39) |
| [T40](T40-framework-fingerprint.md) | 第 5 阶段②：框架指纹——代码、配置、信任链文件的指纹写进账本和凭证，任务中途被改立即中断、不交付 | Codex | DONE (#41) |
| [T41](T41-deepseek-harness.md) | 第三家：DeepSeek Harness（提示词走参数、用量未知如实记、shell 无沙箱如实声明）；试启动报错说人话 | Codex + 用户经 Codex 桌面端真跑 | DONE (#40) |
| [T42](T42-obs-witness.md) | 第 5 阶段③：独立 OBS 旁证——关键事件都要另一进程用自己的密钥签回执；Gate 按配置里的 OBS 公钥核对，伪造的账本尾巴拿不到回执即 FAIL，OBS 中断即 UNKNOWN | Codex（GLM 必审） | DONE (#42) |
| [T45](T45-obs-cleanup.md) | OBS 收尾（先于 T43）：账本缓存 ledger_id，不再每张回执全表读账本（实测 5 万事件时 OBS 多花 2.8 秒）；OBS 失败只记一条 UNKNOWN 判定；cmd_run 报错收窄；obs.py 展开 | Codex（GLM 必审） | DONE (#43) |
| [T43](T43-release-manifest.md) | 第 5 阶段④：启动自检——代码和配置文件必须对上 Root 签名的发行清单（测试域代码可写 dev，配置永远要对）；改配置 = 所有者用 Root 重签清单；对不上拒绝启动并记账；框架指纹提前到试启动前 | Codex（GLM 必审） | DONE (#44) |
| [T44](T44-obs-required.md) | 第 5 阶段⑤：OBS 必需；回执与 OBS 日志双向对账；`m2k run` 的显示与退出码以 Gate/裁决器为准（没交付就不报 PASS）；子任务合并前也要裁决 PASS | Codex（GLM 必审） | DONE (#45) |
| [T63](T63-external-anchor.md) | 第 5 阶段⑥：OBS 签名的检查点（账本链头 + OBS 链头）发布到 `ledger-anchors` 分支；远端核对（以 OBS 日志为主线）+ 查强推/删分支；PRODUCTION 锚定失败不交付、TEST_ONLY 记账警告；旁证启动只验链 + 末行签名 | Codex（GLM 必审） | DONE (#46) |
| [T64](T64-release-rollback.md) | 第 5 阶段⑦：清单带 slot + version，每份配置一条线；OBS 拒绝版本回退和吊销单回退（两个信任域都拒绝启动）；清单可吊销；裁决器核对运行时的清单 | Codex（GLM 必审） | DONE (#47) |
| [T47](T47-delivery-receipt.md) | 第 5 阶段⑧：交付回执——先暂存、记 OBS 签名回执、锚定后才放出；交付目录旁写便携回执；`m2k check-delivery` 把没有回执或对不上的产出一律标为未经核验；`deliver_to` 不能与工作区重叠；CI 发布路径按 Gate 同等裁决 | Codex（GLM 必审） | DONE (#48) |
| [T46](T46-stage5-attack-drill.md) | 第 5 阶段攻击演练：21 种攻击框架本身（改框架代码、换审查者、换 Root 或授权书、回滚配置与吊销单、伪造回执、假 PASS、删账本与 OBS 日志重来、改锚点历史、绕过 Gate 放文件、伪造便携回执、整套换 Root 等）全部被拒或被发现；4 个离线验收场景 | 大肥鱼（DeepSeek，经 m2k） | DONE (#49) |
| [T65](T65-root-pin.md) | 第 5 阶段收尾：OBS 记住每个信任域首次见到的 Root，换 Root 即拒绝启动（`ROOT_CHANGED`）；远端核对报出本账本锚点的公证方变更；每次运行和 `trust status` 醒目显示 Root 指纹；T46 的 a21 改为必须通过 | Codex（GLM 必审） | DONE (#50) |
| [T69](T69-small-fixes-2.md) | 锚点发布先同步远端再推送（修本机推送被拒“非快进”）；git 报错保留原文；8 处防御小修补（自有锚点畸形记录、换 Root 签的授权书拒绝启动并记账、OBS 拒绝码、`trust status` 标签、交付改名失败清理等） | Codex（GLM 必审） | DONE (#51) |
| [T70](T70-small-fixes-3.md) | 零散小修补（续）：card-scope 能读折行的 Files 列表；git 一律用 C 语言环境（“非快进”重试不受本地化影响）；远端还没有锚点分支时也能首次发布；`[anchor]` 写错时报清楚的配置错误；裁决器按 seq 直接取 `release.checked` | Codex（GLM 必审） | DONE (#52) |
| [T71](T71-live-view.md) | `m2k live`：只读、实时的运行动画网页——流水线节点和关卡亮灯、小圆点沿流程移动；Agent 和模型是像素动物（DeepSeek 鲸鱼、Claude 螃蟹、GPT/Codex 小龙人、GLM 狐狸）；实时跟随和回放共用一页 | Codex（GLM 必审）+ 用户看效果 | 已合并（#54）：`m2k live` 实时跟随与回放，画面为定稿动画 |
| [T72](T72-same-model-review.md) | 允许 Worker 和审查者用同一个模型：配置里显式写 `allow_same_model = true` 才行（受 Root 签名保护），以新会话独立审查；每次记账为“同源审查”、启动时黄字提示、报告里标出 | Codex（GLM 必审） | 已合并（#53） |
| [T74](T74-ci-one-python.md) | CI 每次只跑一个 Python：PR 用 Linux 3.13、main 用 Windows 3.13，同一 job 加 3.11 语法检查以节省 Actions 分钟 | Codex（GLM 必审） | 已合并（#55） |
| [T75](T75-isolation-check.md) | `m2k isolation check`：只读检查两个 Windows 账号的有效权限，账号未创建时补充内置组信息 | Codex（GLM 必审） | 已合并（#56，f5886d4） |
| [T76](T76-isolation-install.md) | `m2k isolation install / grant / revoke / uninstall`：管理员一次性建两个账号并锁目录权限，改前 `icacls /save` 备份并生成管理员手动执行的 `restore-acl.ps1`（不启动进程、不存密码） | Codex（GLM 必审；合并前须用户协助真机验收） | 已合并（#57，68a1e3e） |
| [T78](T78-windows-ci-tests.md) | 修正 Windows CI 短路径与 ACL 还原测试，调查老式 ACL 继承复算及本机目录风险 | Codex（GLM-5.3 必审） | DONE (#58) |
| [T73](T73-workspace-watch.md) | 框架观测外部 Agent：运行中实时记录工作区变化（`workspace.changed`）；所有者指定的工作区外监视路径被改即判 FAIL（`outside.changed`，裁决器同样核对） | Codex（GLM-5.3 必审） | DONE (#59) |
| [T48](T48-contract-versions.md) | 第 3.5 阶段①：契约版本链——`m2k run --amends <旧任务>` 开新版本，从旧版本最后冻结的候选接着做；预算跨版本累计；旧结论一律不算，重新验收、审查；裁决器核对版本链与用量 | Codex（GLM-5.3 必审） | DONE (#60) |
| [T49](T49-impact-reuse.md) | 第 3.5 阶段②：影响分析——改需求开新版本时，只重跑受影响的子任务，未受影响的子任务结果复用（记 `subtask.reused`，不占预算）；父任务验收与终审照样重跑；裁决器重算复用决定 | Codex（GLM-5.3 必审） | DONE (#61) |
| [T79](T79-owner-requests.md) | 第 3.5 阶段③：所有者请求——Agent 用 `ask_owner`（外部 Agent 写 `.m2k/ask.json`）提问或申请扩大范围/追加预算，任务以“待授权”结束（退出码 3）；顶层预算用完同样待授权；`m2k answer` 只能在交互终端输入 YES 批准并开新版本；扩大范围或追加预算必须经批准，记 `budget.raised`；裁决器核对 | Codex（GLM-5.3 必审） | DONE (#62) |
| [T80](T80-preauthorization.md) | 第 3.5 阶段④：预授权——契约 `[preauth]` 写明可额外写的路径、可加的工具、能否联网、预算可追加的百分比（同一任务所有版本累计）；申请落在范围内时框架当场放行、任务不停（原生 Worker 同一对话继续，外部 Agent 同一工作区重启），重签凭证并记 `authority.granted`（带 OBS 回执）；超出照 T79 待授权；裁决器逐条重放 | Codex（GLM-5.3 必审） | DONE (#63) |
| [T82](T82-value-experiment.md) | 价值对照实验：同一个 Agent 在 24 个真实任务上分别有、无 m2k 跑（A 信 Agent、B 跑完自己跑验收、C 经 m2k），隐藏复核测试判真假；数谎称完成、偷改验收、误报和额外花费；判定规则事先写死 | Codex（建题、试跑、全量、统计）+ Claude 分析 + 用户判争议个案 | DONE（结果已存档 2026-10-29） |
| [T83](T83-r0-scale-benchmark-build.md) | R0 规模化基准（上）：从真实小库挖空 100 个独立函数块（可见 + 隐藏验收，逐块验真值与独立性）；harness 跑 ① gpt-6-astra 单干、② DeepSeek-V4-Flash 拆分不验证、③ DeepSeek-V4-Flash + m2k（③′ 现有内核一契约 N 子任务只记录），三线同法拼回同一主干、隐藏验收打分；N = 10 试跑后停 | Codex（建题、试跑）+ 独立核查 + Claude 分析 | DONE（试跑 2026-10-29） |
| [T84](T84-r0-scale-benchmark-run.md) | R0 规模化基准（下）：工人加网络重试、为 ③′ 另签 R0 专用测试授权书；正式跑 ① N=10/30/100、②③ 全部 100 块、③′ N=10/30；按事先写死的三条规则出判定数字与失败原因分类 | Codex（跑、统计）+ 独立核算 + Claude 分析 + 用户定方向 | DONE（2026-10-30，三条规则全不过，方向待定） |
| [T85](T85-r0-decisive-experiment.md) | R0 决定性实验：看不到的验收 + 退回重做 | Codex（修改、试跑、统计）+ 独立核算 + Claude 分析 | DONE（2026-10-30，规则 2 不过 → 选项 2） |
| [T86](T86-audit-layer-tail.md) | 审计层收尾：预算用完的候选照常验收与审查（过了就判 PASS）；审查失败（出错或非单个 JSON）用新 nonce 重审一次、原话入账；连接层错误按 `retry_delays` 重试，丢失请求的用量记为未知 | Codex（GLM-5.3 必审） | DONE (#64) |
| [T87](T87-positioning-and-demo.md) | 定位与事后核验演示（收尾最后一张卡）：`docs/POSITIONING.md` 写清 m2k 管什么、不管什么并附 T82/T84/T85 结论与 T86 修复；`examples/delivery_check/` 别的电脑一条命令可复现的 check-delivery 演示；check-delivery 每份交付只打一行并加中文合计 | Codex（GLM-5.3 必审 + Codex 独立复审） | DONE (#65) |
| [T88](T88-setup-guide.md) | 部署文档与一键配置脚本（封版后小破例）：`tools/quickstart.py` 一条命令生成部署目录（测试信任链、配置、签好的清单、示例任务）与改配置后重签；`docs/SETUP.md`（中文，给人）、`docs/AGENT_SETUP.md`（英文，给别人的 Agent 照着部署与接入自家 harness） | Codex（GLM-5.3 必审 + Codex 独立复审） | DONE (#66) |
| [T89](T89-public-cleanup.md) | 公开前清理：本机路径、Key 文件位置、账号名、私人项目名换成占位符；加 MIT `LICENSE`、补 `.gitignore`、`ci.yml` 显式只读；clone 地址改成 `yyf-agent` | Claude（独立完成） | DONE (#67) |
| [T90](T90-concurrent-runs.md) | 同一部署多个 run 并发：OBS / 锚点追加加跨进程锁、见证按账本顺序、模型请求按端点排队（`max_concurrent`）；修 20 并发写坏 OBS 的事故，压测到 150 | Claude（GLM 必审 + Codex 复审） | DONE (#68) |

第 1.5 阶段建议顺序：T07 → T06 → T05 → T10 → T11 → T09（Codex 依次做，每张卡新会话）；T08 由 Claude 在 T07 合并后并行做。
五张卡都改核心逻辑，按分工不交给 DeepSeek；DeepSeek 继续接只写测试、样例的小卡。

第 2 阶段顺序：T14 → T15 → T16 → T17 → T18 → T19 → T20（T16 用到 T15 的配置加载，改为串行）。签名格式沿用旧项目（{schema, body, signature_b64} + 规范化 JSON），旧测试根可直接用于演练。

第 3 阶段顺序：T21 → T22 → T23 → T24（T25、T26 可并行）→ T27 → T28 → T29；之后首个适配器（通用 CLI Worker + Codex）。

第 5 阶段顺序：T39 只读裁决 → T40 框架指纹 → T42 独立 OBS 旁证 → T45 OBS 收尾与性能 → T43 发行清单与配置变更须所有者确认 → T44 OBS 必需、回执与 OBS 日志对账、报告以 Gate 为准 → T63 链头签名与外部锚定 → T64 清单防回滚、吊销、裁决器核对清单 → T47 交付回执 → 第 5 阶段攻击演练（DeepSeek 经 m2k，T46）。原 T44 于 2026-10-17 拆为 T44、T63、T64（卡号只能是数字）。
