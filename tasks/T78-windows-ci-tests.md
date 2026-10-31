# T78: main Windows CI green again (tests only) + find out whether `icacls /restore` widens legacy ACLs

- **Assignee:** Codex (GLM-5.3 GO required)
- **Branch:** `task/T78-windows-ci-tests` (already contains this card; start from it)
- **Line budget:** ≤ 0 added lines in `m2k/` (this card changes tests only; **do not touch `m2k/`**)
- **Files you may change:** `tests/test_trust_cli.py`, `tests/test_live.py`, `tests/test_fingerprint.py`,
  `tests/test_isolation_install.py`, `tasks/README.md`, this card (Handoff note only)
- **Status:** DONE (merged #58, 512b4e0)

## Goal
Since T71 the `test (windows-latest, 3.13)` job on main has been red; after T76 (68a1e3e) it has 4 failures.
Three are old: the runner's temp directory is the 8.3 short name `C:\Users\RUNNER~1\…`, the code under test calls
`.resolve()` and gets the long name `runneradmin`, so path assertions do not match. The fourth is new:
`tests.test_isolation_install.WindowsAclTest.test_owned_tree_byte_identical_restore` (it runs only with an elevated
token, which the runner has). After `icacls /restore` the `/save` text starts with `D:AI(...)` and has extra
inherited ACEs, while the original was `D:(...)` (no auto-inherit flag) with fewer entries. Likely cause: the runner's
temp directories carry *legacy* ACLs (no `SE_DACL_AUTO_INHERITED`, entries copied as explicit), and the restore
recomputes inheritance from the parent the modern way. Fix the tests so that they test what T76 promises on normal
directories, and **find out** whether restoring a legacy-ACL directory can grant more than it had before (that would
matter for the owner's real `install` / `uninstall`).

## Required behaviour
1. **Three short-name failures** (`test_trust_cli`
   `test_release_version_uses_public_json_reader_and_preserves_private_alias`, `test_live`
   `test_cli_resolves_config_relative_ledger_without_constructing_it`, `test_fingerprint`
   `test_cli_hashes_config_and_trust_paths_before_opening_ledger`): make the tests' temporary roots canonical
   (e.g. `Path(tmp.name).resolve()` in `setUp`) or compare resolved paths. Change only test setup / assertions.
   Re-run the whole module each time, because a resolved root can change other assertions in the same file.
   - Try to reproduce first: the owner's own temp path has no short names. Check `fsutil 8dot3name query G:` /
     `C:`; if a volume generates short names, create a long-named directory under `<worktrees>\t78-tmp\`
     (or, if only C: generates them, `C:\m2k-t78-shortname\`, deleted afterwards), get its short form
     (`cmd /c for %I in ("<dir>") do @echo %~sI`), point `TEMP`/`TMP` at the short form and run the three tests
     before and after the fix. If nothing generates short names, say so and fix by reasoning.
2. **`test_owned_tree_byte_identical_restore`**: before saving the baseline, put the self-made tree into the state
   a real directory has after normal Windows use: top level set with the real `System.acl()` (protected DACL,
   children reset to inherit, `AI` flag present), then create the explicit-Everyone child and the
   inheritance-disabled child as today, then `/save` the baseline. The rest of the test (apply → `/restore` →
   `/save` → byte-identical) stays. On mismatch the failure message must print both `/save` texts in full,
   decoded as UTF-16 (not bytes).
3. **Investigation test** (new, Windows + elevated only, same skip condition as item 2), e.g.
   `test_legacy_acl_restore_inheritance`:
   - parent = the test's own temp root, set with `System.acl()` to owner/SYSTEM/Administrators full **and
     Everyone (`S-1-1-0`) RX inheritable**;
   - `tree` and one subdirectory under it given a **legacy** DACL that has no Everyone entry and no `AI`/`P` flag,
     written with the legacy API `SetFileSecurityW` (DACL_SECURITY_INFORMATION) from SDDL like
     `D:(A;OICI;FA;;;<owner>)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)` (ctypes in the test file; no `m2k/` change);
   - assert the baseline really is legacy (`/save` line for `tree` starts with `D:(`) and Everyone's effective mask
     on `tree` (via `WindowsBackend.rights`) is 0;
   - `/save` baseline → `System.acl(tree, [(owner, FULL)])` → exactly the restore command `restore_script()` emits
     → measure again;
   - print **one** line to stdout starting with `T78-LEGACY:` containing the before/after `/save` lines for `tree`
     and its subdirectory and Everyone's masks before/after (this line is read from the CI log after the merge);
   - assert only what must hold either way: restore exit code 0 and every original explicit ACE still present.
     **Do not** assert whether Everyone gained access — that is the open question.
4. **Local evidence without elevation** (not committed; report only). Codex cannot pass UAC, so `icacls /restore`
   cannot run locally. In a scratch folder `<worktrees>\t78-probe\` build the same parent/legacy-child
   setup and call `SetNamedSecurityInfoW` with the legacy DACL **unprotected** (what a restore of a non-`P` DACL
   amounts to). Report Everyone's mask before/after and the resulting SDDL. Delete the probe folder afterwards.
5. **Scan of the owner's real directories** (read-only; report only, nothing changed): for `G:\`, `<work-root>`,
   `C:\` (the items themselves) and recursively for `<repo>`, `<dev-keys>`, `<demo-dir>`,
   `<old-project-private>`, count items whose DACL is **neither protected nor auto-inherited** (control
   bits `SE_DACL_PROTECTED` 0x1000 and `SE_DACL_AUTO_INHERITED` 0x0400 both clear), list up to 20 examples, and say
   whether `C:\m2k-acl-backup`, `<svc-data>`, `C:\m2k-tools` exist. Access errors are counted, not fatal.
6. `tasks/README.md`: add the T78 row.

## Out of scope
- Any change to `m2k/`, workflows or other tests. If the investigation shows a real widening risk for the owner's
  directories, **stop and report** — a preflight or warning in `install` / `restore-acl.ps1` is a separate decision
  for the owner.

## Acceptance
```
<python> -m unittest tests.test_trust_cli tests.test_live tests.test_fingerprint tests.test_isolation_install -v
<python> -m unittest discover -s tests -t .
```
(locally the elevated-only tests are skipped). After the merge, the reviewer checks that main's
`test (windows-latest, 3.13)` is green and reads the `T78-LEGACY:` line from its log.

## Review notes (written by reviewer)

- 卡内调整（Claude，第一轮）：第 3 项的基线前置条件（`D:(`、Everyone 为 0）改为“不满足时打印 `T78-LEGACY: baseline not legacy …` 并 skip”，不再断言。原因：Codex 本机预演时 `/save` 顶层为 `D:P`，提权跑机上若同样不是老式 ACL，硬断言会让 main 再次变红；这个测试只负责记录。
- GLM-5.3：GO（非阻断：icacls /save 若带 UTF-16 BOM，字典键会带 `\ufeff`——Codex 本机已用同一 `save()` 读到 `tree` 键，未改；restore 命令与 `restore_script()` 一致性、Everyone RX 是否可继承等为提示）。
- Codex 独立复审（gpt-6-astra）：PASS。817 项 0 失败、跳过 13；短名条件下修前 3/3 失败、修后三模块 59/59；legacy 测试结果如何都只记录不变红。
- 真实目录只读核算（复原后权限 = 显式 ∪ 父目录可继承）：<repo>、<dev-keys>、<demo-dir>、<old-project-private> 共 4006 项，老式 DACL 3974 项差集为 0；保留保护位后预测新增权限项为 0。结论：对用户现有目录，install → uninstall 复原不会让权限变宽。一般情况（老式项比父目录窄）复原会补进父目录的继承权限，留作遗留项（是否在 install 预检提示由用户决定）。
- Claude 抽查 tests/test_live.py:134、tests/test_isolation_install.py:796、:867：PASS，2026-10-27 合并。
- 合并后 main Windows CI（512b4e0，job 122055883712）：全绿。Ran 817 tests in 597.378s / OK (skipped=10)。日志：T78-LEGACY: {"before": {"tree": "D:(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", "tree\\sub": "D:(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"}, "after": {"tree": "D:AI(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIID;FA;;;LA)(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;0x1200a9;;;WD)", "tree\\sub": "D:AI(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIID;FA;;;LA)(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;0x1200a9;;;WD)"}, "everyone_before": {"tree": "0x00000000", "tree\\sub": "0x00000000"}, "everyone_after": {"tree": "0x001200A9", "tree\\sub": "0x001200A9"}}

## Handoff note (written by implementer, ≤ 10 lines)

- 对照为未修改的 205d485（四模块及 m2k 与 origin/main 一致）；仅改四个测试模块、任务索引及本节，Status 未改，m2k/ 零变更。
- 短名：fsutil 查询 G:/C: 均拒绝访问；G: 探针未生成短名，C: 生成 C:\M2K-T7~1\LONGDI~1；原版三例全部失败，修后三个完整模块 59/59 通过，探针目录已删。
- 正常树先用 System.acl 保护顶层并重置子项继承，字节差异附 UTF-16LE 全文；新增同一管理员跳过条件的老式 ACL 调查测试和单行 T78-LEGACY 输出。
- Python 3.13.11 的两条 Acceptance：定向 104 项、失败 0、跳过 2；全套 817 项、失败 0、跳过 13。
- 第4项：SetFileSecurityW 原始树/子树为 D:(...)、Everyone=0；显式解除保护的 SetNamedSecurityInfoW 复算后均为 D:AI(...)、Everyone=0x001200A9；t78-probe 已删除。
- 本机非管理员 /save 将顶层保存为 D:P(...)，第3项 D:( 前置在此预演不满足；按卡保留原断言，管理员 CI、T78-LEGACY 及实际 icacls /restore 尚待核验。
- 第5项扫描快照（老式/总项）：G:\ 1/1、<work-root> 1/1、C:\ 1/1；m2-kernel 3797/3811、<dev-keys> 40/40、<demo-dir> 99/104、<old-project-private> 0/13；访问错误均为0。
- 老式项示例：m2-kernel\trust、<dev-keys>\root.pem、<demo-dir>\t71-shots；C:\m2k-acl-backup、<svc-data>、C:\m2k-tools 均不存在；真实目录只读，未执行还原。
- 风险：已实证解除保护后可增加继承权限；真实老式项只有与父项一致的默认目录/文件 ACL 两类，尚未实证其还原扩权；未改产品代码，worktree 保留，CI/GLM-5.3 待审。
