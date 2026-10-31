"""Administrator-only Windows isolation setup and standalone ACL restoration."""
from __future__ import annotations

import ctypes as ct
import getpass
import json
import os
import stat
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from .isolation import DANGEROUS, GROUPS, DWORD, PTR, WindowsBackend, _configuration, _ok, check

ADMIN, SYSTEM = "S-1-5-32-544", "S-1-5-18"
FULL, RX, MODIFY = "FA", "0x1200a9", "0x1301bf"


def _api(library, name, arguments, result=ct.c_int):
    function = getattr(ct.WinDLL(library, use_last_error=True), name)
    function.argtypes, function.restype = arguments, result
    return function


def _is_reparse(path):
    if os.name != "nt":
        return os.path.islink(path)
    try:
        return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False


class System(WindowsBackend):
    """All machine actions, including input and file writes, are injectable."""
    windows = sys.platform == "win32"
    log = None
    emit, readline, password = staticmethod(print), staticmethod(input), staticmethod(getpass.getpass)
    is_reparse = staticmethod(_is_reparse)

    def elevated(self):
        token, elevation, size = PTR(), DWORD(), DWORD()
        process = _api("kernel32", "GetCurrentProcess", [], PTR)()
        _ok(_api("advapi32", "OpenProcessToken", [PTR, DWORD, PTR])(process, 8, ct.byref(token)))
        try:
            _ok(_api("advapi32", "GetTokenInformation", [PTR, DWORD, PTR, DWORD, PTR])(
                token, 20, ct.byref(elevation), ct.sizeof(elevation), ct.byref(size)))
            return bool(elevation.value)
        finally:
            _api("kernel32", "CloseHandle", [PTR])(token)

    def account(self, name):
        principal = self.account_sid(name)
        if principal is None:
            return None
        size, text = DWORD(), PTR()
        self._info(principal.context, 1, 0, ct.byref(size), None)
        buffer = ct.create_string_buffer(size.value)
        _ok(self._info(principal.context, 1, size.value, ct.byref(size), buffer))
        _ok(_api("advapi32", "ConvertSidToStringSidW", [PTR, PTR])(PTR.from_buffer(buffer), ct.byref(text)))
        try:
            return {"sid": ct.wstring_at(text), "administrator": principal.administrator}
        finally:
            self._free(text)

    def backup_rights(self, name, path):
        principal = self.sid(name) if name in {sid for _, sid in GROUPS} else self.account_sid(name)
        return self.rights(principal, path) if principal is not None else None

    def volume(self, path):
        root, filesystem = ct.create_unicode_buffer(32768), ct.create_unicode_buffer(256)
        _ok(_api("kernel32", "GetVolumePathNameW", [ct.c_wchar_p, PTR, DWORD])(str(path), root, len(root)))
        _ok(_api("kernel32", "GetVolumeInformationW", [ct.c_wchar_p, PTR, DWORD, PTR, PTR, PTR, PTR, DWORD])(
            root.value, None, 0, None, None, None, filesystem, len(filesystem)))
        return filesystem.value

    def roots(self):
        import winreg
        profiles = []
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList") as key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                with winreg.OpenKey(key, winreg.EnumKey(key, index)) as profile:
                    profiles.append(Path(os.path.expandvars(winreg.QueryValueEx(profile, "ProfileImagePath")[0])).resolve())
        return Path(os.environ["SystemRoot"]).resolve(), [Path("C:/Users"), *profiles]

    def mkdir(self, path):
        path.mkdir(parents=True, exist_ok=True)

    def write(self, path, text, encoding="utf-8", append=False):
        with path.open("a" if append else "w", encoding=encoding) as stream:
            stream.write(text)

    def run(self, arguments, cwd=None, interactive=False):
        arguments = list(map(str, arguments))
        if interactive:
            result = subprocess.run(arguments, cwd=cwd)
            output = "(交互输出直接显示在控制台，未记录)"
        else:
            result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, encoding="oem", errors="replace")
            output = result.stdout + result.stderr
        if self.log:
            self.write(self.log, json.dumps({"command": arguments, "cwd": str(cwd), "exit": result.returncode,
                                            "output": output}, ensure_ascii=False) + "\n", append=True)
        if result.returncode and not interactive:
            raise OSError(f"命令失败（退出码 {result.returncode}）：{arguments!r}\n{output}")
        return result.returncode

    def acl(self, path, entries, recursive=True):
        descriptor, dacl, present, defaulted = PTR(), PTR(), ct.c_int(), ct.c_int()
        sddl = "D:P" + "".join(f"(A;OICI;{right};;;{sid})" for sid, right in reversed(dict(reversed(entries)).items()))
        _ok(_api("advapi32", "ConvertStringSecurityDescriptorToSecurityDescriptorW", [ct.c_wchar_p, DWORD, PTR, PTR])(
            sddl, 1, ct.byref(descriptor), None))
        try:
            _ok(_api("advapi32", "GetSecurityDescriptorDacl", [PTR, PTR, PTR, PTR])(
                descriptor, ct.byref(present), ct.byref(dacl), ct.byref(defaulted)))
            error = _api("advapi32", "SetNamedSecurityInfoW", [ct.c_wchar_p, DWORD, DWORD, PTR, PTR, PTR, PTR], DWORD)(
                str(path), 1, 0x80000004, None, None, dacl, None)
            if error:
                raise OSError(f"设置 ACL 失败：{path}；{ct.WinError(error)}")
        finally:
            self._free(descriptor)
        if recursive and any(path.iterdir()):
            self.run(["icacls", path / "*", "/reset", "/T", "/C", "/Q"])

    def create_account(self, name, password, comment):
        class UserInfo(ct.Structure):
            _fields_ = [("name", ct.c_wchar_p), ("password", PTR), ("age", DWORD), ("privilege", DWORD),
                        ("home", ct.c_wchar_p), ("comment", ct.c_wchar_p), ("flags", DWORD), ("script", ct.c_wchar_p)]
        info = UserInfo(name, ct.cast(password, PTR), 0, 1, None, comment, 0x10001, None)
        error = _api("netapi32", "NetUserAdd", [ct.c_wchar_p, DWORD, PTR, PTR], DWORD)(None, 1, ct.byref(info), None)
        if error:
            raise OSError(f"NetUserAdd {name} 失败，错误码 {error}")

    def check(self, config):
        return check(config)  # Fresh Authz contexts after account and group changes.


def _quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def restore_script(manifest):
    saved = [entry for entry in manifest["folders"] if entry["existed_before"]]
    lines = ["#Requires -RunAsAdministrator", "$ErrorActionPreference = 'Stop'",
             f"# ACL 备份时间 {manifest['time']}；配置 {_quote(manifest['config'])}",
             "# 首次安装前备份" if manifest["first_install"] else "# 注意：此备份拍摄时隔离已安装", "$failed = 0"]
    for entry in saved:
        lines += [f"Write-Host {_quote(entry['path'] + ' <- ' + entry['acl'])}",
                  f"if (!(Test-Path -LiteralPath {_quote(entry['acl'])} -PathType Leaf)) {{ throw 'ACL 备份缺失' }}"]
    for entry in manifest["folders"]:
        if not entry["existed_before"]:
            lines.append(f"Write-Host {_quote('原先不存在，不会删除，可手动移除：' + entry['path'])}")
    lines.append("if ((Read-Host '输入 YES 开始还原') -cne 'YES') { Write-Host '已取消，未做任何更改'; exit 1 }")
    for entry in saved:
        lines += [f"& icacls {_quote(entry['parent'])} /restore {_quote(entry['acl'])} /C",
                  "$code = $LASTEXITCODE; Write-Host ('icacls 退出码：' + $code); if ($code -ne 0) { $failed++ }"]
    lines.append("Write-Host ('ACL 还原汇总，失败数：' + $failed)")
    if manifest["created"]:
        lines.append(f"if ((Read-Host {_quote('是否删除账号 ' + ', '.join(manifest['created']) + '？输入 DELETE 删除，直接回车跳过')}) -ceq 'DELETE') {{")
        for name in manifest["created"]:
            lines += [f"  try {{ Remove-LocalUser -Name {_quote(name)} }} catch {{ Write-Host $_; $failed++ }}",
                      f"  Write-Host {_quote('账号的个人目录仍保留：C:' + chr(92) + 'Users' + chr(92) + name)}"]
        lines.append("}")
    return "\n".join([*lines, "if ($failed -ne 0) { exit 1 }; exit 0", ""])


def _grants(cfg):
    return [(path, right) for key, right in (("agent_read", "RX"), ("agent_write", "M")) for path in cfg[key]]


def _targets(cfg, identities):
    def sid(name):
        return identities[name]["sid"] if identities[name] else f"<{name} 创建后查询 SID>"
    common = [(sid(cfg["owner_user"]), FULL), (ADMIN, FULL), (SYSTEM, FULL)]
    svc, agent = sid(cfg["svc_user"]), sid(cfg["agent_user"])
    return [(cfg["tools_dir"], [*common, (svc, RX), (agent, RX)]),
            (cfg["data_dir"], [(ADMIN, FULL), (SYSTEM, FULL), (svc, FULL)]),
            (cfg["code_dir"], [*common, (svc, RX), *[(sid(n), RX) for n in cfg["code_readers"]]]),
            *[(p, common) for p in cfg["protected"]]]


def _plan(system, cfg, action, managed, identities, paths, backup):
    system.emit(f"计划 isolation {action}；备份 / 日志目录：{cfg['backup_root']}")
    system.emit(f"创建缺失的备份根，并递归收紧 DACL、清除子项显式 ACE：*{identities[cfg['owner_user']]['sid']}、*{ADMIN}、*{SYSTEM} 完全控制")
    system.emit(f"SetNamedSecurityInfoW {_quote(cfg['backup_root'])}；icacls {_quote(cfg['backup_root'] / '*')} /reset /T /C /Q（有子项时）")
    if action == "install":
        for index, path in enumerate(managed):
            if path.exists():
                system.emit(f"icacls {_quote(path.name)} /save {_quote(backup / f'{index}.acl')} /T /C （工作目录 {_quote(path.parent)}）")
        system.emit(f"先在 {backup} 写 before-check.txt、manifest.json、install.log、restore-acl.ps1（UTF-8 BOM），再改账号 / 管理目录")
        for key in ("svc_user", "agent_user"):
            system.emit(f"账号 {cfg[key]}：" + ("已存在，仅核验非管理员" if identities[cfg[key]] else "NetUserAdd 标准用户；隐藏输入密码两遍"))
        system.emit(f"创建目录：{cfg['tools_dir']}；{cfg['data_dir']} 及 secrets、ledger、queue/inbox、work、logs；FA=完全控制，0x1200a9=读取/执行")
        for path, entries in _targets(cfg, identities):
            system.emit(f"SetNamedSecurityInfoW {_quote(path)}：精确且受保护的 DACL {entries}；所有者不变")
            system.emit(f"icacls {_quote(path / '*')} /reset /T /C /Q （有子项时）")
        for suffix, right in (("ledger", "RX"), ("queue/inbox", "M")):
            system.emit(f"icacls {_quote(cfg['data_dir'] / suffix)} /grant *{identities[cfg['owner_user']]['sid']}:(OI)(CI){right}")
    for path, right in _grants(cfg) if action != "revoke" else [(p, None) for p in paths]:
        system.emit(f"icacls {_quote(path)} " + (f"/grant <{cfg['agent_user']} SID>:(OI)(CI){right}" if right else
                                                f"/remove:g *{identities[cfg['agent_user']]['sid']}"))
    system.emit("记录 grants.jsonl；最后运行 isolation check 并显示结果")


def _preflight(cfg, system, action, config):
    if system.is_reparse(cfg["backup_root"]):
        raise ValueError(f"备份路径含符号链接或目录联接，拒绝执行：{cfg['backup_root']}")
    backup_root = cfg["backup_root"].resolve()
    config = config.resolve()
    if action in ("install", "grant", "revoke") and not any(
            config.is_relative_to(root) for root in [cfg["code_dir"], *cfg["protected"]]):
        raise ValueError("isolation: 配置文件必须位于 code_dir 或 protected 目录内，"
                         f"否则 agent 账号可能改写它：{config}")
    managed = [cfg["tools_dir"], cfg["data_dir"], cfg["code_dir"], *cfg["protected"]]
    for path in managed:
        if path.is_relative_to(backup_root) or backup_root.is_relative_to(path):
            raise ValueError(f"isolation.backup_root: 不得与受管目录相等或嵌套：{cfg['backup_root']} / {path}")
    identities = {name: system.account(name) for name in dict.fromkeys(
        [cfg["owner_user"], cfg["svc_user"], cfg["agent_user"], *cfg["code_readers"]])}
    for key in ("owner_user", "code_readers"):
        for name in cfg[key] if isinstance(cfg[key], list) else [cfg[key]]:
            if identities[name] is None:
                raise ValueError(f"isolation.{key}: 账号不存在：{name}")
    if action == "install":
        windows, profiles = system.roots()
        for path in managed:
            if (path == path.parent or path.is_relative_to(windows) or path in profiles or
                    any(path.is_relative_to(other) or other.is_relative_to(path) for other in managed if other is not path)):
                raise ValueError(f"isolation: 拒绝管理此路径（系统目录、备份目录或目录重叠）：{path}")
            if (path.exists() or path not in (cfg["data_dir"], cfg["tools_dir"])) and system.volume(path) != "NTFS":
                raise ValueError(f"isolation: 目录必须位于 NTFS：{path}")
    history = cfg["backup_root"] / "grants.jsonl"
    paths = {p for p, _ in _grants(cfg)}
    if action == "revoke" and history.exists():
        paths.update(Path(json.loads(line)["path"]).resolve() for line in history.read_text(encoding="utf-8").splitlines())
    for path in paths:
        if any(path.is_relative_to(root) for root in [*managed, backup_root]):
            raise ValueError(f"isolation.agent_read / agent_write: 禁止主动授权：{path}")
        if action in ("install", "grant") and not path.exists():
            raise ValueError(f"isolation.agent_read / agent_write: 路径不存在：{path}")
    if action in ("grant", "revoke") and identities[cfg["agent_user"]] is None:
        raise ValueError("isolation.agent_user: 账号不存在，请先 install")
    return managed, identities, sorted(paths)


def _accounts(system, cfg, identities, manifest, persist):
    for key, comment in (("svc_user", "m2k service account"), ("agent_user", "m2k agent account")):
        name = cfg[key]
        if identities[name]:
            if identities[name]["administrator"]:
                raise OSError(f"账号 {name} 已是管理员，停止安装")
            continue
        for attempt in range(3):
            passwords, buffers = [], []
            try:
                for prompt in (f"{name} 密码（至少 12 位）：", f"再次输入 {name} 密码："):
                    passwords.append(system.password(prompt))
                if any("\x00" in password for password in passwords):
                    system.emit("密码不能包含空字符")
                    continue
                if len(passwords[0]) < 12 or passwords[0] != passwords[1]:
                    system.emit("密码至少 12 位，且两次必须一致")
                    continue
                buffers.append(ct.create_unicode_buffer(passwords[0]))
                passwords.clear()
                system.create_account(name, buffers[0], comment)
                manifest["created"].append(name)
                persist()
                identities[name] = system.account(name)
                if identities[name] is None or identities[name]["administrator"]:
                    raise OSError(f"新账号 {name} 的 SID 或标准用户身份核验失败")
                manifest["sids"][name] = identities[name]["sid"]
                persist()
                break
            finally:
                passwords.clear()
                for buffer in buffers:
                    ct.memset(ct.addressof(buffer), 0, ct.sizeof(buffer))
        else:
            raise OSError(f"{name} 密码连续三次不符合要求，停止安装")


def _apply(system, cfg, config, action, managed, identities, paths, backup=None):
    restore = None
    try:
        owner = identities[cfg["owner_user"]]["sid"]
        common = [(owner, FULL), (ADMIN, FULL), (SYSTEM, FULL)]
        root = cfg["backup_root"]
        if action == "uninstall":
            system.log = backup / "install.log"
            return system.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               backup / "restore-acl.ps1"], interactive=True), f"还原脚本：{backup / 'restore-acl.ps1'}"
        if not root.exists():
            system.mkdir(root)
        system.log = root / "install.log"
        system.acl(root, common)
        if action == "install":
            system.mkdir(backup)
            system.log = backup / "install.log"
            system.write(system.log, "")
            manifest = {"time": datetime.now().isoformat(), "config": str(config.resolve()), "first_install": all(
                identities[cfg[k]] is None for k in ("svc_user", "agent_user")), "created": [],
                "accounts_existed": {cfg[k]: identities[cfg[k]] is not None for k in ("svc_user", "agent_user")},
                "sids": {ADMIN: ADMIN, SYSTEM: SYSTEM, **{n: p["sid"] if p else None for n, p in identities.items()}}, "folders": []}
            for index, path in enumerate(managed):
                acl = backup / f"{index}.acl"
                manifest["folders"].append({"path": str(path), "parent": str(path.parent),
                                            "existed_before": path.exists(), "acl": str(acl)})
                if path.exists():
                    system.run(["icacls", path.name, "/save", acl, "/T", "/C"], cwd=path.parent)
            system.write(backup / "before-check.txt", cfg["before_check"])
            def persist():
                system.write(backup / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                system.write(backup / "restore-acl.ps1", restore_script(manifest), encoding="utf-8-sig")
            persist()
            restore = backup / "restore-acl.ps1"
            _accounts(system, cfg, identities, manifest, persist)
            for suffix in ("", "secrets", "ledger", "queue/inbox", "work", "logs"):
                system.mkdir(cfg["data_dir"] / suffix)
            system.mkdir(cfg["tools_dir"])
            for path, entries in _targets(cfg, identities):
                system.acl(path, entries)
            for suffix, right in (("ledger", "RX"), ("queue/inbox", "M")):
                system.run(["icacls", cfg["data_dir"] / suffix, "/grant", f"*{owner}:(OI)(CI){right}"])
        else:
            backups = sorted(root.glob("*/install.log"), key=lambda p: p.stat().st_mtime_ns)
            system.log = backups[-1] if backups else root / "install.log"
        agent = identities[cfg["agent_user"]]["sid"]
        for path, right in _grants(cfg) if action != "revoke" else [(p, None) for p in paths]:
            system.run(["icacls", path, "/grant", f"*{agent}:(OI)(CI){right}"] if right else
                       ["icacls", path, "/remove:g", f"*{agent}"])
            system.write(root / "grants.jsonl", json.dumps({"time": datetime.now().isoformat(), "path": str(path),
                                                           "right": right}, ensure_ascii=False) + "\n", append=True)
        code, report = system.check(config)
        if action == "install":
            system.write(backup / "after-check.txt", report)
        if code:
            if action == "revoke":
                failed = [row[0].split("（", 1)[0] for line in report.splitlines()
                          if len(row := line.split("\t", 2)) == 3 and row[1] == "不通过"]
                if failed and all(label in ("agent_read 主动授权", "agent_write 主动授权") for label in failed):
                    return 1, ("撤销已完成；配置中仍列有这些授权路径，check 的主动授权行因此不通过（预期）。"
                               "如不再需要，请从配置中删除这些路径。\n" + report)
            raise OSError("后续 isolation check 未通过\n" + report)
        return 0, report + (f"\n备份：{backup}\n还原脚本：{restore}" if restore else "")
    except (OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        hint = f"\n管理员手动还原命令：powershell.exe -NoProfile -ExecutionPolicy Bypass -File {_quote(restore)}" if restore else ""
        state = "受管目录尚未改动，仅创建/收紧了备份目录" if action == "install" and restore is None else "更改可能只完成了一部分"
        return 1, f"操作失败：{error}；{state}。{hint}"


def execute(config, action, system=None):
    """Return (exit code, final report); plans and input use the system object."""
    if system is None and sys.platform != "win32":
        return 2, f"m2k isolation {action} 仅支持 Windows"
    try:
        with ExitStack() as cleanup:
            system = system or cleanup.enter_context(System())
            if not system.windows:
                return 2, f"m2k isolation {action} 仅支持 Windows"
            if not system.elevated():
                return 2, "请在“以管理员身份运行”的终端中执行"
            config = Path(config)
            cfg = _configuration(config)
            managed, identities, paths = _preflight(cfg, system, action, config)
            backup = None
            if action == "uninstall":
                candidates = [p for p in cfg["backup_root"].glob("*/manifest.json")
                              if not system.is_reparse(p.parent) and
                              json.loads(p.read_text(encoding="utf-8")).get("first_install") is True]
                if not candidates:
                    raise ValueError("没有 first_install: true 的首次安装备份")
                backup = max(candidates, key=lambda p: p.stat().st_mtime_ns).parent
                for path in (cfg["backup_root"], backup, backup / "restore-acl.ps1"):
                    if system.is_reparse(path) or (path == backup and backup.resolve().parent != cfg["backup_root"].resolve()):
                        raise ValueError(f"备份路径含符号链接或目录联接，拒绝执行：{path}")
                    for name in (cfg["svc_user"], cfg["agent_user"], *(sid for _, sid in GROUPS)):
                        rights = system.backup_rights(name, path)
                        if rights is not None and (rights.unmapped_generic or rights.mask & DANGEROUS[1]):
                            raise ValueError(f"备份目录或复原脚本可被非管理员改写，拒绝执行：{path}")
                system.emit(f"计划：使用备份 {backup}；powershell.exe -NoProfile -ExecutionPolicy Bypass -File {_quote(backup / 'restore-acl.ps1')}")
            else:
                if action == "install":
                    backup = cfg["backup_root"] / datetime.now().strftime("%Y%m%d-%H%M%S")
                    while backup.exists():
                        backup = backup.with_name(backup.name + "-1")
                    _, cfg["before_check"] = system.check(config)
                _plan(system, cfg, action, managed, identities, paths, backup)
                try:
                    answer = system.readline("输入 YES 执行：")
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer != "YES":
                    return 1, "已取消，未做任何更改"
            return _apply(system, cfg, config, action, managed, identities, paths, backup)
    except (OSError, ValueError, KeyError) as error:
        return 2, f"配置 / 预检错误：{error}"
