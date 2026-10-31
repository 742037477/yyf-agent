"""T76: portable administrator workflow and owner-created Windows ACL round trip."""
import ctypes as ct
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from m2k import isolation, isolation_install as install
from m2k.cli import main


def _token_elevated():
    with install.System() as system:
        return system.elevated()


class FakeSystem:
    windows, log = True, None

    def __init__(self, root):
        self.root, self.calls, self.output = root, [], []
        self.accounts = {"owner": {"sid": "S-1-5-21-10", "administrator": True},
                         "reader": {"sid": "S-1-5-21-11", "administrator": False}}
        self.is_elevated, self.filesystem, self.answer = True, "NTFS", "YES"
        self.passwords, self.buffers, self.fail = [], [], lambda call: False
        self.backup_permissions, self.backup_errors = {}, {}
        self.reparse_points = set()
        self.check_result = (0, "检查项\t结果\t路径\n汇总：通过 21，不通过 0，信息 0")

    def record(self, *call):
        self.calls.append(call)
        if self.fail(call):
            raise OSError("injected " + call[0])

    @property
    def mutations(self):
        return [c for c in self.calls if c[0] in ("mkdir", "write", "run", "acl", "create")]

    def elevated(self):
        self.record("elevated")
        return self.is_elevated

    def account(self, name):
        self.record("account", name)
        return self.accounts.get(name)

    def is_reparse(self, path):
        self.record("is_reparse", path)
        return path in self.reparse_points

    def backup_rights(self, name, path):
        self.record("backup_rights", name, path)
        if (name, path) in self.backup_errors:
            raise self.backup_errors[name, path]
        if name not in {sid for _, sid in isolation.GROUPS} and name not in self.accounts:
            return None
        return self.backup_permissions.get((name, path), isolation.Rights(0))

    def volume(self, path):
        self.record("volume", path)
        return self.filesystem

    def roots(self):
        return self.root / "Windows", [self.root / "Users", self.root / "Users/owner"]

    def emit(self, text):
        self.output.append(text)

    def readline(self, prompt):
        self.record("readline", prompt)
        return self.answer

    def password(self, prompt):
        self.record("password", prompt)
        return self.passwords.pop(0) if self.passwords else "T76-secret-123456"

    def mkdir(self, path):
        self.record("mkdir", path)
        install.System.mkdir(self, path)

    def write(self, path, text, encoding="utf-8", append=False):
        self.record("write", path, text, encoding, append)
        install.System.write(self, path, text, encoding, append)

    def run(self, arguments, cwd=None, interactive=False):
        arguments = list(map(str, arguments))
        self.record("run", arguments, cwd, interactive)
        if "/save" in arguments:
            self.write(Path(arguments[arguments.index("/save") + 1]), "fake saved ACL")
        if self.log:
            self.write(self.log, json.dumps({"command": arguments, "exit": 0, "output": "fake success"}) + "\n", append=True)
        return 0

    def acl(self, path, entries, recursive=True):
        self.record("acl", path, entries, recursive)

    def create_account(self, name, buffer, comment):
        self.buffers.append(buffer)
        self.record("create", name, comment)
        self.accounts[name] = {"sid": f"S-1-5-21-{len(self.accounts) + 100}", "administrator": False}

    def check(self, config):
        self.record("check", config)
        return self.check_result


class InstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "protected" / "config.toml"
        self.backup = self.root / "backup"
        for name in ("code space's", "protected", "read", "write"):
            (self.root / name).mkdir()
        self.section = {"svc_user": "svc", "agent_user": "agent", "owner_user": "owner", "code_readers": ["reader"],
                        "code_dir": "code space's", "data_dir": "data", "tools_dir": "tools", "protected": ["protected"],
                        "agent_read": ["read"], "agent_write": ["write"], "backup_root": "backup"}
        self.write_config()
        self.system = FakeSystem(self.root)

    def write_config(self, changes=None):
        section = self.section | (changes or {})
        def absolute(value):
            return str(self.root / value) if isinstance(value, str) and value.strip() and "\x00" not in value else value
        for key in ("code_dir", "data_dir", "tools_dir", "backup_root"):
            section[key] = absolute(section[key])
        for key in ("protected", "agent_read", "agent_write"):
            if isinstance(section[key], list):
                section[key] = [absolute(value) for value in section[key]]
        self.config.write_text("[isolation]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in
                                                        section.items()), encoding="utf-8")

    def execute(self, action="install"):
        return install.execute(self.config, action, self.system)

    def manifests(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.backup.glob("*/manifest.json"))]

    def install_ok(self):
        status, report = self.execute()
        self.assertEqual(status, 0, report)
        return self.manifests()[-1]

    def test_config_in_code_or_protected_passes_preflight(self):
        self.system.accounts["agent"] = {"sid": "S-1-5-21-100", "administrator": False}
        self.system.answer = "NO"
        for folder in ("code space's", "protected"):
            self.config = self.root / folder / "config.toml"
            self.write_config({"protected": []} if folder == "code space's" else {})
            for action in ("install", "grant", "revoke"):
                with self.subTest(folder=folder, action=action):
                    self.assertEqual(self.execute(action), (1, "已取消，未做任何更改"))
                    self.assertEqual(self.system.mutations, [])

    def test_config_outside_code_and_protected_is_rejected_without_mutation(self):
        self.system.accounts["agent"] = {"sid": "S-1-5-21-100", "administrator": False}
        self.system.answer = "NO"
        for path in (self.root / "outside.toml", self.root / "protected/../escaped.toml",
                     self.root / "code space's-sibling/outside.toml"):
            self.config = path
            path.parent.mkdir(parents=True, exist_ok=True)
            self.write_config()
            for action in ("install", "grant", "revoke"):
                with self.subTest(path=path, action=action):
                    self.system.calls.clear()
                    status, report = self.execute(action)
                    self.assertEqual(status, 2, report)
                    self.assertEqual(report, "配置 / 预检错误：isolation: 配置文件必须位于 code_dir 或 protected 目录内，"
                                     f"否则 agent 账号可能改写它：{path.resolve()}")
                    self.assertEqual(self.system.mutations, [])
                    self.assertFalse(any(c[0] == "readline" for c in self.system.calls))

    def test_uninstall_accepts_config_outside_managed_folders(self):
        self.install_ok()
        self.config = self.root / "outside.toml"
        self.write_config()
        status, report = self.execute("uninstall")
        self.assertEqual(status, 0, report)
        self.assertTrue(any(c[0] == "run" and c[3] for c in self.system.calls))

    def test_non_windows_and_elevation_refuse_without_writes(self):
        self.system.windows = False
        self.assertEqual(self.execute()[0], 2)
        self.assertEqual(self.system.mutations, [])
        self.system.windows, self.system.is_elevated = True, False
        for action in ("install", "grant", "revoke", "uninstall"):
            with self.subTest(action=action):
                self.assertEqual(self.execute(action), (2, "请在“以管理员身份运行”的终端中执行"))
                self.assertEqual(self.system.mutations, [])
        with patch.object(install.sys, "platform", "linux"), patch.object(install, "System") as native:
            self.assertEqual(install.execute(self.config, "install")[0], 2)
            native.assert_not_called()

    def test_configuration_errors_do_not_mutate(self):
        cases = [(key, value) for key in ("svc_user", "agent_user", "owner_user", "code_dir", "backup_root", "data_dir", "tools_dir")
                 for value in (False, [], "", "\x00")]
        cases += [(key, value) for key in ("protected", "agent_read", "agent_write", "code_readers")
                  for value in (False, "wrong", [""], [1])]
        cases += [(key, "S-1-1-0") for key in ("svc_user", "agent_user", "owner_user")]
        cases += [("code_readers", ["S-1-1-0"]), ("code_readers", ["SVC"]), ("code_readers", ["agent"]),
                  ("owner_user", "missing"), ("code_readers", ["missing"])]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.system.calls.clear()
                self.write_config({key: value})
                status, report = self.execute()
                self.assertEqual(status, 2, report)
                self.assertIn(key, report)
                self.assertEqual(self.system.mutations, [])

    def test_managed_path_refusals_and_non_ntfs_precede_writes(self):
        refused = [self.root.anchor, "Windows", "Windows/child", "Users", "Users/owner", "backup", "backup/child",
                   "data", "data/child", "protected/child", "."]
        for path in refused:
            with self.subTest(path=path):
                self.write_config({"code_dir": str(path)})
                self.assertEqual(self.execute()[0], 2)
                self.assertEqual(self.system.mutations, [])
        self.write_config({"protected": ["protected", "protected"]})
        self.assertEqual(self.execute()[0], 2)
        self.write_config()
        self.system.filesystem = "ReFS"
        status, report = self.execute()
        self.assertEqual(status, 2)
        self.assertIn("NTFS", report)
        self.assertEqual(self.system.mutations, [])

    def test_only_exact_yes_mutates(self):
        for answer in ("", "yes", "Yes", " YES", "YES ", "Y", "NO"):
            with self.subTest(answer=answer):
                self.system.answer = answer
                self.assertEqual(self.execute(), (1, "已取消，未做任何更改"))
                self.assertEqual(self.system.mutations, [])
        self.assertIn("NetUserAdd", "\n".join(self.system.output))
        self.assertIn("/save", "\n".join(self.system.output))
        self.assertIn("/reset", "\n".join(self.system.output))

    def test_backup_and_script_before_accounts_and_managed_acls(self):
        manifest = self.install_ok()
        calls = self.system.calls
        first_change = next(i for i, c in enumerate(calls) if c[0] == "create")
        saves = [i for i, c in enumerate(calls) if c[0] == "run" and "/save" in c[1]]
        self.assertEqual(len(saves), 2)
        self.assertTrue(all(i < first_change for i in saves))
        script_index = next(i for i, c in enumerate(calls) if c[0] == "write" and c[1].name == "restore-acl.ps1")
        self.assertLess(max(saves), script_index)
        self.assertLess(script_index, first_change)
        targets = [i for i, c in enumerate(calls) if c[0] == "acl" and c[1] != self.backup]
        self.assertTrue(all(script_index < i for i in targets))
        self.assertTrue(manifest["first_install"])
        self.assertEqual(manifest["accounts_existed"], {"svc": False, "agent": False})
        self.assertEqual(manifest["created"], ["svc", "agent"])
        self.assertEqual(manifest["config"], str(self.config))
        self.assertEqual(manifest["sids"][install.ADMIN], install.ADMIN)
        for entry in manifest["folders"]:
            if entry["existed_before"]:
                call = next(c for c in calls if c[0] == "run" and "/save" in c[1] and entry["acl"] in c[1])
                self.assertEqual(call[2], Path(entry["parent"]))
                self.assertEqual(call[1][1], Path(entry["path"]).name)
        for suffix in ("secrets", "ledger", "queue/inbox", "work", "logs"):
            self.assertTrue((self.root / "data" / suffix).is_dir())
        self.assertTrue(next(self.backup.glob("*/before-check.txt")).exists())
        self.assertTrue(next(self.backup.glob("*/after-check.txt")).exists())

    def test_each_step_failure_stops_and_hints_only_after_script(self):
        cases = {"backup": lambda c: c[0] == "run" and "/save" in c[1],
                 "accounts": lambda c: c[0] == "create",
                 "folders": lambda c: c[0] == "mkdir" and c[1] == self.root / "data",
                 "acl": lambda c: c[0] == "acl" and c[1] == self.root / "tools",
                 "grants": lambda c: c[0] == "run" and str(self.root / "read") in c[1]}
        for label, predicate in cases.items():
            with self.subTest(step=label):
                # Each case gets a distinct root: no previous failure can influence this one.
                self.system = FakeSystem(self.root)
                self.write_config({"backup_root": "backup-" + label})
                self.system.fail = predicate
                status, report = self.execute()
                self.assertEqual(status, 1, report)
                self.assertIn("injected", report)
                self.assertEqual("管理员手动还原命令" in report, label != "backup")
                index = next(i for i, c in enumerate(self.system.calls) if predicate(c))
                self.assertEqual(index, len(self.system.calls) - 1)
                if label != "backup":
                    self.assertIn("powershell.exe -NoProfile -ExecutionPolicy Bypass -File", report)

    def test_existing_accounts_no_passwords_and_new_backup(self):
        self.install_ok()
        self.system.calls.clear()
        manifest = self.install_ok()
        self.assertEqual(len(self.manifests()), 2)
        self.assertFalse(manifest["first_install"])
        self.assertEqual(manifest["created"], [])
        self.assertFalse(any(c[0] in ("password", "create") for c in self.system.calls))
        latest = sorted(self.backup.glob("*/restore-acl.ps1"))[-1].read_text(encoding="utf-8-sig")
        self.assertIn("注意：此备份拍摄时隔离已安装", latest)

    def test_existing_backup_is_recursively_locked_before_all_three_commands(self):
        self.backup.mkdir()
        child = self.backup / "wide child"
        child.mkdir()
        (child / "file.txt").write_text("owner-created", encoding="utf-8")
        for action in ("install", "grant", "revoke"):
            with self.subTest(action=action):
                self.system = FakeSystem(self.root)
                self.system.accounts.update({name: {"sid": f"S-1-5-21-{index}", "administrator": False}
                                             for index, name in enumerate(("svc", "agent"), 100)})
                self.system.backup_permissions = {(sid, path): isolation.Rights(isolation.DANGEROUS[1])
                                                 for _, sid in isolation.GROUPS for path in (self.backup, child, child / "file.txt")}
                status, report = self.execute(action)
                self.assertEqual(status, 0, report)
                calls = [c for c in self.system.calls if c[0] == "acl" and c[1] == self.backup]
                expected = ("acl", self.backup, [(self.system.accounts["owner"]["sid"], install.FULL),
                            (install.ADMIN, install.FULL), (install.SYSTEM, install.FULL)], True)
                self.assertEqual(calls, [expected])
                self.assertEqual(self.system.mutations[0], expected)

    def test_backup_and_managed_folders_never_overlap_for_any_action(self):
        self.system.accounts["agent"] = {"sid": "S-1-5-21-100", "administrator": False}
        self.system.answer = "NO"
        for managed in ("data", "tools", "code space's", "protected"):
            for backup in (managed + "/backup", managed, "."):
                self.write_config({"backup_root": backup})
                for action in ("install", "grant", "revoke", "uninstall"):
                    with self.subTest(managed=managed, backup=backup, action=action):
                        self.system.calls.clear()
                        status, report = self.execute(action)
                        self.assertEqual(status, 2, report)
                        self.assertIn("isolation.backup_root", report)
                        self.assertEqual(self.system.mutations, [])
                        self.assertFalse(any(c[0] == "readline" for c in self.system.calls))

    def test_save_failure_reports_only_backup_directory_changed(self):
        self.system.fail = lambda c: c[0] == "run" and "/save" in c[1]
        status, report = self.execute()
        self.assertEqual(status, 1, report)
        self.assertIn("受管目录尚未改动，仅创建/收紧了备份目录", report)
        self.assertFalse(any(c[0] == "create" or c[0] == "acl" and c[1] != self.backup for c in self.system.calls))

    def test_existing_administrator_stops_without_touching_it(self):
        self.system.accounts["svc"] = {"sid": "S-1-5-21-99", "administrator": True}
        status, report = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("已是管理员", report)
        self.assertIn("管理员手动还原命令", report)
        self.assertFalse(any(c[0] == "create" for c in self.system.calls))

    def test_password_retry_limits_no_secret_in_output_logs_files_and_buffers_erased(self):
        secret = "T76-private-password-7654"
        self.system.passwords = ["short", "short", secret, secret + "mismatch", secret, secret, secret, secret]
        self.install_ok()
        self.assertEqual(len([c for c in self.system.calls if c[0] == "password"]), 8)
        self.assertNotIn(secret, repr(self.system.calls) + repr(self.system.output))
        for path in self.backup.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())
                self.assertNotIn(secret.encode("utf-16-le"), path.read_bytes())
        self.assertTrue(all(ct.string_at(ct.addressof(buffer), ct.sizeof(buffer)) == bytes(ct.sizeof(buffer))
                            for buffer in self.system.buffers))

    def test_three_password_failures_stop_and_native_failure_also_erases_buffer(self):
        self.system.passwords = ["short"] * 6
        status, report = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("连续三次", report)
        self.assertFalse(any(c[0] == "create" for c in self.system.calls))
        self.system.passwords = []
        self.system.fail = lambda c: c[0] == "create"
        self.assertEqual(self.execute()[0], 1)
        self.assertTrue(self.system.buffers)
        self.assertTrue(all(ct.string_at(ct.addressof(buffer), ct.sizeof(buffer)) == bytes(ct.sizeof(buffer))
                            for buffer in self.system.buffers))

    def test_twelve_character_password_containing_nul_never_creates_account(self):
        password = "abcde\x00fghijk"
        self.assertEqual(len(password), 12)
        self.system.passwords = [password] * 6
        status, report = self.execute()
        self.assertEqual(status, 1, report)
        self.assertEqual(self.system.output.count("密码不能包含空字符"), 3)
        self.assertIn("连续三次", report)
        self.assertFalse(any(c[0] == "create" for c in self.system.calls))
        self.assertEqual(self.system.buffers, [])

    def test_password_length_counts_python_characters(self):
        self.system.passwords = ["😀" * 6] * 2 + ["😀" * 12] * 4
        self.install_ok()
        self.assertEqual(self.system.output.count("密码至少 12 位，且两次必须一致"), 1)
        self.assertEqual(len([c for c in self.system.calls if c[0] == "password"]), 6)
        self.assertEqual(len([c for c in self.system.calls if c[0] == "create"]), 2)

    def test_exact_target_dacls_by_sid(self):
        self.install_ok()
        owner, svc, agent, reader = (self.system.accounts[n]["sid"] for n in ("owner", "svc", "agent", "reader"))
        common = [(owner, install.FULL), (install.ADMIN, install.FULL), (install.SYSTEM, install.FULL)]
        expected = {self.backup: common, self.root / "protected": common,
                    self.root / "code space's": [*common, (svc, install.RX), (reader, install.RX)],
                    self.root / "tools": [*common, (svc, install.RX), (agent, install.RX)],
                    self.root / "data": [(install.ADMIN, install.FULL), (install.SYSTEM, install.FULL), (svc, install.FULL)]}
        actual = {c[1]: c[2] for c in self.system.calls if c[0] == "acl"}
        self.assertEqual(actual, expected)
        order = [c[1] for c in self.system.calls if c[0] == "acl"]
        self.assertEqual(order[1:], [self.root / n for n in ("tools", "data", "code space's", "protected")])
        for suffix, right in (("ledger", "RX"), ("queue/inbox", "M")):
            self.assertTrue(any(c[0] == "run" and c[1] == ["icacls", str(self.root / "data" / suffix),
                                "/grant", f"*{owner}:(OI)(CI){right}"] for c in self.system.calls))

    def test_restore_bom_parents_quoting_and_only_created_accounts(self):
        self.system.accounts["svc"] = {"sid": "S-1-5-21-99", "administrator": False}
        self.write_config({"backup_root": "back up's"})
        self.assertEqual(self.execute()[0], 0)
        script = next((self.root / "back up's").glob("*/restore-acl.ps1"))
        self.assertTrue(script.read_bytes().startswith(b"\xef\xbb\xbf#Requires -RunAsAdministrator"))
        text = script.read_text(encoding="utf-8-sig")
        manifest = json.loads((script.parent / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("$ErrorActionPreference = 'Stop'", text)
        self.assertIn("-cne 'YES'", text)
        self.assertIn("是否删除账号 agent？输入 DELETE 删除，直接回车跳过", text)
        self.assertNotIn("Remove-LocalUser -Name 'svc'", text)
        for entry in manifest["folders"]:
            if entry["existed_before"]:
                self.assertIn(f"& icacls {install._quote(entry['parent'])} /restore {install._quote(entry['acl'])} /C", text)
        self.assertEqual(text.count(" /restore "), 2)
        self.assertIn("back up''s", text)
        self.assertIn("code space''s", text)
        self.assertIn("原先不存在，不会删除，可手动移除", text)
        self.assertNotIn("Remove-Item", text)

    def test_follow_up_failure_keeps_report_and_restore_hint(self):
        self.system.check_result = (1, "检查项\t结果\t路径\n权限\t不通过\tx")
        status, report = self.execute()
        self.assertEqual(status, 1)
        self.assertIn("不通过", report)
        self.assertIn("管理员手动还原命令", report)
        self.assertIn("不通过", next(self.backup.glob("*/after-check.txt")).read_text(encoding="utf-8"))

    def test_grant_and_revoke_exact_aces_and_history_union(self):
        self.install_ok()
        self.system.calls.clear()
        status, report = self.execute("grant")
        self.assertEqual(status, 0, report)
        sid = self.system.accounts["agent"]["sid"]
        commands = [c[1] for c in self.system.calls if c[0] == "run"]
        self.assertEqual(commands, [["icacls", str(self.root / "read"), "/grant", f"*{sid}:(OI)(CI)RX"],
                                    ["icacls", str(self.root / "write"), "/grant", f"*{sid}:(OI)(CI)M"]])
        history = [json.loads(line) for line in (self.backup / "grants.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(set(row) == {"time", "path", "right"} for row in history))
        self.write_config({"agent_read": [], "agent_write": []})
        self.system.calls.clear()
        self.assertEqual(self.execute("revoke")[0], 0)
        commands = [c[1] for c in self.system.calls if c[0] == "run"]
        self.assertEqual(commands, [["icacls", str(self.root / p), "/remove:g", f"*{sid}"] for p in ("read", "write")])
        self.assertEqual(self.system.calls[-1][0], "check")

    def test_revoke_only_deliberate_grant_failures_explains_expected_check(self):
        self.install_ok()
        read = f"agent_read 主动授权（不可读）\t不通过\t{self.root / 'read'}"
        write = f"agent_write 主动授权（不可写）\t不通过\t{self.root / 'write'}"
        for failures in ([read], [write], [read, write]):
            with self.subTest(failures=failures):
                check_report = "\n".join(["检查项\t结果\t路径", "svc 非管理员\t通过\t-", *failures,
                                           f"汇总：通过 1，不通过 {len(failures)}，信息 0"])
                self.system.check_result = (1, check_report)
                self.system.calls.clear()
                status, report = self.execute("revoke")
                self.assertEqual(status, 1)
                self.assertEqual(report, "撤销已完成；配置中仍列有这些授权路径，check 的主动授权行因此不通过（预期）。"
                                         "如不再需要，请从配置中删除这些路径。\n" + check_report)
                sid = self.system.accounts["agent"]["sid"]
                self.assertEqual([c[1] for c in self.system.calls if c[0] == "run"],
                                 [["icacls", str(self.root / p), "/remove:g", f"*{sid}"] for p in ("read", "write")])
                self.assertEqual(self.system.calls[-1][0], "check")

    def test_other_check_failures_keep_original_partial_change_message(self):
        self.install_ok()
        active = "agent_read 主动授权（不可读）\t不通过\tread"
        private = "agent 不可读框架私有目录（可读）\t不通过\tprivate"
        cases = [("revoke", 1, [private]), ("revoke", 1, [active, private]),
                 ("revoke", 1, ["agent_read 主动授权附加行\t不通过\tread"]), ("revoke", 1, []),
                 ("revoke", 2, ["Windows 权限查询\t不通过\t-"]),
                 ("install", 1, [active]), ("grant", 1, [active])]
        for action, code, failures in cases:
            with self.subTest(action=action, code=code, failures=failures):
                check_report = "\n".join(["检查项\t结果\t路径", *failures,
                                           f"汇总：通过 0，不通过 {len(failures)}，信息 0"])
                self.system.check_result = (code, check_report)
                status, report = self.execute(action)
                self.assertEqual(status, 1)
                self.assertTrue(report.startswith("操作失败：后续 isolation check 未通过\n" + check_report), report)
                self.assertIn("更改可能只完成了一部分", report)
                self.assertNotIn("撤销已完成", report)

    def test_revoke_command_failure_keeps_original_partial_change_message(self):
        self.install_ok()
        self.system.check_result = (1, "检查项\t结果\t路径\nagent_read 主动授权（不可读）\t不通过\tread")
        self.system.fail = lambda call: call[0] == "run" and "/remove:g" in call[1]
        self.system.calls.clear()
        status, report = self.execute("revoke")
        self.assertEqual(status, 1)
        self.assertIn("操作失败：injected run", report)
        self.assertIn("更改可能只完成了一部分", report)
        self.assertNotIn("撤销已完成", report)
        self.assertFalse(any(c[0] == "check" for c in self.system.calls))

    def test_grant_refuses_all_managed_backup_paths_and_missing_paths_without_mutation(self):
        for key in ("agent_read", "agent_write"):
            for path in ("data", "data/child", "protected", "protected/sub", "code space's", "code space's/child",
                         "tools", "tools/sub", "backup", "backup/sub", "missing"):
                for action in ("install", "grant"):
                    with self.subTest(key=key, path=path, action=action):
                        self.write_config({key: [path]})
                        status, report = self.execute(action)
                        self.assertEqual(status, 2, report)
                        self.assertIn(key, report)
                        self.assertEqual(self.system.mutations, [])

    def test_cancel_grant_revoke_and_missing_agent_do_not_mutate(self):
        for action in ("grant", "revoke"):
            self.assertEqual(self.execute(action)[0], 2)
            self.assertEqual(self.system.mutations, [])
        self.system.accounts["agent"] = {"sid": "S-1-5-21-100", "administrator": False}
        self.system.answer = "no"
        for action in ("grant", "revoke"):
            self.assertEqual(self.execute(action), (1, "已取消，未做任何更改"))
            self.assertEqual(self.system.mutations, [])

    def test_uninstall_selects_newest_first_install_and_preserves_exit_code(self):
        self.assertEqual(self.execute("uninstall")[0], 2)
        self.assertEqual(self.system.mutations, [])
        self.install_ok()
        initial = next(self.backup.glob("*/manifest.json"))
        # A later first-install backup wins over the original; a still newer re-run must not win.
        newer = self.backup / "later-first"
        newer.mkdir()
        (newer / "manifest.json").write_bytes(initial.read_bytes())
        (newer / "restore-acl.ps1").write_text("test", encoding="utf-8-sig")
        os.utime(newer / "manifest.json", ns=(initial.stat().st_atime_ns, initial.stat().st_mtime_ns + 10**9))
        self.install_ok()
        self.system.calls.clear()
        status, report = self.execute("uninstall")
        self.assertEqual(status, 0, report)
        command = next(c for c in self.system.calls if c[0] == "run")
        self.assertEqual(command[1], ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                    str(newer / "restore-acl.ps1")])
        self.assertTrue(command[3])
        self.assertFalse(any(c[0] == "readline" for c in self.system.calls))  # The script owns its YES prompt.
        with patch.object(self.system, "run", return_value=7):
            self.assertEqual(self.execute("uninstall")[0], 7)

    def test_uninstall_rejects_dangerous_or_generic_rights_on_all_three_paths(self):
        self.install_ok()
        backup = next(self.backup.glob("*/manifest.json")).parent
        masks = [isolation.Rights(1 << bit) for bit in range(32) if isolation.DANGEROUS[1] & (1 << bit)]
        for path in (self.backup, backup, backup / "restore-acl.ps1"):
            for name in ("svc", "agent", *(sid for _, sid in isolation.GROUPS)):
                for rights in [*masks, isolation.Rights(0, True)]:
                    with self.subTest(path=path, name=name, rights=rights):
                        self.system.calls.clear()
                        self.system.backup_permissions = {(name, path): rights}
                        status, report = self.execute("uninstall")
                        self.assertEqual(status, 2, report)
                        self.assertIn(f"备份目录或复原脚本可被非管理员改写，拒绝执行：{path}", report)
                        self.assertEqual(self.system.mutations, [])
                        self.assertIn(("backup_rights", name, path), self.system.calls)

    def test_reparse_backup_root_is_rejected_before_any_mutation(self):
        self.backup.mkdir()
        self.system.reparse_points.add(self.backup)
        self.system.accounts["agent"] = {"sid": "S-1-5-21-100", "administrator": False}
        for action in ("install", "grant", "revoke", "uninstall"):
            with self.subTest(action=action):
                self.system.calls.clear()
                status, report = self.execute(action)
                self.assertEqual(status, 2, report)
                self.assertIn(f"备份路径含符号链接或目录联接，拒绝执行：{self.backup}", report)
                self.assertEqual(self.system.mutations, [])
                self.assertFalse(any(c[0] == "readline" for c in self.system.calls))

    def test_reparse_detection_uses_windows_lstat_and_portable_islink(self):
        with patch.object(install.os, "name", "nt"), patch.object(install.os, "lstat") as lstat:
            for attributes, expected in ((0x10, False), (0x410, True)):
                lstat.return_value = Mock(st_file_attributes=attributes)
                self.assertIs(install.System.is_reparse(self.backup), expected)
                lstat.assert_called_with(self.backup)
            lstat.side_effect = FileNotFoundError()
            self.assertFalse(install.System.is_reparse(self.backup))
            lstat.side_effect = PermissionError("query denied")
            with self.assertRaises(PermissionError):
                install.System.is_reparse(self.backup)
        with patch.object(install.os, "name", "posix"), patch.object(install.os.path, "islink") as islink:
            for expected in (False, True):
                islink.return_value = expected
                self.assertIs(install.System.is_reparse(self.backup), expected)
                islink.assert_called_with(self.backup)

    @unittest.skipUnless(os.name == "nt", "Windows directory junction integration")
    def test_owned_junction_backup_paths_are_skipped_or_rejected(self):
        self.install_ok()
        initial = next(self.backup.glob("*/manifest.json"))
        external = self.root / "external"
        external.mkdir()
        (external / "manifest.json").write_bytes(initial.read_bytes())
        linked = self.backup / "junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(linked), str(external)], capture_output=True)
        if result.returncode:
            self.skipTest("无法在测试自建临时目录中创建目录联接")
        def cleanup_link():
            try:
                linked.rmdir()  # Remove only the junction; the target is another test-owned folder.
            except OSError:
                pass
        self.addCleanup(cleanup_link)
        self.assertTrue(install.System.is_reparse(linked))
        os.utime(external / "manifest.json", ns=(initial.stat().st_atime_ns, initial.stat().st_mtime_ns + 10**9))
        with patch.object(self.system, "is_reparse", side_effect=install.System.is_reparse):
            self.system.calls.clear()
            status, report = self.execute("uninstall")
            self.assertEqual(status, 0, report)
            command = next(c for c in self.system.calls if c[0] == "run")
            self.assertEqual(command[1][-1], str(initial.parent / "restore-acl.ps1"))
            self.write_config({"backup_root": str(linked)})
            self.assertEqual(isolation._configuration(self.config)["backup_root"], linked)
            for action in ("install", "grant", "revoke", "uninstall"):
                with self.subTest(action=action):
                    self.system.calls.clear()
                    status, report = self.execute(action)
                    self.assertEqual(status, 2, report)
                    self.assertIn(f"备份路径含符号链接或目录联接，拒绝执行：{linked}", report)
                    self.assertEqual(self.system.mutations, [])

    def test_uninstall_skips_reparse_backup_candidates_before_reading_manifest(self):
        self.install_ok()
        original = next(self.backup.glob("*/manifest.json")).parent
        linked = self.backup / "linked"
        linked.mkdir()
        (linked / "manifest.json").write_text("must not be read as JSON", encoding="utf-8")
        self.system.reparse_points.add(linked)
        self.system.calls.clear()
        status, report = self.execute("uninstall")
        self.assertEqual(status, 0, report)
        command = next(c for c in self.system.calls if c[0] == "run")
        self.assertEqual(command[1][-1], str(original / "restore-acl.ps1"))
        self.assertIn(("is_reparse", linked), self.system.calls)
        self.system.reparse_points.add(original)
        self.system.calls.clear()
        self.assertEqual(self.execute("uninstall")[0], 2)
        self.assertEqual(self.system.mutations, [])

    def test_uninstall_rechecks_root_selected_backup_and_script_for_reparse_points(self):
        self.install_ok()
        backup = next(self.backup.glob("*/manifest.json")).parent
        script = backup / "restore-acl.ps1"
        for target in (self.backup, backup, script):
            with self.subTest(target=target):
                seen = []
                def changed_to_reparse(path):
                    seen.append(path)
                    return path == target and (path == script or seen.count(path) > 1)
                self.system.calls.clear()
                with patch.object(self.system, "is_reparse", side_effect=changed_to_reparse):
                    status, report = self.execute("uninstall")
                self.assertEqual(status, 2, report)
                self.assertIn(f"备份路径含符号链接或目录联接，拒绝执行：{target}", report)
                self.assertEqual(self.system.mutations, [])

    def test_uninstall_rejects_backup_resolving_outside_root(self):
        self.install_ok()
        backup = next(self.backup.glob("*/manifest.json")).parent
        resolve = Path.resolve
        def outside(path, *args, **kwargs):
            return self.root / "external" / path.name if path == backup else resolve(path, *args, **kwargs)
        self.system.calls.clear()
        with patch.object(Path, "resolve", outside):
            status, report = self.execute("uninstall")
        self.assertEqual(status, 2, report)
        self.assertIn(f"备份路径含符号链接或目录联接，拒绝执行：{backup}", report)
        self.assertEqual(self.system.mutations, [])

    def test_uninstall_allows_readonly_backup_and_skips_absent_accounts(self):
        self.install_ok()
        backup = next(self.backup.glob("*/manifest.json")).parent
        paths = (self.backup, backup, backup / "restore-acl.ps1")
        self.system.accounts.pop("svc")
        self.system.accounts.pop("agent")
        self.system.backup_permissions = {(sid, path): isolation.Rights(0x1200A9)
                                         for _, sid in isolation.GROUPS for path in paths}
        self.system.calls.clear()
        status, report = self.execute("uninstall")
        self.assertEqual(status, 0, report)
        for path in paths:
            for name in ("svc", "agent", *(sid for _, sid in isolation.GROUPS)):
                self.assertIn(("backup_rights", name, path), self.system.calls)
        self.assertTrue(any(c[0] == "run" and c[3] for c in self.system.calls))

    def test_uninstall_query_error_is_not_safe_to_execute(self):
        self.install_ok()
        self.system.backup_errors["S-1-1-0", self.backup] = OSError("Authz query failed")
        self.system.calls.clear()
        status, report = self.execute("uninstall")
        self.assertEqual(status, 2, report)
        self.assertIn("Authz query failed", report)
        self.assertEqual(self.system.mutations, [])

    def test_backup_queries_use_t75_account_and_builtin_contexts(self):
        system = object.__new__(install.System)
        account, group, rights = isolation.Sid("account"), isolation.Sid("group"), isolation.Rights(0)
        system.account_sid = Mock(return_value=account)
        system.sid = Mock(return_value=group)
        system.rights = Mock(return_value=rights)
        for name in ("svc", "agent", *(sid for _, sid in isolation.GROUPS)):
            self.assertIs(system.backup_rights(name, self.backup), rights)
            system.rights.assert_called_with(account if name in ("svc", "agent") else group, self.backup)
        self.assertEqual(system.account_sid.call_args_list, [unittest.mock.call("svc"), unittest.mock.call("agent")])
        self.assertEqual(system.sid.call_args_list, [unittest.mock.call(sid) for _, sid in isolation.GROUPS])
        system.account_sid.return_value = None
        system.rights.reset_mock()
        self.assertIsNone(system.backup_rights("absent", self.backup))
        system.rights.assert_not_called()

    def test_cli_dispatch_never_opens_ledger(self):
        for action in ("install", "grant", "revoke", "uninstall"):
            output = io.StringIO()
            with patch.object(install, "execute", return_value=(1, "test report")) as execute, \
                    patch("m2k.cli._ledger", side_effect=AssertionError("ledger opened")), redirect_stdout(output):
                self.assertEqual(main(["--config", str(self.config), "isolation", action]), 1)
            execute.assert_called_once_with(self.config, action)
            self.assertEqual(output.getvalue(), "test report\n")

    def test_native_command_records_arguments_exit_and_output_before_raising(self):
        system = object.__new__(install.System)
        system.log = self.root / "install.log"
        command = ["icacls", self.root / "code space's", "/reset", "/T", "/C", "/Q"]
        result = subprocess.CompletedProcess(command, 5, "some stdout", "some stderr")
        with patch.object(install.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(OSError, "退出码 5"):
                system.run(command, cwd=self.root)
        self.assertEqual(run.call_args.args, (list(map(str, command)),))
        self.assertNotIn("shell", run.call_args.kwargs)
        record = json.loads(system.log.read_text(encoding="utf-8"))
        self.assertEqual(record["exit"], 5)
        self.assertEqual(record["output"], "some stdoutsome stderr")
        self.assertEqual(record["command"], list(map(str, command)))
        self.assertEqual(record["cwd"], str(self.root))

    def test_interactive_command_inherits_console_and_logs_only_placeholder(self):
        command = ["powershell.exe", "-NoProfile", "-File", self.root / "restore script's.ps1"]
        for code in (0, 7):
            with self.subTest(code=code):
                system = object.__new__(install.System)
                system.log = self.root / f"interactive-{code}.log"
                result = subprocess.CompletedProcess(command, code, "unrecorded stdout", "unrecorded stderr")
                printed = io.StringIO()
                with patch.object(install.subprocess, "run", return_value=result) as run, \
                        patch.object(install.subprocess, "Popen", side_effect=AssertionError("output pipe used")), \
                        redirect_stdout(printed):
                    self.assertEqual(system.run(command, cwd=self.root, interactive=True), code)
                run.assert_called_once_with(list(map(str, command)), cwd=self.root)
                self.assertEqual(printed.getvalue(), "")
                record = json.loads(system.log.read_text(encoding="utf-8"))
                self.assertEqual(record, {"command": list(map(str, command)), "cwd": str(self.root), "exit": code,
                                          "output": "(交互输出直接显示在控制台，未记录)"})
                self.assertNotIn("unrecorded", system.log.read_text(encoding="utf-8"))


@unittest.skipUnless(os.name == "nt", "Windows ACL integration")
class WindowsAclTest(unittest.TestCase):
    def test_restore_script_parses_in_windows_powershell(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            manifest = {"time": "2026-10-27", "config": str(root / "config space's.toml"), "first_install": True,
                        "created": ["test-user's"], "folders": [{"path": str(root / "tree space's"),
                        "parent": str(root), "existed_before": True, "acl": str(root / "back up's.acl")}]}
            script = root / "restore script's.ps1"
            script.write_text(install.restore_script(manifest), encoding="utf-8-sig")
            command = "$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile(" + \
                install._quote(script) + ", [ref]$tokens, [ref]$errors) | Out-Null; $errors | Out-String; exit $errors.Count"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _owned_tree(self, system):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        tree = root / "tree space's"
        tree.mkdir()
        (tree / "file.txt").write_text("owner-created", encoding="utf-8")
        for name in ("sub", "explicit", "no-inherit"):
            (tree / name).mkdir()
            (tree / name / "child.txt").write_text(name, encoding="utf-8")
        owner = system.account(os.environ["USERNAME"])["sid"]
        system.acl(tree, [(owner, install.FULL), (install.SYSTEM, install.FULL), (install.ADMIN, install.FULL)])
        system.run(["icacls", tree / "explicit", "/grant", "*S-1-1-0:(OI)(CI)M"])
        system.run(["icacls", tree / "no-inherit", "/inheritance:d"])
        original = root / "before.acl"
        system.run(["icacls", tree.name, "/save", original, "/T", "/C"], cwd=root)
        return root, tree, original, owner

    def test_owned_tree_acl_reset(self):
        with install.System() as system:
            root, tree, _, owner = self._owned_tree(system)
            system.acl(tree, [(owner, install.FULL), ("S-1-1-0", install.RX)])
            changed = root / "changed.acl"
            system.run(["icacls", tree.name, "/save", changed, "/T", "/C"], cwd=root)
            text = changed.read_text(encoding="utf-16-le")
            self.assertIn("(A;OICIID;0x1200a9;;;WD)", text)
            self.assertNotIn("(A;OICI;0x1301bf;;;WD)", text)
            with install.WindowsBackend() as backend:
                for name in ("sub", "explicit", "no-inherit"):
                    rights = backend.rights(backend.sid("S-1-1-0"), tree / name)
                    self.assertEqual(rights.mask, 0x1200A9)

    @unittest.skipUnless(os.name == "nt" and _token_elevated(), "icacls /restore 需要管理员（SeRestorePrivilege）")
    def test_owned_tree_byte_identical_restore(self):
        with install.System() as system:
            root, tree, original, owner = self._owned_tree(system)
            system.acl(tree, [(owner, install.FULL), ("S-1-1-0", install.RX)])
            # Exactly the command emitted by restore_script(); run only with an elevated token.
            system.run(["icacls", root, "/restore", original, "/C"])
            restored = root / "restored.acl"
            system.run(["icacls", tree.name, "/save", restored, "/T", "/C"], cwd=root)
            self.assertEqual(restored.read_bytes(), original.read_bytes(),
                             "原始 /save：\n" + original.read_text(encoding="utf-16-le") +
                             "\n还原后 /save：\n" + restored.read_text(encoding="utf-16-le"))

    @unittest.skipUnless(os.name == "nt" and _token_elevated(), "icacls /restore 需要管理员（SeRestorePrivilege）")
    def test_legacy_acl_restore_inheritance(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        with install.System() as system:
            owner = system.account(os.environ["USERNAME"])["sid"]
            entries = [(owner, install.FULL), (install.SYSTEM, install.FULL), (install.ADMIN, install.FULL)]
            system.acl(root, [*entries, ("S-1-1-0", install.RX)])
            tree, sub = root / "tree", root / "tree/sub"
            sub.mkdir(parents=True)
            descriptor = ct.c_void_p()
            convert = install._api("advapi32", "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                                   [ct.c_wchar_p, ct.c_uint32, ct.c_void_p, ct.c_void_p])
            set_legacy = install._api("advapi32", "SetFileSecurityW",
                                      [ct.c_wchar_p, ct.c_uint32, ct.c_void_p])
            sddl = "D:" + "".join(f"(A;OICI;{right};;;{sid})" for sid, right in entries)
            install._ok(convert(sddl, 1, ct.byref(descriptor), None))
            try:
                for path in (tree, sub):
                    install._ok(set_legacy(str(path), 4, descriptor))
            finally:
                system._free(descriptor)

            def save(destination):
                system.run(["icacls", tree.name, "/save", destination, "/T", "/C"], cwd=root)
                lines = destination.read_text(encoding="utf-16-le").splitlines()
                return dict(zip(lines[::2], lines[1::2]))

            everyone = system.sid("S-1-1-0")
            original = root / "before.acl"
            before = save(original)
            before_masks = {str(path.relative_to(root)): system.rights(everyone, path).mask for path in (tree, sub)}
            if not before[tree.name].startswith("D:(") or before_masks[tree.name]:
                # Report-only investigation: an unexpected baseline is recorded, not a CI failure.
                print("T78-LEGACY: baseline not legacy " + json.dumps({"before": before, "everyone_before": {
                      name: f"0x{mask:08X}" for name, mask in before_masks.items()}}, ensure_ascii=False))
                self.skipTest("baseline is not a legacy DACL on this machine")
            system.acl(tree, [(owner, install.FULL)])
            code = system.run(["icacls", root, "/restore", original, "/C"])
            after = save(root / "restored.acl")
            after_masks = {str(path.relative_to(root)): system.rights(everyone, path).mask for path in (tree, sub)}
            print("T78-LEGACY: " + json.dumps({"before": before, "after": after,
                  "everyone_before": {name: f"0x{mask:08X}" for name, mask in before_masks.items()},
                  "everyone_after": {name: f"0x{mask:08X}" for name, mask in after_masks.items()}}, ensure_ascii=False))
            self.assertEqual(code, 0)
            for name, line in before.items():
                for ace in line.split("(")[1:]:
                    if "ID" not in ace.split(";")[1]:
                        self.assertIn("(" + ace, after[name], name)


if __name__ == "__main__":
    unittest.main()
