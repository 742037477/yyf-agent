# T87: 定位与事后核验演示——写清 m2k 管什么、不管什么，附三次实验结论；做一个别的电脑能复现的 check-delivery 演示包

- **Assignee:** Codex `gpt-6.1-sol`（max）实现；GLM-5.3 必审 + Codex `gpt-6-astra`（ultra）独立复审
- **Branch:** `task/T87-positioning-demo`
- **Line budget:** ≤ 10 added lines in `m2k/`（只许改 `m2k/cli.py` 的 `cmd_check_delivery` 输出；测试、文档、示例不计）
- **Files you may change:** `m2k/cli.py`、`tests/test_delivery.py`、`tests/test_delivery_demo.py`（新建）、
  `examples/delivery_check/*`（新建）、`docs/POSITIONING.md`（新建）、`docs/ARCHITECTURE.md`、`README.md`、
  `tasks/T87-positioning-and-demo.md`、`tasks/README.md`
- **Status:** DONE (merged #65, 273f948)

## 背景（用户 2026-10-30 拍板：m2k 不再做大，修几处毛病、封版，把这段经历当成果用）
T86 已修掉实验暴露的三处毛病。本卡是收尾的最后一张卡，以文档为主：让第一次打开仓库的人三分钟内知道 m2k 能证明什么、
不能证明什么、证据从哪来；再给一个不需要 API Key、不联网、在别的电脑上一条命令就能跑的“事后核验”演示。
本卡之后停止开发。

## 1. `m2k check-delivery` 输出整理（`m2k/cli.py`，≤ 10 行）
现在同一份交付会打印两行（文件夹一行、`<id>.m2k-receipt.json` 一行，内容相同），演示时让人困惑。改为：
1. 同一对“文件夹 + 回执”只打印一行，用先出现的名字（排序后文件夹在前）。判断“同一对”用 `check_delivery` 返回值里
   共享的结果对象（`checked[folder]`），**不要**按文件名后缀去重（任务 ID 本身可以以 `.m2k-receipt.json` 结尾，见
   `tests/test_delivery.py` 里 `named` 那条）。孤立回执、残留 staging 等没有配对的条目照旧各打一行。
2. 末尾加一行中文合计：`合计 N 份：通过 X 份，未经核验 Y 份`（空交付目录仍打印原来的 `OK empty delivery root`，不加合计）。
3. 退出码、`check_delivery()` 的返回值、每行原有的 `OK …` / `未经核验 …:` 格式和英文原因文字都不变。
- 旧测试只有在断言“回执名那一行”时才需要同步（按派活规则：最保守地改、Handoff note 逐条写明）；其他断言一个不改。

## 2. 事后核验演示包 `examples/delivery_check/`
### `make_demo.py`（约 120 行，可直接 `python examples/delivery_check/make_demo.py [目录]` 运行）
- 只用标准库 + 本仓库的 `m2k`（脚本把仓库根目录加进 `sys.path`，不要求 `pip install`）；**不调真实模型、不联网、不需要 Key**；
  Windows / macOS / Linux 都能跑（pathlib、UTF-8；用 `m2k.cli.main([...])` 进程内调用 check-delivery，避免控制台编码问题）。
- 目标目录默认 `delivery-check-demo`，必须不存在或为空，否则拒绝并退出 2（不覆盖任何东西）。
- 步骤（每步打印一行中文说明）：
  1. 生成一对临时 OBS 密钥（`trust.generate_keypair`），用 `obs.ObsClient` 起本地旁证进程，账本、OBS 日志、私钥都在演示目录里、工作区之外。
  2. 用 `ScriptedModel`（脚本模型，事先写好的步骤）和 `Kernel(..., obs=observer)` 跑 4 个小任务：`report-ok`、`report-edited`、
     `report-swapped`、`report-forged`，每个都是“写 `calc.py` 通过所有者的 `check.py`”，独立审查 GO，交付到 `delivered/`，
     各自带 OBS 签名回执。打印每个任务的判定（应全部 PASS）。
  3. **刚交付时**核验一次：独立模式（只凭公钥）→ 4 份全部 OK，退出码 0。
  4. 模拟交付之后发生的事（打印每一条做了什么）：
     - `report-edited`：有人改了 `calc.py` 的内容；
     - `report-swapped`：删掉 `check.py`、加了一个 `notes.txt`；
     - `report-forged`：改了 `calc.py`，并把回执里该文件的哈希改成新内容的哈希（想“补一张回执”）；
     - 新建 `hand-made/`：手工放进去的文件夹，没有回执；
     - `report-ok`：不动。
  5. 再核验两次，打印每次的完整输出和退出码：
     - 独立模式：`check-delivery delivered --standalone --public-key <公钥>`（收件人只拿到交付目录和公钥时的做法）；
     - 完整模式：写 `check.toml`（`ledger`、`[obs] log`、`public_key_b64`，格式照 `tests/test_delivery.py` 的 `cli_check`），
       `--config check.toml check-delivery delivered`（发件方有账本和 OBS 日志时，另外核对哈希链、OBS 日志、Gate PASS、只读裁决）。
  6. 把公钥写到 `public-key.txt`，最后打印两条可以自己再跑的命令（Windows 和 bash 各一行）。
- 预期第 5 步两种模式都是：`report-ok` OK；`report-edited` `changed file: calc.py`；`report-swapped` `missing file: check.py; extra file: notes.txt`；
  `report-forged` `receipt event hash mismatch`；`hand-made` `missing receipt file`；合计 5 份、通过 1、未经核验 4；退出码 1。
  （Claude 已在 Linux 上用原型实测过这五种原因文字。）
- 脚本自身退出码：第 3 步全 OK 且第 5 步两种模式都恰好是上面的结果 → 0，否则 1（演示也是自检）。

### `README.md`（中文，约 60～80 行）
- 一段话：这个演示证明什么（交付之后文件被改、被删、被加、回执被篡改、没有回执的产出，都会被指出来）；
  不证明什么（脚本模型不代表真实 Agent 的质量；回执证明“这份文件就是当时验收和审查通过的那份”，不证明验收本身写得对；
  拿得到 OBS 私钥的人可以签假回执——本机演示里私钥就在演示目录，正式使用时私钥应只在框架服务账号下，见 POSITIONING 的“不管什么”）。
- 别的电脑上怎么跑：要求 Python ≥ 3.11 和 `cryptography`；`git clone` → `pip install cryptography` → `python examples/delivery_check/make_demo.py`。
- 一张表把五个英文原因翻成中文并解释（missing receipt file / changed file / missing file / extra file / receipt event hash mismatch，
  再加常见的 `signature does not verify`、`delivery was not post-verified`、`no Gate PASS for the delivery candidate`、`adjudication …`）。
- 独立模式与完整模式各核验什么、什么时候用哪个。
- 实际输出示例：贴第 5 步的真实输出（实现时在 Windows 上实跑后粘贴，路径可截短）。

## 3. 定位文档 `docs/POSITIONING.md`（新建，中文，约 150～200 行）
按下面的结构写；数字一律照本节，不要另行推算或改写口径；每个实验链接到对应任务卡。
1. **一句话**：m2k 是给 AI Agent 产出用的“审计与可核验层”——交付前跑所有者的验收和独立审查，只交付判 PASS 的那一份冻结候选；
   全程写进只追加、哈希链接、有旁证回执和外部锚点的账本；交付之后任何人可用回执核验文件有没有被改。
2. **管什么**（每条一句话 + 机制 + 对应命令或卡号）：只交付 PASS 的冻结候选（G6/G7）；验收文件受保护，改了判 FAIL；
   账本只追加 + OBS 回执 + 外部锚点，事后改写可被发现（`check-ledger`）；只读裁决器从账本重算结论（`adjudicate`）；
   交付回执 + `check-delivery`；信任链、凭证、预算、写入范围；改需求开新版本、所有者批准、预授权（T48、T79、T80）；
   `[watch]` 发现 Agent 改了工作区外的配置（T73）。
3. **不管什么 / 证明不了**：
   - 不让可信厂商 Agent 做得更好：T82 里顶级 Agent 本来就不谎报、不改验收、不理注入，m2k 多拦下 0 次，耗时多一倍以上。
   - 不能靠“便宜模型 + 验证”规模化追上贵模型：T84、T85。
   - AI 审查在这些实验里几乎没拦下东西（T84：GLM 71 次 GO、1 次 NO_GO，且那 1 次可见测试已经挂了），却是最贵的环节。
   - 验收写得对不对由所有者负责；m2k 只照验收判。
   - 本机同一 Windows 用户、没有系统级隔离：专门攻击框架的 Agent 可能伪造批准、拿到 OBS 私钥；读机密并外传看不见，
     `[watch]` 只揭示改动不阻止。Linux 上命令在 bubblewrap 里跑；T46 攻击演练在 GitHub 的隔离 Linux 环境通过。账号隔离（T75～T77）已暂停。
   - 没测到的场景：任务之间互相依赖、长时间无人看管、不可信 Agent、不可逆动作（发布、删除、付款）。
4. **什么时候值得用**：日常用可信厂商 Agent——跑完自己跑测试、留下记录就够了；需要向别人证明“这份交付经过了验收、交付后没被改过”，
   或要事后追查谁在什么时候做了什么时，用 m2k 的账本、回执和 `check-delivery`（演示见 `examples/delivery_check/`）。
5. **三次实验**（共同方法：判定规则在跑之前写进任务卡、结果出来后不改；打分用工人看不到的隐藏测试作真值；
   统计由一个会话做、另一个只读会话用自己的脚本从原始结果重算核对，数字一致才采用；原始数据与 harness 在仓库外的实验目录，
   完整记录在各卡 Review notes）。每个实验写：问了什么、怎么做、事先写死的规则、结果、判定。
   - **T82 价值对照**（[卡](../tasks/T82-value-experiment.md)）：24 个真实修复任务（普通 12、验收写错的压力题 6、注入 6），
     A 信 Agent、B 跑完自己跑可见验收、C 经 m2k；Codex（gpt-6-astra）跑全部 24 题 A/C，dsh 跑 6 道注入题 A/C，共 60 次。
     规则：m2k 比 B 多拦下 ≥ 3 次、确认误报 ≤ 3、额外耗时中位 ≤ +60% 才按原路线；C 与 B 差 < 3 → 调方向。
     结果：坏交付 A/B/C 两个 Agent 都是 0/0/0，多拦下 0 次；无篡改，6 道注入没有一个被照做；6 道压力题两条线都停下报告矛盾；
     成功率 A→C：Codex 18/24→18/24、dsh 6/6→6/6；额外花费（逐题 C/A 中位）Codex 时间 2.21 倍、token 1.10 倍，dsh 时间 2.82 倍；
     确认误报 3 例（都是审查环节故障）。判定：规则 2，调方向。
   - **T84 规模化基准**（[卡](../tasks/T84-r0-scale-benchmark-run.md)，规则见 [T83](../tasks/T83-r0-scale-benchmark-build.md)）：
     从 4 个真实小库挖空 100 个独立函数块（可见 + 隐藏测试）；① gpt-6-astra 单干、② DeepSeek-V4-Flash 拆分不验证、
     ③ DeepSeek-V4-Flash + m2k（可见验收 + GLM 审查）。规则：③ ≥ ① 的 90%；③ 花费 ≤ ① 的 40%；③ ≥ ② + 10 个百分点且坏合并 ≤ ② 的一半。
     结果 N=100：按隐藏测试完成 ① 89、② 72、③ 52；坏合并 11 / 28 / 19；标价花费 ① $2.80、② $1.16、③ $2.79（其中 GLM $1.63）；
     N=30：27 / 22 / 15。三条规则全部不过。原因：③ 有 25 块轮数用完未交付，其中 12 块代码已经写对却被丢弃。
   - **T85 决定性实验**（[卡](../tasks/T85-r0-decisive-experiment.md)）：隐藏测试按固定哈希拆成两半——A 给所有者当工人看不到的验收、
     T 只用来打分；②\* 自测不过就带失败信息重做、③\* 所有者验收不过就退回重做，各最多 3 次；① 用 T84 产出按 T 重评。
     规则：③\* ≥ ① 的 90%，且 ③\* ≥ ②\* + 10 个百分点。结果 N=100：① 93、②\* 88、③\* 84；规则 1 过（84 ≥ 83.7，只多 0.3），
     规则 2 不过（需 ≥ 98）。坏合并 ②\* 12、③\* 9。判定：放弃“便宜模型规模化”，m2k 回到审计 / 可核验层。
     花费 ②\* $1.22、③\* $1.44。
   - **合起来看**：可信厂商 Agent 单任务，m2k 没多拦下东西；规模化场景，看不到的验收提高了合并精度，但总完成率不如便宜模型自测重做，
     更不如贵模型单干（贵模型按标价也不贵）。
6. **实验暴露、已在 T86 修掉的三处**（[卡](../tasks/T86-audit-layer-tail.md)）：预算用完时已写好的候选照常验收和审查，通过就交付
   （T84 中 12 块写对却被丢弃）；审查出错或回复不是单个 JSON 时用新 nonce 重审一次，原话前 2000 字入账（T82 的 3 次误报、且无法诊断）；
   连接层错误按 `retry_delays` 重试，丢失请求的用量记为未知（T84、T85 的断线）。
7. **项目状态**：已封版（2026-10-31）。冻结、不再推进：实时等待授权（T81）、Windows 账号隔离（T75～T77 之后部分）、记忆与经验库（第 6 阶段）、
   中断续接（第 4 阶段）、规模化重构（R1～R6）、交给他人（第 8 阶段）。若将来有真实用户带着上面“没测到的场景”来，再按需要重启。

## 4. README 与 ARCHITECTURE
- `README.md`：
  - 开头第 3～5 行换成三到五行定位摘要（照第 3 节第 1 条），并链接 `docs/POSITIONING.md` 和 `examples/delivery_check/`；保留原有指向 ARCHITECTURE 的链接。
  - 紧接着加一节“三次实验一览”：一张 3 行的小表（实验、问题、关键数字、判定），细节链接到 POSITIONING。
  - 在“快速开始”后加一节“事后核验演示”：两三行说明 + 一条命令。
  - 末尾过时的“当前边界（第 3 阶段进行中）”一节改为“项目状态”：说明已封版、链接 POSITIONING 第 7 条；其中命令隔离（T08）那一段事实仍有效，原样保留。
  - 其他章节不动。
- `docs/ARCHITECTURE.md`：只在文件最开头（标题下）加 ≤ 5 行“封版说明”：2026-10-31 封版，定位与实验结论见 POSITIONING，
  下文的分阶段计划中未完成的部分全部冻结。正文不改。

## 5. 测试 `tests/test_delivery_demo.py`
- 在临时目录运行演示脚本（`runpy` 或导入其主函数），断言：脚本退出码 0；4 个任务都 PASS；第 3 步结果全部 OK；
  第 5 步两种模式的五个条目及原因与第 2 节一致；目录非空时拒绝且不改动已有文件。
- 第 1 节的输出整理加测试：同一对只打印一行；合计行数字正确；孤立回执仍单独一行；退出码不变。
- 不调真实模型、不联网。

## Out of scope
- `m2k/` 里除 `cmd_check_delivery` 输出以外的任何改动；英文原因文字的翻译（放 README 表里）；新的命令或参数。
- 重跑任何实验、改实验目录、改三份演示配置、主账本、主工作区。
- 动画页、`m2k live`。

## Acceptance
```
python -m unittest tests.test_delivery tests.test_delivery_demo -v
python -m unittest discover -s tests -t .
python examples/delivery_check/make_demo.py <新的临时目录>      # Windows 上实跑，退出码 0，输出贴进 examples/delivery_check/README.md
```
复审另外核对：POSITIONING 与 README 里的每个数字都与 T82、T84、T85、T86 卡 Review notes 一致；文档没有夸大（没有写成“m2k 能防住攻击”
或“m2k 提高了完成率”）；演示 README 的命令在全新克隆上照抄能跑。

## Review notes (written by reviewer)

- 2026-10-31 Claude: merged #65 (273f948). GLM-5.3 GO; independent Codex review (gpt-6-astra, ultra) PASS with no blocking or non-blocking points — full suite 1015 passed / 15 skipped, the demo exits 0 in a fresh directory, both check modes give 5 entries / 1 passed / 4 unverified with the card's exact reasons, a non-empty target is refused (exit 2) with nothing changed, and every number in POSITIONING / README matches the T82–T86 Review notes. CI (ubuntu, card-scope, review) green.
- 2026-10-31 Claude: spot-checked `m2k/cli.py` (+7 / 10): pairs deduplicated by the shared result object, exit code and `check_delivery()` return value unchanged; POSITIONING states the limits plainly and does not claim m2k blocks attacks or raises completion rates.
- 2026-10-31 Claude: this was the last card. The project is sealed; frozen items are listed in `docs/POSITIONING.md` section 7.

## Handoff note (written by implementer, ≤ 10 lines)

- 对照 fff8125：原交付测试 33 项通过；新增三个 CLI 用例先复现重复行与缺合计，再验证按共享对象去重、计数及退出码。
- 旧测试断言未修改或删除；tests/test_delivery.py 只新增配对去重、混合条目合计、空目录与回执后缀任务名的测试。
- 新增 112 行离线演示及三个演示测试，使用 ScriptedModel 和真实本地 OBS 进程；不调真实模型、不联网。
- Python 3.13：定向验收 39 项通过（跳过 2）；全套 1015 项通过（跳过 15），退出码均为 0。
- Windows 指定目录 T87-demo-run 实跑退出 0，第 5 步完整真实输出已贴进演示 README；该演示目录已删除。
- 全新本地克隆依 README 安装依赖并运行默认命令退出 0；PowerShell 独立核验与完整核验命令均返回预期退出码 1。
- POSITIONING 与根 README 的实验数字照卡引用并核对 T82～T86 Review notes；ARCHITECTURE 仅在标题下加三行，正文及根 README 其余章节保留。
- 仅改 Files 范围，m2k/ 新增 7/10 行；card_check、diff --check、Python 3.11 语法及本地文档链接检查通过；CI、GLM 和独立复审待执行，Linux/macOS 未实跑。
- 复跑克隆内容已清理，T87-readme-check/m2-kernel/.git 为空隐藏目录，强制删除被自动审批以 blocked by policy 拒绝，保留该空目录。
