"""Read-only Windows Authz isolation checks; no ledger or permission changes."""
from __future__ import annotations

import ctypes as ct
import getpass
import sys
import tomllib
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

DWORD, PTR = ct.c_uint32, ct.c_void_p
PDWORD, PPTR = ct.POINTER(DWORD), ct.POINTER(PTR)
GROUPS = (("Users", "S-1-5-32-545"), ("Authenticated Users", "S-1-5-11"), ("Everyone", "S-1-1-0"))
REQUIRED = (0x120089, 0x120116, 0x1200A0)  # FILE_GENERIC_READ, WRITE, EXECUTE
DANGEROUS = (0x1, 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000)


class _Luid(ct.Structure):
    _fields_ = [("low", DWORD), ("high", ct.c_int32)]


class _SidAttributes(ct.Structure):
    _fields_ = [("sid", PTR), ("attributes", DWORD)]


class _Groups(ct.Structure):
    _fields_ = [("count", DWORD), ("groups", _SidAttributes * 1)]


class _Request(ct.Structure):
    _fields_ = [("access", DWORD), ("self_sid", PTR), ("objects", PTR),
                ("object_count", DWORD), ("arguments", PTR)]


class _Reply(ct.Structure):
    _fields_ = [("count", DWORD), ("granted", PDWORD), ("sacl", PDWORD), ("error", PDWORD)]


class _Ace(ct.Structure):
    _fields_ = [("kind", ct.c_ubyte), ("flags", ct.c_ubyte), ("size", ct.c_uint16), ("mask", DWORD)]


@dataclass(frozen=True)
class Sid:
    context: object
    administrator: bool = False


@dataclass(frozen=True)
class Rights:
    mask: int
    unmapped_generic: bool = False


def _ok(result):
    if not result:
        raise ct.WinError(ct.get_last_error())


class WindowsBackend(ExitStack):
    """Own all native allocations; expose only SID lookup and rights queries."""
    def __init__(self):
        super().__init__()
        adv, auth, kernel = (ct.WinDLL(name, use_last_error=True) for name in ("advapi32", "authz", "kernel32"))
        string, boolean = ct.c_wchar_p, ct.c_int
        def bind(dll, name, args, result=boolean):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result
            return function
        self._lookup = bind(adv, "LookupAccountNameW", [string, string, PTR, PDWORD, PTR, PDWORD, PDWORD])
        self._username = bind(adv, "GetUserNameW", [PTR, PDWORD])
        self._convert = bind(adv, "ConvertStringSidToSidW", [string, PPTR])
        self._equal = bind(adv, "EqualSid", [PTR, PTR])
        self._security = bind(adv, "GetNamedSecurityInfoW", [string, DWORD, DWORD, PPTR, PPTR, PPTR, PPTR, PPTR], DWORD)
        self._acl_info = bind(adv, "GetAclInformation", [PTR, PTR, DWORD, DWORD])
        self._ace = bind(adv, "GetAce", [PTR, DWORD, PPTR])
        self._free = bind(kernel, "LocalFree", [PTR], PTR)
        self._init_rm = bind(auth, "AuthzInitializeResourceManager", [DWORD, PTR, PTR, PTR, string, PPTR])
        self._free_rm = bind(auth, "AuthzFreeResourceManager", [PTR])
        self._init = bind(auth, "AuthzInitializeContextFromSid", [DWORD, PTR, PTR, PTR, _Luid, PTR, PPTR])
        self._free_context = bind(auth, "AuthzFreeContext", [PTR])
        self._info = bind(auth, "AuthzGetInformationFromContext", [PTR, DWORD, DWORD, PDWORD, PTR])
        self._access = bind(auth, "AuthzAccessCheck", [DWORD, PTR, PTR, PTR, PTR, PTR, DWORD, PTR, PTR])
        self._manager, self._admin = PTR(), PTR()
        _ok(self._init_rm(1, None, None, None, "m2k isolation check", ct.byref(self._manager)))  # NO_AUDIT
        self.callback(self._free_rm, self._manager)
        try:
            _ok(self._convert("S-1-5-32-544", ct.byref(self._admin)))
            self.callback(self._free, self._admin)
        except OSError:
            self.close()
            raise
        self._storage = []

    def sid(self, name=None):
        """Own-SID probes only: the interactive user and the fixed built-in groups."""
        if name is None:
            size = DWORD(32768)
            username = ct.create_unicode_buffer(size.value)
            _ok(self._username(username, ct.byref(size)))
            return self._account_sid(username.value, True)
        if name not in {sid for _, sid in GROUPS}:
            raise ValueError("内部 SID 探测仅接受固定内置组")
        raw = PTR()
        _ok(self._convert(name, ct.byref(raw)))
        self.callback(self._free, raw)
        return self._context(raw, True)

    def account_sid(self, name):
        """Configured accounts always require lookup, group expansion and admin checks."""
        return self._account_sid(name, False)

    def _account_sid(self, name, direct):
        size, domain_size, kind = DWORD(), DWORD(), DWORD()
        self._lookup(None, name, None, ct.byref(size), None, ct.byref(domain_size), ct.byref(kind))
        error = ct.get_last_error()
        if error == 1332:  # ERROR_NONE_MAPPED
            return None
        if error != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise ct.WinError(error)
        raw, domain = ct.create_string_buffer(size.value), ct.create_unicode_buffer(domain_size.value)
        _ok(self._lookup(None, name, raw, ct.byref(size), domain, ct.byref(domain_size), ct.byref(kind)))
        self._storage.append(raw)
        return self._context(raw, direct)

    def _context(self, raw, direct):
        context = PTR()
        # SKIP_TOKEN_GROUPS is reserved for internal probes; configured accounts expand groups.
        _ok(self._init(2 if direct else 0, raw, self._manager, None, _Luid(), None, ct.byref(context)))
        self.callback(self._free_context, context)
        administrator = False
        if not direct:
            size = DWORD()
            self._info(context, 2, 0, ct.byref(size), None)  # AuthzContextInfoGroupsSids
            if ct.get_last_error() != 122:
                raise ct.WinError(ct.get_last_error())
            buffer = ct.create_string_buffer(size.value)
            _ok(self._info(context, 2, size.value, ct.byref(size), buffer))
            count = DWORD.from_buffer(buffer).value
            entries = (_SidAttributes * count).from_buffer(buffer, _Groups.groups.offset)
            administrator = any(self._equal(entry.sid, self._admin) for entry in entries)
        return Sid(context, administrator)

    def rights(self, sid, path):
        descriptor, dacl = PTR(), PTR()
        # OWNER | GROUP | DACL require only READ_CONTROL, never SACL privileges.
        error = self._security(str(path), 1, 7, None, None, ct.byref(dacl), None, ct.byref(descriptor))
        if error:
            raise ct.WinError(error)
        try:
            generic = False
            if dacl:
                info, ace = (DWORD * 3)(), PTR()
                _ok(self._acl_info(dacl, info, ct.sizeof(info), 2))  # AclSizeInformation
                for index in range(info[0]):
                    _ok(self._ace(dacl, index, ct.byref(ace)))
                    entry = ct.cast(ace, ct.POINTER(_Ace)).contents
                    generic |= not entry.flags & 0x8 and bool(entry.mask & 0xF0000000)  # not INHERIT_ONLY
            request, granted, error = _Request(0x02000000), DWORD(), DWORD()  # MAXIMUM_ALLOWED
            reply = _Reply(1, ct.pointer(granted), None, ct.pointer(error))
            _ok(self._access(0, sid.context, ct.byref(request), None, descriptor, None, 0, ct.byref(reply), None))
            if error.value not in (0, 5):
                raise ct.WinError(error.value)
            # Windows 实测：文件落盘先映射通用位；原始 GW/GA 描述符被 Authz 忽略（掩码 0、错误 5），故保守兜底。
            return Rights(granted.value if error.value == 0 else 0, generic)
        finally:
            self._free(descriptor)


def _configuration(config):
    section = tomllib.loads(config.read_text(encoding="utf-8")).get("isolation")
    if not isinstance(section, dict):
        raise ValueError("isolation: 缺少 [isolation] 表或类型错误")
    result, base = {}, config.resolve().parent
    def path(key, value):
        try:
            if "\x00" in value:
                raise ValueError("路径不得包含空字符")
            candidate = base / value
            # Keep backup_root links visible to the install/uninstall preflight.
            return candidate.absolute() if key == "backup_root" else candidate.resolve()
        except (OSError, ValueError, RuntimeError) as error:
            raise ValueError(f"isolation.{key}: {error}") from error
    defaults = {"owner_user": getpass.getuser(), "code_dir": str(Path(__file__).resolve().parent.parent),
                "backup_root": "C:\\m2k-acl-backup"}
    for key in ("svc_user", "agent_user", "data_dir", "tools_dir", *defaults):
        value = section.get(key, defaults.get(key))
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"isolation.{key}: 必须是非空字符串")
        if key.endswith("_user") and value.startswith("S-"):
            raise ValueError(f"isolation.{key}: 必须使用账号名，不接受裸 SID")
        result[key] = value if key.endswith("_user") else path(key, value)
    for key in ("protected", "agent_read", "agent_write", "code_readers"):
        values = section.get(key, [] if key != "protected" else None)
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() or "\x00" in v for v in values):
            raise ValueError(f"isolation.{key}: 必须是非空字符串数组")
        if key == "code_readers" and any(v.startswith("S-") or v.casefold() in
                                        {result["svc_user"].casefold(), result["agent_user"].casefold()} for v in values):
            raise ValueError("isolation.code_readers: 必须使用账号名，且不得包含 svc_user / agent_user")
        result[key] = values if key == "code_readers" else [path(key, value) for value in values]
    for key in ("agent_read", "agent_write"):
        if any(p.is_relative_to(root) for p in result[key] for root in [result["data_dir"], *result["protected"]]):
            raise ValueError(f"isolation.{key}: 不得等于或位于 data_dir / protected 内")
    return result


def check(config, backend=None):
    """Return (exit code, Chinese report); injected backends make checks portable."""
    if sys.platform != "win32":
        return 2, "m2k isolation check 仅支持 Windows"
    config = Path(config)
    try:
        cfg = _configuration(config)
    except (OSError, ValueError) as error:
        detail = str(error)
        return 2, f"配置错误：{'' if detail.startswith('isolation') else 'isolation: '}{detail}"
    rows, identities, errors = [], {}, {}
    def row(label, passed, path="-", note="", information=False):
        result = "信息" if information else "通过" if passed else "不通过"
        rows.append((label + (f"（{note}）" if note else ""), result, str(path)))
    def access(name, path, bit, expected, label, information=False):
        verb = ("读", "写", "执行")[bit]
        if identities[name] is None:
            row(label, False, path, errors.get(name, "账号不存在"), information)
            return
        try:
            rights = backend.rights(identities[name], path)
            if rights.unmapped_generic and (not expected or information):
                row(label, False, path, "ACE 含未映射的通用权限位，无法可靠判定", information)
                return
            actual = (bool(rights.mask & DANGEROUS[bit]) if not expected or information
                      else rights.mask & REQUIRED[bit] == REQUIRED[bit])
            note = ("可" if actual else "不可") + verb
            if bit == 1 and actual:
                note += f"，授予掩码 0x{rights.mask:08X}"
            row(label, actual == expected, path, note, information)
        except FileNotFoundError:
            row(label, not expected, path, "路径不存在", information)
        except OSError as error:
            row(label, False, path, f"查询失败：{error}", information)
    try:
        with ExitStack() as cleanup:
            if backend is None:
                backend = cleanup.enter_context(WindowsBackend())
            svc, agent = cfg["svc_user"], cfg["agent_user"]
            for name in (svc, agent, None):
                try:
                    identities[name] = backend.sid() if name is None else backend.account_sid(name)
                except OSError as error:
                    identities[name], errors[name] = None, f"账号查询失败：{error}"
            for name in (svc, agent):
                principal = identities[name]
                row(f"{name} 账号存在", principal is not None, note=errors.get(name, "账号不存在") if principal is None else "")
                row(f"{name} 非管理员", principal is not None and not principal.administrator,
                    note="账号不存在或查询失败" if principal is None else "")
            code = cfg["code_dir"]
            private = [cfg["data_dir"], *cfg["protected"]]
            for path in private:
                access(agent, path, 0, False, f"{agent} 不可读框架私有目录")
            for path in (code, config.resolve(), cfg["tools_dir"]):
                access(agent, path, 1, False, f"{agent} 不可写代码/配置/工具")
            for bit in (0, 2):
                access(agent, cfg["tools_dir"], bit, True, f"{agent} 可{('读', '写', '执行')[bit]}工具")
            for path, bit, expected in ((code, 0, True), (cfg["tools_dir"], 0, True), (code, 1, False),
                                        (cfg["data_dir"], 0, True), (cfg["data_dir"], 1, True)):
                access(svc, path, bit, expected, f"{svc} {'可' if expected else '不可'}{('读', '写')[bit]}")
            for bit in (0, 1):
                access(None, code, bit, True, f"当前用户（自身 SID）可{('读', '写')[bit]}代码")
            for key, bit in (("agent_read", 0), ("agent_write", 1)):
                for path in cfg[key]:
                    access(agent, path, bit, True, f"{key} 主动授权")
            if any(identities[name] is None for name in (svc, agent)):
                for label, name in GROUPS:
                    try:
                        identities[name] = backend.sid(name)
                    except OSError as error:
                        identities[name], errors[name] = None, str(error)
                    for path in private:
                        for bit in (0, 1):
                            access(name, path, bit, True, f"{label}（{name}）{'读' if bit == 0 else '写'}", True)
    except OSError as error:
        row("Windows 权限查询", False, note=str(error))
    failures = sum(result == "不通过" for _, result, _ in rows)
    return int(bool(failures)), "\n".join(["检查项\t结果\t路径", *("\t".join(r) for r in rows),
                                        f"汇总：通过 {sum(r[1] == '通过' for r in rows)}，不通过 {failures}，信息 {sum(r[1] == '信息' for r in rows)}"])
