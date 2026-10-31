# 事后核验演示

这个演示指出交付之后文件被改、被删、被加、回执被篡改，以及没有回执的产出。
脚本模型不代表真实 Agent 的质量；回执证明“这份文件就是当时验收和审查通过的那份”，不证明验收本身写得对。
拿得到 OBS 私钥的人可以签假回执：本机演示中私钥就在演示目录，正式使用时私钥应只在框架服务账号下。
完整限制见 [POSITIONING 的“不管什么”](../../docs/POSITIONING.md#3-不管什么--证明不了)。

## 怎么跑

需要 Python ≥ 3.11、Git 和 `cryptography`；Windows、macOS、Linux 均可运行。
克隆与安装依赖需要联网；演示本身不联网、不调真实模型、不需要 API Key，也不要求安装 m2k。

```bash
git clone https://github.com/742037477/yyf-agent.git
cd yyf-agent
pip install cryptography
python examples/delivery_check/make_demo.py
```

默认生成 `delivery-check-demo/`；也可在命令最后指定新目录，目录必须不存在或为空，否则退出 2，保留已有内容。
脚本先生成临时 OBS 密钥、本地旁证日志和账本，在各自工作区中运行四个任务，全部交付后先核验，再模拟改动。
演示目录中的 `workspaces/` 是任务工作区；私钥、账本和 OBS 日志都在其外。这里只用临时密钥，不发布外部锚点。
自检符合预期时脚本退出 0：刚交付时四份全通过；改动后两种核验都只有一份通过，核验命令各退出 1。

## 两种核验模式

独立模式只需要交付目录和可信公钥，核对回执事件哈希、OBS 签名及文件清单，适合收件人核验可搬移的交付。
完整模式还需要发件方的账本和 OBS 日志，另外核对账本哈希链、OBS 日志、同一候选的 Gate PASS 和只读裁决。
完整模式的信任链等检查取决于配置；本演示的 `check.toml` 只配置账本、OBS 日志和公钥，不演示正式信任链部署。
在仓库根目录重跑，PowerShell 读取公钥的写法是：
```powershell
python -m m2k check-delivery delivery-check-demo/delivered --standalone --public-key (Get-Content -Raw delivery-check-demo/public-key.txt).Trim()
```
bash 的对应写法是：
```bash
python -m m2k check-delivery delivery-check-demo/delivered --standalone --public-key "$(cat delivery-check-demo/public-key.txt)"
```
完整模式在两种终端中都可用：
```text
python -m m2k --config delivery-check-demo/check.toml check-delivery delivery-check-demo/delivered
```
脚本最后也会打印含实际路径、公钥的 PowerShell 和 bash 命令。公钥应从可信渠道取得，不能仅信随文件附来的任意公钥。

## 英文原因怎么读

| 原因 | 中文含义与解释 |
| --- | --- |
| `missing receipt file` | 缺少回执文件，无法证明该目录经过交付流程。 |
| `changed file: …` | 文件内容与签名交付记录中的哈希不同。 |
| `missing file: …` | 交付记录列出的文件已缺失。 |
| `extra file: …` | 目录多出了交付记录中没有的文件。 |
| `receipt event hash mismatch` | 回执中的事件内容与事件哈希不符；只修改文件哈希不能补出有效回执。 |
| `signature does not verify` | OBS 签名无法用指定公钥验证，可能公钥不对或签名、内容被改。 |
| `delivery was not post-verified` | 记录没有确认交付落地后的核验通过。 |
| `no Gate PASS for the delivery candidate` | 账本没有同一交付候选的 Gate PASS。 |
| `adjudication …` | 只读裁决未得到同一候选的 PASS，后面的文字说明原因。 |

## Windows 实际输出

2026-10-31，Python 3.13 运行 `python examples/delivery_check/make_demo.py <worktrees>\T87-demo-run`，脚本退出 0。第 5 步完整输出如下：
```text
第 5 步：交付后再核验，分别打印独立模式和完整模式的结果。
独立模式：
未经核验 hand-made: missing receipt file
未经核验 report-edited: changed file: calc.py
未经核验 report-forged: receipt event hash mismatch
OK report-ok (2 files)
未经核验 report-swapped: missing file: check.py; extra file: notes.txt
合计 5 份：通过 1 份，未经核验 4 份
退出码：1
完整模式：
未经核验 hand-made: missing receipt file
未经核验 report-edited: changed file: calc.py
未经核验 report-forged: receipt event hash mismatch
OK report-ok (2 files)
未经核验 report-swapped: missing file: check.py; extra file: notes.txt
合计 5 份：通过 1 份，未经核验 4 份
退出码：1
```
