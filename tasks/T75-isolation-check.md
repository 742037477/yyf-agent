# T75: `m2k isolation check` — a read-only report of what two Windows accounts can read and write (isolation line, card 1 of 2; formerly "T66a")

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T75-isolation-check` (start from the latest main)
- **Line budget:** ≤ 340 added lines in `m2k/` in total (tests not counted)
- **Files you may change:** `m2k/isolation.py` (new), `m2k/cli.py`, `tests/test_isolation.py` (new), `m2k.example.toml`, `README.md`, `tasks/README.md`
- **Status:** DONE (merged #56, f5886d4)

## Goal
The owner decided (2026-10-17/18/19, see `docs/ARCHITECTURE.md`) to run governed agents under a separate standard
Windows account, `m2k-agent`, while the framework's own data (Root public key, OBS key and log, ledger, anchors,
model API keys) belongs to a second standard account, `m2k-svc`. The separation is enforced by NTFS permissions
(ACLs). Changing ACLs comes later (card 2, T76, an administrator-run install command with a backup and an
administrator-run restore script). **This card only looks:** it adds a command that reports, for each relevant
folder, what each of the two accounts can effectively read and write, so the owner can see the real state of the
machine before anything is changed, and so later cards (T76 install, T68 `m2k run` integration) have a checker to
call.

Nothing in this card creates accounts, changes permissions, writes the ledger or touches `m2k run`.

## Configuration (`[isolation]`, read only by this command for now)
```toml
[isolation]
svc_user    = "m2k-svc"
agent_user  = "m2k-agent"
data_dir    = '<svc-data>'          # framework data, owned by m2k-svc
tools_dir   = 'C:\m2k-tools'           # shared read-only Python, node and agent CLIs
protected   = ['<dev-keys>', '<old-project-private>', '<demo-dir>']
agent_read  = []                       # folders the owner deliberately lets m2k-agent read (optional)
agent_write = []                       # folders the owner deliberately lets m2k-agent write (optional)
```
Relative paths resolve against the config folder, as elsewhere. Missing `[isolation]`, a missing required key, a
wrong type, or a path in `agent_read` / `agent_write` that is equal to or inside `data_dir` or a `protected` folder
is a configuration error (exit 2, message names the key).

## Required behaviour
1. New subcommand: `m2k --config <file> isolation check`. Lives in a new module `m2k/isolation.py`; `cli.py` only
   wires the parser and prints.
2. **Effective rights, not the ACL text.** Decide "can read" / "can write" for an account on a path with the Windows
   Authz API through `ctypes` (no pywin32, no new dependency): look up the account SID (`LookupAccountNameW`), build a
   client context with `AuthzInitializeContextFromSid` (this includes the groups the account belongs to, such as
   Users, Authenticated Users and Everyone), read the path's security descriptor (`GetNamedSecurityInfoW`, owner +
   group + DACL), and call `AuthzAccessCheck` with `FILE_GENERIC_READ` and with `FILE_GENERIC_WRITE` separately.
   Keep all Windows calls behind one small backend object with two methods (for example `sid(name)` and
   `rights(sid, path) -> (can_read, can_write)`) so tests can inject a fake.
3. **Checks** (each one row of the report):
   - both accounts exist; neither is a member of the local Administrators group (S-1-5-32-544);
   - `agent_user` cannot read `data_dir`, nor any folder in `protected` (unless the same path is in `agent_read`,
     which the config check already forbids for protected paths);
   - `agent_user` cannot write the framework code folder (the folder that contains the `m2k` package), the config
     file being used, or `tools_dir`;
   - `agent_user` can read and execute `tools_dir`;
   - `svc_user` can read the framework code folder and `tools_dir`, cannot write the framework code folder, and can
     read and write `data_dir`;
   - the current interactive user (the one running the command) can still read and write the framework code folder,
     checked against that user's own SID explicitly (not via Administrators), so that UAC-filtered tokens are not
     locked out later;
   - every `agent_read` path is readable and every `agent_write` path is writable by `agent_user` (deliberate
     grants, reported as such, not as failures of isolation).
4. **Works before the accounts exist.** If an account is missing, its rows say so (FAIL), and the command
   additionally reports, for `data_dir` and every `protected` folder, whether the built-in groups Users
   (S-1-5-32-545), Authenticated Users (S-1-5-11) and Everyone (S-1-1-0) can read or write it. These extra rows are
   informational (they never change the exit code) and let the owner see today's G: drive permissions.
5. **Output:** a plain table in Chinese, one row per check: 检查项 / 结果（通过、不通过、信息） / 路径. A missing path is
   reported as "路径不存在" on that row (FAIL for rows that require access, PASS for rows that require no access, with
   a note). End with one summary line.
6. **Exit codes:** 0 when every check passes, 1 when any check fails, 2 for a configuration error or when not on
   Windows (message: `m2k isolation check 仅支持 Windows`).
7. **Strictly read-only.** The command never changes a permission, an owner, an account, a file or the ledger, and
   never opens a ledger. Request only read access to security information (`READ_CONTROL`); do not enable
   privileges such as `SeSecurityPrivilege` or `SeBackupPrivilege`.

## Tests (`tests/test_isolation.py`)
- **Portable tests (run on the Linux PR job):** a fake backend (accounts → SID, `(sid, path)` → rights, a set of
  administrator SIDs) drives every row of rule 3–5: all-pass → exit 0; each single wrong permission → exit 1 with
  that row FAIL; missing account → FAIL rows plus the informational group rows, which do not change the exit code;
  missing path; each configuration error → exit 2 naming the key (including an `agent_read` path inside a
  `protected` folder); non-Windows → exit 2 with the message from rule 6 (patch the platform check).
- **Read-only proof:** the fake backend records every call; assert it only ever received lookups and rights
  queries, and that no file under a temporary config folder changed (bytes and mtime) after the command.
- **Real Windows test** (`@unittest.skipUnless(os.name == "nt", ...)`, so it runs only in the Windows job after the
  merge and on the owner's machine): create a temporary file, read its rights through the real backend for the
  current user's SID (expect read and write) and for Everyone (compare with what `icacls` prints for that file, or
  make the expectation independent of the machine by first checking the DACL contains no Everyone entry and then
  expecting no access). Do not create accounts or change any ACL in tests.
- Before writing, grep the existing tests for anything that enumerates the CLI subcommands or parses
  `m2k.example.toml`; none is expected, but if one exists add it to this card's Files list in the PR description
  and change only that assertion.

## Acceptance
```
python -m unittest discover -s tests -t .
```
Plus, on the owner's Windows machine before pushing (Codex runs it in a temporary worktree, not the main working
tree): write a throw-away config with the `[isolation]` example above in `%TEMP%`, run
`m2k --config <that file> isolation check`, and paste its full output into the handoff note (expected today: both
accounts missing → exit 1, plus informational rows for the G: folders). Confirm the command left the G: folders'
`icacls` output unchanged (run `icacls` before and after and compare).

## Out of scope (later cards)
- Creating accounts, the administrator install command, ACL backup and the administrator-run restore script,
  DPAPI-stored credentials, starting a process as `m2k-agent`, Job Objects, the queue and the scheduled task: **T76**.
- Calling this check from `m2k run`, `startup.refused` codes for failed isolation, moving OBS/ledger/anchors to
  `m2k-svc`: **T68**.
- Any change to `m2k/kernel.py`, `m2k/trust.py`, `m2k/obs.py`, other workflows or `pyproject.toml`.

## Known limits (write them in the README section for this command)
- An administrator can bypass any ACL; the owner's own administrator account (<owner-account>) is trusted by design.
- ACLs only govern files. Network access and other processes are not covered here (T76 uses a Job Object for
  processes; network stays governed by the contract and charter).
- The report is a snapshot: it is only as true as the moment it ran.

## After the merge (reviewer, not this card)
- Codex runs the command once on the owner's machine with the throw-away config and posts the output; it is the
  input for writing T76.

## Review notes (written by reviewer)

- 第一轮（2026-10-19）：GLM GO；Codex 复审 FAIL（配置账号写成裸 SID 时跳过组展开、管理员与存在检查）；Claude 抽查不通过（否定检查要求全位授予，部分写权限如 WRITE_DAC/DELETE 会误判“不可写”）。另将 GLM 提出的“ACE 中通用权限位未映射”升为阻断。三项一并修复，m2k/ 预算由 260 调至 340（写卡人决定）。
- 遗留非阻断：只查目录本身、不递归查子项（断开继承的子项可能漏查），留给 T76 安装后再议。
- 第二轮（2026-10-19，08b1d60）：Codex 复审 PASS（自建临时文件实测：只授予 WRITE_DATA / WRITE_DAC / DELETE 均报可写；NULL DACL 全可、空 DACL 全不可；两个裸 SID 反例退出 2）；GLM 前两次空回复，第三次 GO；Claude 抽查账号/内部探测拆分、DACL 扫描与 INHERIT_ONLY 过滤、MAXIMUM_ALLOWED 危险位判定通过。squash 合并 f5886d4。偏差说明：真实 Windows 测试会改它自己在 TemporaryDirectory 中新建文件的 DACL（通用位修复的证据），不碰任何已有文件。非阻断：DANGEROUS 无执行项（目前走不到）；首次 Windows main CI 注意 AuthzInitializeContextFromSid 参数布局。

## Handoff note (written by implementer, ≤ 10 lines)

- 实现：新增只读 Authz 后端、中文权限表、配置校验及 isolation check 接线；补示例、README 和任务索引；m2k/ 新增 247 行（预算 260）。
- 可信对照：origin/main 2340b445cfc8c1b0c26c2f0a888e3f5830853d52，修改前全套 741 项 OK（跳过 11）；只在 %TEMP%\t75 的 task/T75-isolation-check 实现，未动主工作区。
- 测试：<python> -m unittest discover -s tests -t . → 762 项 OK（跳过 11）；新增 21 项全通过（含 3 项真实 Windows 查询），3.11 语法和示例 TOML 检查通过。
- 实机命令（worktree 内）：<python> -B -m m2k --config <user-home>\AppData\Local\Temp\t75-isolation.toml isolation check → exit 1（两账号不存在，符合预期）；汇总：通过 2，不通过 18，信息 24；data_dir 不存在。
- 内置组信息：Authenticated Users 可读写 <dev-keys>、<demo-dir>；Users 可读不可写这两处；三内置组均不可读写 <old-project-private>，Everyone 也不可读写其余两处。
- 只读确认：分别在命令前后执行 icacls <dev-keys>、icacls <old-project-private>、icacls <demo-dir>，均 exit 0 且 stdout/stderr 字节完全一致；临时配置字节和 mtime 亦未变。
- 完整命令/输出（含前后 icacls）：<user-home>\AppData\Local\Temp\t75-check-output.txt；整个过程未修改 ACL、所有者或账号，未创建 m2k-svc / m2k-agent，检查命令未打开账本。
- 交接：按要求不创建 PR；待 Claude 开 PR、ci/card-scope 及 GLM-5.3 GO、最终审阅；卡状态仍为 OPEN。
- 2026-10-19 本轮返工（对照 aa13347）：配置账号改走独立账号方法并拒绝裸 SID；单次 MAXIMUM_ALLOWED 返回实际掩码，肯定检查要求全位、否定/信息检查识别任一危险位，可写备注附十六进制掩码；有效通用 ACE 使该路径否定检查不通过；配置错误前缀去重，m2k/ 新增 287 行（预算 340）。
- 本轮验证：全套 770 项 OK（跳过 11），隔离测试 29 项通过（含 4 项真实 Windows），3.11 语法通过；仅自建临时文件实验：落盘通用位已映射，原始 SD 被 Authz 忽略（掩码 0/错误 5），故兜底；实机 exit 1，汇总通过 2/不通过 18/信息 24，两账号不存在；三处真实目录 icacls 前后 stdout/stderr/退出码逐字节一致，临时配置字节/mtime 未变；输出覆盖写入 %TEMP%\t75-check-output.txt；未动主工作区、未建账号或修改真实目录 ACL，卡仍 OPEN，待 CI/复审。
