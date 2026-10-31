# T76: `m2k isolation install / grant / revoke / uninstall` — create the two accounts and lock the folders, with an ACL backup and an administrator-run restore script (isolation line, card 2 of 3; formerly "T66b", admin side only)

- **Assignee:** Codex (GLM-5.3 GO required; owner-assisted acceptance required before merge, see "Acceptance")
- **Branch:** `task/T76-isolation-install` (start from the latest main)
- **Line budget:** ≤ 450 added lines in `m2k/` in total (tests not counted)
- **Files you may change:** `m2k/isolation_install.py` (new), `m2k/isolation.py` (only the config keys and the code
  folder, see "Configuration"), `m2k/cli.py`, `tests/test_isolation_install.py` (new), `tests/test_isolation.py`
  (only assertions about the configuration keys and the code folder), `m2k.example.toml`, `README.md`,
  `tasks/README.md`
- **Status:** DONE (merged #57, 68a1e3e)

## Goal
T75 added a read-only `m2k isolation check`. Its first real run on the owner's machine (2026-10-19, Windows 11 Home
25H2, NTFS, owner account `<owner-account>` is a UAC-filtered administrator) showed:
- neither `m2k-svc` nor `m2k-agent` exists; `<svc-data>` and `C:\m2k-tools` do not exist;
- **Authenticated Users can modify `<dev-keys>` and `<demo-dir>`** (mask 0x001301BF) and Users can read
  them, so any new standard account, `m2k-agent` included, could read and write the development Root key folder;
- all four existing folders (`<dev-keys>`, `<old-project-private>`, `<demo-dir>`, the framework code
  `<repo>`) are owned by `<owner-account>`.

This card adds the **administrator side** of the isolation: one command an administrator runs once to create the
two standard accounts and set the folder permissions (ACLs), a full ACL backup taken before anything changes, a
restore script that an administrator runs by hand and that does not depend on m2k or Python, commands to grant and
revoke the owner's deliberate exceptions, and an uninstall that calls the restore script.

**This card starts no process as another account and stores no password.** Storing the `m2k-agent` password with
DPAPI, starting agents as `m2k-agent` inside a Job Object, the queue and the scheduled task are **T77** (written
after this card is merged and installed, starting with a feasibility test on the real accounts). Wiring all of it
into `m2k run` stays **T68**.

The owner decided (2026-10-19): the owner types both account passwords during install (hidden input, typed twice);
the restore script is run manually by an administrator; account deletion during restore is a separate question that
defaults to "no".

## Configuration (`[isolation]`, extends T75)
```toml
[isolation]
svc_user     = "m2k-svc"
agent_user   = "m2k-agent"
data_dir     = '<svc-data>'
tools_dir    = 'C:\m2k-tools'
protected    = ['<dev-keys>', '<old-project-private>', '<demo-dir>']
agent_read   = []
agent_write  = []
# new in T76 (all optional):
owner_user   = "<owner-account>"                  # default: the user running the command
code_dir     = '<repo>'        # default: the folder that contains the m2k package (T75 behaviour)
code_readers = []                       # extra accounts given read+execute on code_dir only, e.g. "CodexSandboxOffline"
backup_root  = 'C:\m2k-acl-backup'      # default shown
```
- `code_dir` replaces T75's hard-wired "folder that contains the `m2k` package" in **both** `check` and `install`
  (default unchanged), so running from a temporary worktree can never lock the worktree instead of the real code.
- `owner_user`, `code_readers` entries: account names only (same rule as `svc_user`: a bare `S-…` SID is a
  configuration error). `code_readers` must not contain `svc_user` or `agent_user`.
- Same error handling as T75 (exit 2, message names the key).

## Commands (all in a new module `m2k/isolation_install.py`; `cli.py` only wires and prints)
| Command | Needs elevation | Changes the machine |
|---|---|---|
| `m2k --config <f> isolation install` | yes | accounts, folders, ACLs, backup folder |
| `m2k --config <f> isolation grant` | yes | adds `agent_read` / `agent_write` ACEs |
| `m2k --config <f> isolation revoke` | yes | removes every `agent_user` ACE it granted |
| `m2k --config <f> isolation uninstall` | yes | runs the restore script of the first install |
| `m2k --config <f> isolation check` | no | nothing (T75) |

Common rules for the four mutating commands:
- **Elevation:** check the token with `GetTokenInformation(TokenElevation)`; not elevated → exit 2,
  `请在“以管理员身份运行”的终端中执行`. Not Windows → exit 2 (same message style as T75).
- **Plan, then `YES`:** before changing anything, print the full list of changes (each account, folder, ACL in
  readable form, each command) and read one line from stdin; only the exact text `YES` continues. Anything else →
  `已取消，未做任何更改`, exit 1. No `--yes` flag in this card.
- **Never a password on a command line, in a log, or in a file.** External commands are run with an argument list
  (no `shell=True`); every command, its exit code and its output go to the install log.
- **Use SIDs, not localized names,** in every ACE (`*S-1-5-32-544` Administrators, `*S-1-5-18` SYSTEM, the looked-up
  SIDs of `owner_user`, `svc_user`, `agent_user`, `code_readers`). The machine is a Chinese-language Windows.
- Exit codes: 0 done and the follow-up `check` passes; 1 cancelled, a step failed, or the follow-up `check` fails;
  2 configuration / preflight error (nothing changed).

## `install`: steps, in this order (each step idempotent; stop at the first failure)
0. **Preflight (changes nothing):** elevated; Windows; config valid; every managed folder (`code_dir`, each
   `protected`, and `data_dir` / `tools_dir` if they already exist) is on NTFS (`GetVolumeInformationW`).
   **Refuse (exit 2)** a managed path that is a drive root, `%SystemRoot%` or inside it, `C:\Users` or any user
   profile root, `backup_root` or inside it, or that is equal to / nested inside another managed path (for example
   `data_dir` inside `code_dir`). Run the T75 `check` and keep its output for step 1. Print the plan; require `YES`.
1. **Backup** (before any change). Create `backup_root` if missing with a protected DACL: `owner_user`,
   Administrators, SYSTEM full control, nothing else. Create `backup_root\<YYYYMMDD-HHMMSS>\` and write into it:
   - for every managed folder that exists: `icacls <folder> /save <n>.acl /T /C` (run so that `/restore` from the
     folder's parent works; record which parent); a non-zero exit stops the install;
   - `before-check.txt` (the step-0 check output) and `manifest.json`: config path, managed folders with
     "existed before" true/false, the `.acl` file of each, the SIDs used, whether each account existed before, and
     `first_install` = neither account existed before;
   - `install.log`, appended by every later step.
2. **Generate `restore-acl.ps1`** into the same folder now, before any change (content: see next section).
3. **Accounts.** For `svc_user` and `agent_user`: if the account exists, verify it is not in Administrators
   (else stop, exit 1) and do not touch it. Otherwise ask for its password twice with hidden input
   (`getpass`), require ≥ 12 characters and both entries equal (three tries, then stop), and create it with
   `NetUserAdd` (ctypes, `USER_INFO_1`, `USER_PRIV_USER`, flags `UF_SCRIPT | UF_DONT_EXPIRE_PASSWD`, comment
   `m2k service account` / `m2k agent account`); Windows puts new accounts in Users. Overwrite the password buffers
   after use. Record "created by this install" in `manifest.json`.
4. **Folders.** Create `data_dir` with subfolders `secrets\`, `ledger\`, `queue\inbox\`, `work\`, `logs\`, and
   `tools_dir`, if missing.
5. **ACLs.** Set each managed folder to **exactly** the DACL below, protected (inheritance from the parent cut),
   owner unchanged, and make every child inherit it (child explicit ACEs removed). Recommended:
   `TreeResetNamedSecurityInfoW(..., KeepExplicit=FALSE)` via ctypes with an SDDL-built DACL; acceptable:
   `SetNamedSecurityInfoW` on the folder plus `icacls "<folder>\*" /reset /T /C /Q`. Order: `tools_dir`,
   `data_dir`, `code_dir`, then each `protected`.
6. **Deliberate exceptions:** apply `agent_read` / `agent_write` exactly as `grant` does.
7. **Verify:** run the T75 `check` again; write `after-check.txt`. Any 不通过 → exit 1 and print the restore script
   path. Success → print the backup folder and restore script path, exit 0.

On any failure after step 2, print in Chinese: what failed, that changes may be partial, and the exact command to
run the restore script.

### Target DACLs (`OI CI` = inherited by subfolders and files; "full" = `FA`; "RX" = read + execute `0x1200A9`)
| Folder | owner_user | Administrators, SYSTEM | svc_user | agent_user | others |
|---|---|---|---|---|---|
| each `protected` | full | full | — | — | — |
| `code_dir` | full | full | RX | — | `code_readers`: RX |
| `tools_dir` | full | full | RX | RX | — |
| `data_dir` (top and all children) | — | full | full | — | — |
| `data_dir\ledger\` | RX (added) | inherited | inherited | — | — |
| `data_dir\queue\inbox\` | modify (added) | inherited | inherited | — | — |
| `backup_root` | full | full | — | — | — |

Notes for the implementer:
- `data_dir\ledger\` and `data_dir\queue\inbox\` keep inheritance from `data_dir` and get one extra explicit
  `OI CI` ACE for `owner_user`. The owner reaches them by full path (traverse checking is bypassed for everyone by
  default); browsing `data_dir` itself in Explorer is expected to be refused.
- Nothing grants `agent_user` anything under `data_dir`; per-task workspace grants under `data_dir\work\` are T77.
- After this card the three demo configs keep working unchanged: `owner_user` keeps full control everywhere it
  had it.

## `restore-acl.ps1` (generated by `install`, run by an administrator by hand)
- Self-contained: no m2k, no Python, only PowerShell 5.1 built-ins and `icacls`. **Saved as UTF-8 with BOM**
  (PowerShell 5.1 reads BOM-less files as the ANSI code page and would garble the Chinese text).
- First line `#Requires -RunAsAdministrator`; `$ErrorActionPreference = 'Stop'`; a Chinese header comment saying
  when the backup was taken, by which config, and whether isolation was already installed at that time
  (`first_install` false → `注意：此备份拍摄时隔离已安装`).
- Lists every folder it will restore with its `.acl` file, checks every `.acl` file exists, then
  `Read-Host '输入 YES 开始还原'`; only the exact `YES` continues.
- For each folder: `icacls '<parent>' /restore '<file>' /C`; prints each exit code and a final summary; never
  deletes or moves a file. Folders that did not exist before the install (`data_dir`, `tools_dir`) are **not**
  deleted: the script names them and says they can be removed by hand.
- Then, separately: only for the accounts this install created, ask
  `是否删除账号 <names>？输入 DELETE 删除，直接回车跳过`; default skip; delete with `Remove-LocalUser` and remind that
  `C:\Users\<name>` profile folders stay.
- All paths written as single-quoted PowerShell strings with `'` doubled; test with a path containing a space and
  a `'`.

## `grant`, `revoke`, `uninstall`
- **`grant`:** for each `agent_read` path add `agent_user:(OI)(CI)RX`, for each `agent_write` path add
  `agent_user:(OI)(CI)M` (`icacls <path> /grant *<sid>:...`). Paths must exist. The T75 config rule already refuses
  paths equal to or inside `data_dir` / `protected`; additionally refuse `code_dir`, `tools_dir`, `backup_root`
  and anything inside them. Append one JSON line per ACE to `backup_root\grants.jsonl` (time, path, right).
  Run `check` afterwards and print it.
- **`revoke`:** remove every explicit `agent_user` ACE (`icacls <path> /remove:g *<sid>`) from the union of the
  current `agent_read` / `agent_write` paths and every path in `grants.jsonl`, so a path dropped from the config
  is still revoked; log it; run `check`.
- **`uninstall`:** pick the newest backup whose `manifest.json` says `first_install: true` (none → exit 2 with a
  clear message); print which; run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <restore-acl.ps1>` in
  the same console and return its exit code. The script still asks for `YES` itself.
- The Root signature on the config (that `agent_read` / `agent_write` cannot be widened by an agent) is enforced
  when T68 wires `m2k run`; in this card the elevated administrator running `grant` is trusted.

## Tests (`tests/test_isolation_install.py`)
- **Portable (Linux PR job):** all machine actions go through one injectable "system" object (account lookup and
  creation, elevation, volume type, ACL set/reset, run command, read stdin, getpass). A fake records every call in
  order. Cover:
  - not elevated / not Windows / each configuration error / each refused path → exit 2 and **zero** mutating calls;
  - anything other than `YES` → exit 1 and zero mutating calls;
  - order: every `icacls /save` and the written `restore-acl.ps1` come before the first account or ACL change;
  - failure injected at each of steps 1, 3, 4, 5, 6 → stop there, exit 1, restore hint printed (after step 2);
  - re-run with both accounts existing → no password prompt, no `NetUserAdd`, new backup with
    `first_install: false`;
  - an existing account that is an administrator → stop, exit 1;
  - password rules (too short, mismatch, three failures); **the password string never appears** in any recorded
    command argument, log line, printed output, or any byte of any file written under a temporary `backup_root`;
  - the DACL requested for every folder matches the target table (by SID), including `code_readers`;
  - `restore-acl.ps1`: starts with a UTF-8 BOM, has `#Requires -RunAsAdministrator`, the `YES` prompt, one
    `icacls … /restore` per saved folder with the right parent, `DELETE` prompt listing only created accounts,
    correct quoting of a path with a space and a `'`;
  - `grant` / `revoke`: exact ACEs, refused paths, `grants.jsonl` union on revoke; `uninstall` picks the newest
    `first_install: true` backup.
- **Real Windows, no administrator needed** (`skipUnless(os.name == "nt")`, temporary folders the test creates
  and owns): build a small tree (a file, a subfolder, a child with an explicit Everyone ACE, a child with
  inheritance cut); `icacls /save` it; apply the real ACL routine with a target DACL of current user full +
  Everyone RX; check one child now inherits and the explicit child ACE is gone; then run the same `icacls
  /restore` command line the generated script would run, and assert a fresh `icacls /save` is byte-identical to
  the first. Never touch a folder the test did not create; never create accounts.
- `tests/test_isolation.py`: only add/adjust assertions for the new keys and `code_dir`.

## Acceptance
```
python -m unittest discover -s tests -t .
```
Plus **owner-assisted acceptance on the owner's machine, required before merge** (Codex prepares and checks; the
owner runs the elevated steps, because Codex cannot pass a UAC prompt). Use only throw-away paths and throw-away
account names, never the real folders or the real account names:
1. Codex creates `C:\m2k-t76-accept\` with `prot\` (files, a subfolder, a child with an explicit Everyone ACE, a
   child with inheritance cut), `code\`, `extra\`; a config with `svc_user = "m2k-t76-svc"`,
   `agent_user = "m2k-t76-agent"`, `protected = ['C:\m2k-t76-accept\prot']`, `code_dir = 'C:\m2k-t76-accept\code'`,
   `data_dir = 'C:\m2k-t76-accept\data'`, `tools_dir = 'C:\m2k-t76-accept\tools'`,
   `backup_root = 'C:\m2k-t76-accept\backup'`, `agent_read = ['C:\m2k-t76-accept\extra']`; saves a baseline
   `icacls <dir> /save … /T /C` of `prot`, `code`, `extra` into `%TEMP%\t76-accept\`; writes the owner a short
   Chinese step list.
2. The owner, in an elevated PowerShell, runs `install` (types both passwords and `YES`), then `grant`, then
   `revoke`, then `uninstall` (types `YES`, then `DELETE`).
3. Codex (not elevated) compares fresh `/save` files with the baseline (byte-identical), confirms with `net user`
   that both throw-away accounts are gone, and puts in the handoff note: the `after-check.txt` table from step 2,
   the comparison result, and any message the owner saw that looked wrong.

## Out of scope
- Storing passwords (DPAPI), starting a process as `agent_user`, Job Objects, the queue, the scheduled task, the
  "log on as a batch job" right, per-task workspace grants and freezing: **T77**.
- `m2k run` integration, moving OBS / ledger / anchors / keys into `data_dir`, re-signing the demo configs,
  `startup.refused` for failed isolation: **T68**. Network / firewall rules: decided after T67.
- Any change to `m2k/kernel.py`, `m2k/trust.py`, `m2k/obs.py`, `m2k/live.py`, workflows or `pyproject.toml`.
- Running `install` against the owner's real folders (the owner decides when, after the merge).

## Known limits (write them in the README section, with a short Chinese "如何复原" block)
- An administrator can bypass any ACL; `owner_user` is trusted by design.
- `restore` brings back exactly what was saved; files created inside a managed folder after the install keep
  inheriting from the restored parent.
- New accounts' profile folders (`C:\Users\<name>`) are not removed by the restore script.
- The backup folder holds ACL copies only (no secrets); keep it as long as the isolation is installed.

## After the merge (reviewer, not this card)
- With the owner present: back up and run `install` on the real config (`<work-root>\…` folders, real account names);
  Codex runs `check` and posts it. If `CodexSandboxOffline` must keep reading the code, list it in `code_readers`.
- Then T77 starts with a feasibility test on the real accounts: can a scheduled task running as `m2k-svc` (session
  0, non-interactive) start a process as `m2k-agent` with `CreateProcessWithLogonW` and put it in a Job Object?

## Review notes (written by reviewer)
- 卡内澄清（2026-10-27，写卡人）：未提权令牌没有 SeRestorePrivilege，`icacls /restore` 返回 1300。真实 Windows 测试拆为两项：ACL 应用与继承断言不需提权；save→apply→restore 逐字节一致只在提权时运行（GitHub Windows 跑机与用户协助验收覆盖）。
- Claude 抽查（2026-10-27）：install 未保护配置文件本身；C:\ 下新建目录默认对 Authenticated Users 可修改，验收配置会让 install 收尾 check 必然失败。补预检：配置文件须位于 code_dir 或 protected 内，否则退出 2；验收配置移入 code\。
- 第一轮复审（2026-10-27，6a3b76f）：GLM GO；Codex 复审 FAIL：①已存在的 backup_root 不收紧，复原脚本可被低权限改写后由管理员执行；②backup_root 可位于受管目录内，递归 reset 会破坏其保护（GLM 也提到）；③密码内嵌 NUL 可绕过长度检查。修复：三个改动命令总是收紧 backup_root，uninstall 执行前用 Authz 核验备份与脚本不可被非管理员写；backup_root 与受管目录双向不重叠；拒绝含 NUL 的密码。备份根先于 /save 收紧属卡第 1 步允许的顺序。
- 第二轮复审（2026-10-27，fd41fb8）：GLM GO；Codex 复审确认第一轮三项已修好（备份树仅余 owner/Administrators/SYSTEM，15 组卸载拒绝与 48 组路径重叠用例均通过），只剩一项阻断：所选备份子目录若为目录联接，核验的是外部目标，目标父目录可写时核验后仍可被替换。修复：卸载跳过并拒绝重解析点、要求所选备份直属于 backup_root；三个改动命令拒绝重解析点形式的 backup_root。（该复审会话在写结论时被 OpenAI 网络安全策略中断，结论由 Claude 据其过程记录整理。）
- 第三轮复审（2026-10-27，49b4116）：GLM GO，Codex 复审 PASS（812 项通过），Claude 抽查通过。用户协助验收（2026-10-27）：install 19/19 通过，grant 19/19，revoke 仅撤销的 agent_read 行不通过（预期），uninstall 两次 icacls /restore 退出码 0、DELETE 删除两个测试账号；发现 uninstall 转印复原脚本输出时中文乱码、提示顺序错乱，改为子进程直接继承控制台；revoke 收尾提示改为说明预期结果。
- 合并（2026-10-27）：用户验收后两处显示修复（uninstall 子进程直接继承控制台、revoke 预期结果提示）由便携回归覆盖，Claude 抽查通过，GLM GO，CI 全绿；squash 合并 68a1e3e。这两处修复尚未在管理员终端人工复测，首次对真实配置安装时确认。非阻断留待以后：_plan 以元组打印 DACL；未用常量 MODIFY；备份内各 .acl 文件未逐个核验；uninstall 无 m2k 层 YES（由脚本确认）；revoke 对已不存在的历史路径会失败。

## Handoff note (written by implementer, ≤ 10 lines)
- 本轮对照 49b4116：交互子进程直接继承 stdin/stdout/stderr，日志 output 为指定占位说明；revoke 仅主动授权行失败时说明预期结果，其他失败保留原提示。
- 指定 Python 3.13.11 全套 816 项 OK（跳过 12），零失败、零错误；三个 Windows 旧失败未复现；Python 3.11 未实跑。
- 用户验收（2026-10-27）：115824 备份对应三次密码不一致、建号前停止且未改受管目录；120022 install 通过 19、不通过 0、信息 0，完整表已读取 C:\m2k-t76-accept\backup\20261027-120022\after-check.txt。
- 用户 grant 19/19；revoke 只有 extra 的 agent_read 行不可读、exit 1（预期），旧“操作失败/部分更改”提示已修正。
- 用户 uninstall 选中 20261027-120022，code 3 个与 prot 8 个文件复原均 exit 0、失败数 0；输入 DELETE 删除两个测试账号。
- 第 3 步非提权核对：同一 cwd C:\m2k-t76-accept、同样 icacls <name> /save <file> /T /C，C:\m2k-t76-accept-after\ 的 prot/code/extra 与 C:\m2k-t76-accept-baseline\ 逐字节一致（1982/656/442 字节），无差异。
- net user 确认 m2k-t76-svc、m2k-t76-agent 均不存在（2221）；C:\Users\m2k-t76-svc、C:\Users\m2k-t76-agent 两目录均不存在。
- 新便携回归先复现管道输出与 revoke 提示问题，修后覆盖继承控制台、日志无输出、仅主动授权失败、混合失败及撤销命令失败；Windows 自建临时树 ACL/联接与脚本解析测试通过。
- card_check 通过，m2k/ 新增 426/450 行；第三轮 Review notes 已原样追加；步骤 C:\m2k-t76-accept\给用户的步骤.txt（UTF-8 BOM）、验收配置与三份基线本轮未改。
- 本会话未提权、未创建账号、未改验收 ACL；提权还原测试本机跳过；用户第 2/3 步结果已核对，本轮显示修复尚未管理员终端人工复测；卡外疑问无，CI/新复审及合并验收待审阅者。
