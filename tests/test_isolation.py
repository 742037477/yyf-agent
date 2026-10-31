"""T75: portable policy checks and read-only real Windows Authz queries."""
import ctypes as ct
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from m2k import isolation
from m2k.cli import main

READ, WRITE, EXECUTE = isolation.REQUIRED


class FakeBackend(ExitStack):
    def __init__(self):
        super().__init__()
        self.accounts = {"m2k-svc": "svc", "m2k-agent": "agent", None: "current",
                         **{sid: sid for _, sid in isolation.GROUPS}}
        self.administrators, self.missing, self.denied = set(), set(), set()
        self.generic = set()
        self.permissions, self.calls, self.lookup_errors = {}, [], {}

    def sid(self, name=None):
        assert name is None or name in {sid for _, sid in isolation.GROUPS}
        self.calls.append(("sid", name))
        return self._principal(name)

    def account_sid(self, name):
        self.calls.append(("account_sid", name))
        return self._principal(name)

    def _principal(self, name):
        if name in self.lookup_errors:
            raise self.lookup_errors[name]
        value = self.accounts.get(name)
        return None if value is None else isolation.Sid(value, value in self.administrators)

    def rights(self, sid, path):
        self.calls.append(("rights", sid.context, path))
        if path in self.missing:
            raise FileNotFoundError(str(path))
        if path in self.denied:
            raise PermissionError("security descriptor unavailable")
        return isolation.Rights(self.permissions.get((sid.context, path), 0), path in self.generic)


class IsolationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "m2k.toml"
        self.code = Path(isolation.__file__).resolve().parent.parent
        self.paths = {name: self.root / name for name in ("data", "private1", "private2", "tools", "read", "write")}
        for path in self.paths.values():
            path.mkdir()
        self.section = {"svc_user": "m2k-svc", "agent_user": "m2k-agent", "data_dir": "data", "tools_dir": "tools",
                        "protected": ["private1", "private2"], "agent_read": ["read"], "agent_write": ["write"]}
        self.write_config()
        self.backend = FakeBackend()
        self.backend.permissions.update({
            ("agent", self.paths["tools"]): READ | EXECUTE,
            ("agent", self.paths["read"]): READ,
            ("agent", self.paths["write"]): WRITE,
            ("svc", self.paths["tools"]): READ | EXECUTE,
            ("svc", self.code): READ | EXECUTE,
            ("svc", self.paths["data"]): READ | WRITE,
            ("current", self.code): READ | WRITE | EXECUTE,
        })

    def write_config(self, section=None):
        section = self.section if section is None else section
        self.config.write_text('ledger = "never-open.sqlite"\n[isolation]\n' +
                               "".join(f"{key} = {json.dumps(value)}\n" for key, value in section.items()),
                               encoding="utf-8")

    def invoke(self):
        with patch("m2k.isolation.sys.platform", "win32"):
            return isolation.check(self.config, self.backend)

    def rows(self, report):
        return [line.split("\t") for line in report.splitlines()[1:-1]]

    def test_all_pass_and_relative_paths(self):
        code, report = self.invoke()
        self.assertEqual(code, 0, report)
        self.assertEqual(report.splitlines()[0], "检查项\t结果\t路径")
        self.assertTrue(all(row[1] == "通过" for row in self.rows(report)))
        self.assertTrue(report.splitlines()[-1].startswith("汇总："))
        self.assertEqual(len(self.rows(report)), 21)
        queried = {call[2] for call in self.backend.calls if call[0] == "rights"}
        self.assertEqual(queried, {self.code, self.config, *self.paths.values()})
        self.assertIn(("sid", None), self.backend.calls)
        self.assertIn(("account_sid", "m2k-svc"), self.backend.calls)
        self.assertIn(("account_sid", "m2k-agent"), self.backend.calls)
        self.assertNotIn(("sid", "S-1-5-32-544"), self.backend.calls)

    def test_t76_defaults_and_configured_code_folder(self):
        with patch("m2k.isolation.getpass.getuser", return_value="owner"):
            cfg = isolation._configuration(self.config)
        self.assertEqual(cfg["owner_user"], "owner")
        self.assertEqual(cfg["code_dir"], self.code)
        self.assertEqual(cfg["code_readers"], [])
        self.assertEqual(cfg["backup_root"], (self.root / "C:\\m2k-acl-backup").resolve())
        code_dir = self.root / "configured code"
        self.write_config(self.section | {"code_dir": str(code_dir), "owner_user": "another-owner",
                                          "code_readers": ["reader"], "backup_root": "backup"})
        for identity in ("svc", "current"):
            self.backend.permissions[(identity, code_dir)] = self.backend.permissions[(identity, self.code)]
        status, report = self.invoke()
        self.assertEqual(status, 0, report)
        queried = {call[2] for call in self.backend.calls if call[0] == "rights"}
        self.assertIn(code_dir, queried)
        self.assertNotIn(self.code, queried)
        cfg = isolation._configuration(self.config)
        self.assertEqual(cfg["code_readers"], ["reader"])
        self.assertEqual(cfg["backup_root"], self.root / "backup")

    def test_t76_configuration_errors_name_the_key(self):
        cases = [(key, value) for key in ("owner_user", "code_dir", "backup_root")
                 for value in (False, [], "", "   ", "\x00")]
        cases += [("owner_user", "S-1-1-0")]
        cases += [("code_readers", value) for value in (False, "reader", [1], [""], ["S-1-1-0"],
                                                       ["M2K-SVC"], ["m2k-agent"])]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.write_config(self.section | {key: value})
                status, report = self.invoke()
                self.assertEqual(status, 2, report)
                self.assertIn(key, report)
                self.assertEqual(self.backend.calls, [])

    def test_each_single_wrong_permission_fails_its_row(self):
        cases = [
            ("agent", self.paths["data"], 0, True, "不可读框架私有目录"),
            ("agent", self.paths["private1"], 0, True, "不可读框架私有目录"),
            ("agent", self.paths["private2"], 0, True, "不可读框架私有目录"),
            ("agent", self.code, 1, True, "不可写代码/配置/工具"),
            ("agent", self.config, 1, True, "不可写代码/配置/工具"),
            ("agent", self.paths["tools"], 1, True, "不可写代码/配置/工具"),
            ("agent", self.paths["tools"], 0, False, "可读工具"),
            ("agent", self.paths["tools"], 2, False, "可执行工具"),
            ("svc", self.code, 0, False, "m2k-svc 可读"),
            ("svc", self.paths["tools"], 0, False, "m2k-svc 可读"),
            ("svc", self.code, 1, True, "m2k-svc 不可写"),
            ("svc", self.paths["data"], 0, False, "m2k-svc 可读"),
            ("svc", self.paths["data"], 1, False, "m2k-svc 可写"),
            ("current", self.code, 0, False, "自身 SID）可读代码"),
            ("current", self.code, 1, False, "自身 SID）可写代码"),
            ("agent", self.paths["read"], 0, False, "agent_read 主动授权"),
            ("agent", self.paths["write"], 1, False, "agent_write 主动授权"),
        ]
        original = self.backend.permissions.copy()
        for sid, path, bit, value, label in cases:
            with self.subTest(sid=sid, path=path, bit=bit):
                self.backend.permissions = original.copy()
                rights = self.backend.permissions.get((sid, path), 0)
                self.backend.permissions[(sid, path)] = (rights | isolation.REQUIRED[bit] if value
                                                       else rights & ~(0x1, 0x2, 0x20)[bit])
                code, report = self.invoke()
                self.assertEqual(code, 1, report)
                failed = [row for row in self.rows(report) if row[1] == "不通过"]
                self.assertEqual(len(failed), 1, report)
                self.assertIn(label, failed[0][0])
                self.assertEqual(failed[0][2], str(path))

    def test_administrator_membership_fails_even_with_correct_permissions(self):
        for sid, label in (("svc", "m2k-svc"), ("agent", "m2k-agent")):
            with self.subTest(sid=sid):
                self.backend.administrators = {sid}
                code, report = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual([row[0] for row in self.rows(report) if row[1] == "不通过"],
                                 [f"{label} 非管理员"])
        self.backend.administrators = {"current"}
        self.assertEqual(self.invoke()[0], 0)

    def test_missing_accounts_fail_and_group_rows_are_only_information(self):
        original = self.backend.accounts.copy()
        for missing in (("m2k-svc",), ("m2k-agent",), ("m2k-svc", "m2k-agent")):
            with self.subTest(missing=missing):
                self.backend.accounts = {k: v for k, v in original.items() if k not in missing}
                summaries = []
                for granted in (False, True):
                    for _, sid in isolation.GROUPS:
                        for name in ("data", "private1", "private2"):
                            self.backend.permissions[(sid, self.paths[name])] = READ | WRITE | EXECUTE if granted else 0
                    code, report = self.invoke()
                    self.assertEqual(code, 1)
                    rows = self.rows(report)
                    information = [row for row in rows if row[1] == "信息"]
                    self.assertEqual(len(information), 18, report)
                    self.assertTrue(all(any(sid in row[0] for _, sid in isolation.GROUPS) for row in information))
                    self.assertTrue(all(("可读" if granted else "不可读") in row[0]
                                        for row in information if "）读" in row[0]))
                    for name in missing:
                        account = next(row for row in rows if row[0].startswith(f"{name} 账号存在"))
                        self.assertEqual(account[1], "不通过")
                        self.assertIn("账号不存在", account[0])
                        permission_rows = [row for row in rows if row[0].startswith(name) and row[2] != "-"]
                        self.assertTrue(permission_rows)
                        self.assertTrue(all(row[1] == "不通过" and "账号不存在" in row[0] for row in permission_rows))
                    summaries.append(report.splitlines()[-1])
                self.assertEqual(summaries[0], summaries[1])

    def test_missing_current_user_and_lookup_errors_fail_closed(self):
        self.backend.accounts.pop(None)
        code, report = self.invoke()
        self.assertEqual(code, 1)
        current = [row for row in self.rows(report) if row[0].startswith("当前用户")]
        self.assertEqual(len(current), 2)
        self.assertTrue(all(row[1] == "不通过" for row in current))
        self.backend.accounts[None] = "current"
        self.backend.lookup_errors["m2k-agent"] = OSError("lookup unavailable")
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("账号查询失败：lookup unavailable", report)
        self.assertEqual(len([r for r in self.rows(report) if r[1] == "信息"]), 18)

    def test_missing_paths_pass_denials_and_fail_required_access(self):
        for path in (self.code, self.config, *self.paths.values()):
            with self.subTest(path=path):
                self.backend.missing = {path}
                code, report = self.invoke()
                rows = [row for row in self.rows(report) if row[2] == str(path)]
                self.assertTrue(rows)
                self.assertTrue(all("路径不存在" in row[0] for row in rows))
                for label, result, _ in rows:
                    self.assertEqual(result, "通过" if "不可" in label else "不通过", report)
                expected = 0 if path in (self.config, self.paths["private1"], self.paths["private2"]) else 1
                self.assertEqual(code, expected, report)

    def test_missing_path_group_rows_remain_informational(self):
        self.backend.accounts.pop("m2k-agent")
        self.backend.missing.add(self.paths["private1"])
        code, report = self.invoke()
        self.assertEqual(code, 1)
        rows = [row for row in self.rows(report) if row[1] == "信息" and row[2] == str(self.paths["private1"])]
        self.assertEqual(len(rows), 6)
        self.assertTrue(all("路径不存在" in row[0] for row in rows))

    def test_descriptor_query_error_is_not_a_successful_denial(self):
        self.backend.denied.add(self.paths["private1"])
        code, report = self.invoke()
        self.assertEqual(code, 1)
        failed = [row for row in self.rows(report) if row[1] == "不通过"]
        self.assertEqual(len(failed), 1)
        self.assertIn("查询失败", failed[0][0])

    def test_required_keys_missing_and_scalar_wrong_types(self):
        for key in ("svc_user", "agent_user", "data_dir", "tools_dir", "protected"):
            for value in ("missing", 123, True, [], ""):
                with self.subTest(key=key, value=value):
                    section = self.section.copy()
                    if value == "missing":
                        section.pop(key)
                    elif key == "protected" and value == []:
                        continue  # An empty protected list is valid.
                    else:
                        section[key] = value
                    self.write_config(section)
                    code, report = self.invoke()
                    self.assertEqual(code, 2, report)
                    self.assertIn(key, report)
                    self.assertEqual(self.backend.calls, [])

    def test_array_types_and_members_are_validated(self):
        for key in ("protected", "agent_read", "agent_write"):
            for value in (False, "directory", 1, [1], [False], [""], ["   "], [[]], ["\x00"]):
                with self.subTest(key=key, value=value):
                    self.write_config(self.section | {key: value})
                    code, report = self.invoke()
                    self.assertEqual(code, 2, report)
                    self.assertIn(key, report)
                    self.assertEqual(self.backend.calls, [])

    def test_missing_section_wrong_section_and_invalid_toml(self):
        for text in ("", "isolation = false", 'isolation = "wrong"', "isolation = []", "[isolation"):
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                code, report = self.invoke()
                self.assertEqual(code, 2)
                self.assertIn("isolation", report)
                self.assertEqual(self.backend.calls, [])

    def test_missing_config_and_null_scalar_path_are_configuration_errors(self):
        self.config.unlink()
        code, report = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("isolation", report)
        for key in ("data_dir", "tools_dir"):
            self.write_config(self.section | {key: "\x00"})
            code, report = self.invoke()
            self.assertEqual(code, 2)
            self.assertIn(key, report)

    def test_grants_cannot_equal_or_enter_any_private_path(self):
        for key in ("agent_read", "agent_write"):
            for root in ("data", "private1", "private2"):
                for path in (root, f"{root}/child", f"{root}/child/../grandchild"):
                    with self.subTest(key=key, path=path):
                        self.write_config(self.section | {key: [path]})
                        code, report = self.invoke()
                        self.assertEqual(code, 2, report)
                        self.assertIn(key, report)
                        self.assertEqual(self.backend.calls, [])

    def test_optional_grants_can_be_omitted_and_sibling_is_not_inside_private(self):
        section = {k: v for k, v in self.section.items() if k not in ("agent_read", "agent_write")}
        self.write_config(section)
        self.assertEqual(self.invoke()[0], 0)
        sibling = self.root / "private1-sibling"
        self.backend.permissions[("agent", sibling)] = READ
        self.write_config(section | {"agent_read": ["private1-sibling"]})
        self.assertEqual(self.invoke()[0], 0)

    def test_non_windows_does_not_read_config_or_query_backend(self):
        with patch("m2k.isolation.sys.platform", "linux"), patch.object(Path, "read_text") as read:
            code, report = isolation.check(self.config, self.backend)
        self.assertEqual((code, report), (2, "m2k isolation check 仅支持 Windows"))
        read.assert_not_called()
        self.assertEqual(self.backend.calls, [])

    def test_cli_is_read_only_and_never_opens_a_ledger(self):
        def snapshot():
            return {p.relative_to(self.root): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
                    for p in [self.root, *self.root.rglob("*")]}
        before, output = snapshot(), io.StringIO()
        with patch("m2k.isolation.sys.platform", "win32"), \
                patch("m2k.isolation.WindowsBackend", return_value=self.backend), \
                patch("m2k.cli.Ledger", side_effect=AssertionError("opened ledger")), \
                patch("m2k.cli._ledger", side_effect=AssertionError("opened ledger")), redirect_stdout(output):
            code = main(["--config", str(self.config), "--ledger", str(self.root / "ignored.sqlite"),
                         "isolation", "check"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("检查项\t结果\t路径", output.getvalue())
        self.assertEqual(snapshot(), before)
        self.assertTrue(self.backend.calls)
        self.assertTrue(all(call[0] in ("account_sid", "sid", "rights") for call in self.backend.calls))

    def test_cli_returns_configuration_and_platform_codes(self):
        output = io.StringIO()
        self.write_config(self.section | {"svc_user": False})
        with patch("m2k.isolation.sys.platform", "win32"), redirect_stdout(output):
            self.assertEqual(main(["--config", str(self.config), "isolation", "check"]), 2)
        self.assertIn("svc_user", output.getvalue())
        output = io.StringIO()
        with patch("m2k.isolation.sys.platform", "linux"), redirect_stdout(output):
            self.assertEqual(main(["isolation", "check"]), 2)
        self.assertEqual(output.getvalue(), "m2k isolation check 仅支持 Windows\n")

    def test_backend_initialization_error_is_a_failed_check(self):
        with patch("m2k.isolation.sys.platform", "win32"), \
                patch("m2k.isolation.WindowsBackend", side_effect=OSError("Authz unavailable")):
            code, report = isolation.check(self.config)
        self.assertEqual(code, 1)
        self.assertIn("Windows 权限查询（Authz unavailable）\t不通过", report)

    def test_bare_sid_configuration_is_rejected_before_any_backend_call(self):
        for key in ("svc_user", "agent_user"):
            for value in ("S-1-1-0", "S-1-5-32-544", "S-not-an-account"):
                with self.subTest(key=key, value=value):
                    self.write_config(self.section | {key: value})
                    code, report = self.invoke()
                    self.assertEqual(code, 2, report)
                    self.assertIn(f"配置错误：isolation.{key}:", report)
                    self.assertNotIn("isolation: isolation", report)
                    self.assertEqual(self.backend.calls, [])

    def test_each_partial_dangerous_write_and_read_fails_negative_checks(self):
        original = self.backend.permissions.copy()
        for mask in (0x2, 0x4, 0x10, 0x40, 0x100, 0x10000, 0x40000, 0x80000):
            for sid, path in (("agent", self.code), ("agent", self.config),
                              ("agent", self.paths["tools"]), ("svc", self.code)):
                with self.subTest(mask=hex(mask), sid=sid, path=path):
                    self.backend.permissions = original.copy()
                    self.backend.permissions[(sid, path)] = original.get((sid, path), 0) | mask
                    code, report = self.invoke()
                    failed = [r for r in self.rows(report) if r[1] == "不通过"]
                    self.assertEqual(code, 1, report)
                    self.assertEqual(len(failed), 1, report)
                    self.assertIn("可写，授予掩码 0x", failed[0][0])
        self.backend.permissions = original.copy()
        self.backend.permissions[("agent", self.paths["private1"])] = 0x1
        code, report = self.invoke()
        self.assertEqual(code, 1, report)
        self.assertIn("不可读框架私有目录（可读）\t不通过", report)

    def test_positive_checks_require_every_bit_of_each_generic_file_mask(self):
        original = self.backend.permissions.copy()
        for required, path, label in ((READ, self.paths["read"], "agent_read 主动授权"),
                                      (WRITE, self.paths["write"], "agent_write 主动授权"),
                                      (EXECUTE, self.paths["tools"], "可执行工具")):
            for bit in range(32):
                if not required & (1 << bit):
                    continue
                with self.subTest(required=hex(required), missing=hex(1 << bit)):
                    self.backend.permissions = original.copy()
                    self.backend.permissions[("agent", path)] &= ~(1 << bit)
                    code, report = self.invoke()
                    self.assertEqual(code, 1, report)
                    self.assertTrue(any(label in r[0] and r[1] == "不通过" for r in self.rows(report)), report)

    def test_information_uses_any_dangerous_bit_and_prints_write_mask(self):
        self.backend.accounts.pop("m2k-agent")
        for _, sid in isolation.GROUPS:
            self.backend.permissions[(sid, self.paths["private1"])] = 0x40001
        code, report = self.invoke()
        self.assertEqual(code, 1)
        rows = [r for r in self.rows(report) if r[1] == "信息" and r[2] == str(self.paths["private1"])]
        self.assertEqual(len(rows), 6)
        self.assertTrue(all("可读）" in r[0] or "可写，授予掩码 0x00040001" in r[0] for r in rows))

    def test_unmapped_generic_ace_fails_all_negative_checks_on_that_path(self):
        self.backend.generic.add(self.code)
        code, report = self.invoke()
        self.assertEqual(code, 1, report)
        failed = [r for r in self.rows(report) if r[1] == "不通过"]
        self.assertEqual(len(failed), 2, report)
        self.assertTrue(all(r[2] == str(self.code) and "ACE 含未映射的通用权限位，无法可靠判定" in r[0]
                            for r in failed))
        self.backend.accounts.pop("m2k-agent")
        self.backend.generic.add(self.paths["private1"])
        _, report = self.invoke()
        rows = [r for r in self.rows(report) if r[1] == "信息" and r[2] == str(self.paths["private1"])]
        self.assertTrue(all("无法可靠判定" in r[0] and "不可" not in r[0] for r in rows), report)


class NativeContractTest(unittest.TestCase):
    def backend(self, mask, error, ace=None):
        backend = isolation.WindowsBackend.__new__(isolation.WindowsBackend)
        def security(path, kind, flags, owner, group, dacl, sacl, descriptor):
            ct.cast(descriptor, isolation.PPTR)[0] = isolation.PTR(1234)
            ct.cast(dacl, isolation.PPTR)[0] = isolation.PTR(5678 if ace is not None else 0)
            return 0
        def access(flags, context, request, audit, descriptor, extra, count, reply, results):
            self.assertEqual(ct.cast(request, ct.POINTER(isolation._Request)).contents.access, 0x02000000)
            value = ct.cast(reply, ct.POINTER(isolation._Reply)).contents
            value.granted[0], value.error[0] = mask, error
            return 1
        backend._security, backend._access, backend._free = security, Mock(side_effect=access), Mock()
        if ace is not None:
            def info(dacl, buffer, size, kind):
                buffer[0] = 1
                return 1
            def get_ace(dacl, index, output):
                ct.cast(output, isolation.PPTR)[0] = ct.cast(ct.pointer(ace), isolation.PTR)
                return 1
            backend._acl_info, backend._ace = info, get_ace
        return backend

    def test_single_maximum_allowed_query_preserves_partial_grants_and_denial_is_zero(self):
        for mask, error in ((0x2, 0), (0x40000, 0), (0x10000, 0), (0xFFFFFFFF, 5)):
            with self.subTest(mask=mask, error=error):
                backend = self.backend(mask, error)
                rights = backend.rights(isolation.Sid(None), Path("test"))
                self.assertEqual(rights.mask, 0 if error == 5 else mask)
                backend._access.assert_called_once()
                backend._free.assert_called_once()

    def test_effective_generic_ace_is_detected_but_inherit_only_is_ignored(self):
        for flags in (0, 0x10, 0x8, 0x18):
            with self.subTest(flags=flags):
                ace = isolation._Ace(0, flags, ct.sizeof(isolation._Ace), 0x40000000)
                backend = self.backend(0, 5, ace)
                rights = backend.rights(isolation.Sid(None), Path("test"))
                self.assertEqual(rights.unmapped_generic, not flags & 0x8)
                self.assertEqual(rights.mask, 0)


@unittest.skipUnless(os.name == "nt", "Windows Authz only")
class RealWindowsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "rights.txt"
        self.path.write_text("read-only permission query", encoding="utf-8")
        self.backend = isolation.WindowsBackend()
        self.addCleanup(self.backend.close)

    def test_current_user_has_read_and_write_using_own_sid(self):
        with patch.object(self.backend, "_init", wraps=self.backend._init) as initialize:
            principal = self.backend.sid(None)
        self.assertEqual(initialize.call_args.args[0], 2)  # AUTHZ_SKIP_TOKEN_GROUPS
        self.assertIsNotNone(principal)
        self.assertEqual(self.backend.rights(principal, self.path).mask & (READ | WRITE), READ | WRITE)

    def test_named_current_user_expands_groups_and_reports_administrator_membership(self):
        size = isolation.DWORD(32768)
        name = ct.create_unicode_buffer(size.value)
        self.assertTrue(self.backend._username(name, ct.byref(size)))
        with patch.object(self.backend, "_init", wraps=self.backend._init) as initialize:
            principal = self.backend.account_sid(name.value)
        self.assertEqual(initialize.call_args.args[0], 0)  # Normal account group expansion.
        self.assertIsNotNone(principal)
        groups = subprocess.run(["whoami", "/groups", "/fo", "csv", "/nh"],
                                capture_output=True, check=True).stdout
        self.assertEqual(principal.administrator, b"S-1-5-32-544" in groups)
        self.assertEqual(self.backend.rights(principal, self.path).mask & (READ | WRITE), READ | WRITE)

    def test_everyone_without_an_everyone_ace_has_no_access(self):
        descriptor, text = isolation.PTR(), isolation.PTR()
        error = self.backend._security(str(self.path), 1, 7, None, None, None, None, ct.byref(descriptor))
        self.assertEqual(error, 0)
        try:
            convert = ct.WinDLL("advapi32", use_last_error=True).ConvertSecurityDescriptorToStringSecurityDescriptorW
            convert.argtypes = [isolation.PTR, isolation.DWORD, isolation.DWORD, isolation.PPTR, isolation.PDWORD]
            convert.restype = ct.c_int
            self.assertTrue(convert(descriptor, 1, 4, ct.byref(text), None))
            try:
                dacl = ct.wstring_at(text)
            finally:
                self.backend._free(text)
        finally:
            self.backend._free(descriptor)
        if ";;;WD)" in dacl or ";;;S-1-1-0)" in dacl:
            self.skipTest("Temporary file inherits Everyone ACE; keep the existing ACL unchanged")
        self.assertNotEqual(dacl, "D:NO_ACCESS_CONTROL")
        self.assertEqual(self.backend.rights(self.backend.sid("S-1-1-0"), self.path).mask &
                         (isolation.DANGEROUS[0] | isolation.DANGEROUS[1]), 0)

    def test_generic_everyone_ace_on_own_temporary_file_and_raw_descriptor(self):
        adv = ct.WinDLL("advapi32", use_last_error=True)
        convert = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [ct.c_wchar_p, isolation.DWORD, isolation.PPTR, isolation.PDWORD]
        convert.restype = ct.c_int
        get_dacl = adv.GetSecurityDescriptorDacl
        get_dacl.argtypes = [isolation.PTR, isolation.PTR, isolation.PPTR, isolation.PTR]
        get_dacl.restype = ct.c_int
        set_security = adv.SetNamedSecurityInfoW
        set_security.argtypes = [ct.c_wchar_p, isolation.DWORD, isolation.DWORD,
                                 isolation.PTR, isolation.PTR, isolation.PTR, isolation.PTR]
        set_security.restype = isolation.DWORD
        original, original_dacl = isolation.PTR(), isolation.PTR()
        security = self.backend._security
        self.assertEqual(security(str(self.path), 1, 7, None, None, ct.byref(original_dacl),
                                  None, ct.byref(original)), 0)
        everyone = self.backend.sid("S-1-1-0")
        try:
            for generic, mapped in ((0x80000000, READ), (0x40000000, WRITE),
                                    (0x20000000, EXECUTE), (0x10000000, 0x1F01FF)):
                with self.subTest(generic=hex(generic)):
                    descriptor, dacl, present, defaulted = isolation.PTR(), isolation.PTR(), ct.c_int(), ct.c_int()
                    self.assertTrue(convert(f"D:P(A;;0x{generic:08X};;;WD)", 1, ct.byref(descriptor), None))
                    try:
                        self.assertTrue(get_dacl(descriptor, ct.byref(present), ct.byref(dacl), ct.byref(defaulted)))
                        self.assertEqual(set_security(str(self.path), 1, 0x80000004, None, None, dacl, None), 0)
                    finally:
                        self.backend._free(descriptor)
                    rights = self.backend.rights(everyone, self.path)
                    self.assertEqual(rights.mask & mapped, mapped)

                    def raw_snapshot(*args):
                        # Windows maps generic bits when storing a file ACL. Restore the raw bit
                        # only in this file's returned SD buffer to exercise native Authz itself.
                        error = security(*args)
                        if error:
                            return error
                        file_dacl = ct.cast(args[5], isolation.PPTR)[0]
                        info, ace = (isolation.DWORD * 3)(), isolation.PTR()
                        self.assertTrue(self.backend._acl_info(file_dacl, info, ct.sizeof(info), 2))
                        self.assertEqual(info[0], 1)  # Exactly one Everyone allow ACE, no inheritance.
                        self.assertTrue(self.backend._ace(file_dacl, 0, ct.byref(ace)))
                        entry = ct.cast(ace, ct.POINTER(isolation._Ace)).contents
                        self.assertEqual(entry.mask, mapped)
                        entry.mask = generic
                        return 0
                    with patch.object(self.backend, "_security", side_effect=raw_snapshot):
                        raw = self.backend.rights(everyone, self.path)
                    self.assertEqual(raw.mask, 0)
                    self.assertTrue(raw.unmapped_generic)
        finally:
            try:
                self.assertEqual(set_security(str(self.path), 1, 0x20000004, None, None, original_dacl, None), 0)
            finally:
                self.backend._free(original)


if __name__ == "__main__":
    unittest.main()
