import ast
import csv
import inspect
import io
import os
import socket
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools import sign_preflight as preflight


def csv_text(rows):
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    return output.getvalue()


def task(name, command="helper.exe", state="Enabled"):
    return ["HOST", name, "N/A", "Ready", "Interactive", "N/A", "0", "Owner", command,
            "N/A", "N/A", state]


class SignPreflightTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(preflight.platform, "system", return_value="Windows"))
        self.enterContext(patch.object(preflight.os, "getpid", return_value=1234))
        self.enterContext(patch.dict(os.environ, {"APPDATA": "C:/Users/Owner/AppData/Roaming",
                                                 "ProgramData": "C:/ProgramData"}))
        self.exists = self.enterContext(patch.object(preflight.os.path, "exists", return_value=False))
        self.connect = self.enterContext(patch.object(preflight.socket, "create_connection",
                                                    side_effect=OSError("offline")))
        self.dns = self.enterContext(patch.object(preflight.socket, "gethostbyname",
                                                side_effect=socket.gaierror("offline")))
        self.processes = [["python.exe", "1234"], ["explorer.exe", "99"]]
        self.header = ["HostName", "TaskName"] + ["column"] * 10
        self.tasks = []
        self.adapters = ""
        self.registry = {key: "" for key in preflight.RUN_KEYS}
        self.overrides = {}
        self.run = self.enterContext(patch.object(preflight.subprocess, "run", side_effect=self.command))

    def command(self, args, **kwargs):
        self.assertEqual(kwargs, {"capture_output": True, "text": True, "timeout": 10})
        override = self.overrides.get(args[0])
        if isinstance(override, BaseException):
            raise override
        if override is not None:
            return override
        if args[0] == "powershell":
            self.assertEqual(args[1:3], ["-NoProfile", "-Command"])
            self.assertIn("Get-NetAdapter -ErrorAction Stop", args[3])
            output = self.adapters
        elif args[0] == "tasklist":
            self.assertEqual(args[1:], ["/FO", "CSV", "/NH"])
            output = csv_text(self.processes)
        elif args[0] == "schtasks":
            self.assertEqual(args[1:], ["/query", "/fo", "CSV", "/v"])
            output = csv_text([self.header, *self.tasks])
        elif args[0] == "reg":
            self.assertEqual(args[1], "query")
            output = self.registry[args[2]]
        elif args == ["ps", "-eo", "comm="]:
            output = "python\npython\nnode\ninit\n"
        elif args == ["ps", "-p", "1234", "-o", "comm="]:
            output = "python\n"
        else:
            self.fail(f"unexpected command: {args}")
        return subprocess.CompletedProcess(args, 0, output, "")

    def invoke(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = preflight.main(list(args))
        lines = output.getvalue().splitlines()
        rows = {}
        for line in lines[1:-1]:
            name, result, details = line.split(" | ", 2)
            rows[name] = (result, details)
        self.assertEqual(list(rows), ["network", "agent processes", "scheduled tasks",
                                     "startup items", "signing drive"])
        return code, rows, lines[-1]

    def test_all_clear_table_exit_and_message(self):
        code, rows, last = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(all(result == "PASS" for result, _ in rows.values()), rows)
        self.assertEqual(last, "可以插入 U 盘签发（LOOK 项请人工确认）")
        self.connect.assert_called_once_with(("1.1.1.1", 53), timeout=2)
        self.dns.assert_called_once_with("example.com")
        self.exists.assert_any_call("H:\\")

    def test_tcp_reachable_fails_even_if_adapter_check_unavailable(self):
        self.connect.side_effect = None
        self.overrides["powershell"] = FileNotFoundError("powershell missing")
        code, rows, last = self.invoke()
        self.assertEqual((code, rows["network"][0]), (1, "FAIL"))
        self.assertIn("TCP connection", rows["network"][1])
        self.assertIn("powershell missing", rows["network"][1])
        self.assertEqual(last, "不要插入 U 盘：先处理 FAIL 项")
        self.connect.return_value.__exit__.assert_called_once()

    def test_dns_success_alone_fails(self):
        self.dns.side_effect = None
        self.dns.return_value = "93.184.216.34"
        code, rows, _ = self.invoke()
        self.assertEqual((code, rows["network"][0]), (1, "FAIL"))
        self.assertIn("DNS lookup of example.com succeeded", rows["network"][1])

    def test_up_adapters_fail_and_names_are_listed(self):
        self.adapters = "Ethernet\nWi-Fi\n"
        code, rows, _ = self.invoke()
        self.assertEqual((code, rows["network"][0]), (1, "FAIL"))
        self.assertIn("Ethernet, Wi-Fi", rows["network"][1])

    def test_dns_timeout_and_unavailable_are_look(self):
        with patch.object(preflight.threading, "Thread") as thread:
            thread.return_value.is_alive.return_value = True
            code, rows, _ = self.invoke()
            self.assertEqual((code, rows["network"][0]), (0, "LOOK"))
            self.assertIn("timed out after 10 s", rows["network"][1])
            thread.return_value.join.assert_called_once_with(10)
            self.assertTrue(thread.call_args.kwargs["daemon"])
        self.dns.side_effect = OSError("resolver unavailable")
        _, rows, _ = self.invoke()
        self.assertEqual(rows["network"][0], "LOOK")
        self.assertIn("resolver unavailable", rows["network"][1])

    def test_codex_fails_and_allow_is_case_insensitive(self):
        self.processes.append(["Codex.exe", "55"])
        code, rows, _ = self.invoke()
        self.assertEqual((code, rows["agent processes"][0]), (1, "FAIL"))
        self.assertIn("Codex.exe (PID 55)", rows["agent processes"][1])
        code, rows, _ = self.invoke("--allow", "CODEX.EXE")
        self.assertEqual((code, rows["agent processes"][0]), (0, "PASS"))

    def test_own_python_exempt_but_other_python_not_exempt(self):
        self.assertEqual(self.invoke()[1]["agent processes"][0], "PASS")
        self.processes.append(["python.exe", "4321"])
        code, rows, _ = self.invoke()
        self.assertEqual((code, rows["agent processes"][0]), (1, "FAIL"))
        self.assertIn("PID 4321", rows["agent processes"][1])
        self.assertNotIn("PID 1234", rows["agent processes"][1])

    def test_builtin_process_list_and_name_allowlist(self):
        for agent in preflight.AGENTS:
            with self.subTest(agent=agent):
                name = agent.upper() + ".EXE"
                self.processes = [["python.exe", "1234"], [name, "77"]]
                self.assertEqual(self.invoke()[1]["agent processes"][0], "FAIL")
                self.assertEqual(self.invoke("--allow", agent)[1]["agent processes"][0], "PASS")
        self.processes = [["Codex.exe", "55"], ["node.exe", "66"], ["python.exe", "1234"]]
        _, rows, _ = self.invoke("--allow", "codex")
        self.assertEqual(rows["agent processes"][0], "FAIL")
        self.assertIn("node.exe", rows["agent processes"][1])
        self.assertNotIn("Codex.exe", rows["agent processes"][1])
        self.assertEqual(self.invoke("--allow", "codex", "node")[0], 0)
        self.assertEqual(self.invoke("--allow", "codex", "--allow", "node")[0], 0)

    def test_non_microsoft_enabled_tasks_are_look_and_csv_command_is_preserved(self):
        self.tasks = [task(r"\Owner\Backup", '"C:\\Tools\\backup.exe" --name "a,b"'),
                      task(r"\Microsoft\Windows\Update"), task(r"\Disabled", state="Disabled")]
        code, rows, _ = self.invoke()
        self.assertEqual((code, rows["scheduled tasks"][0]), (0, "LOOK"))
        self.assertIn(r"\Owner\Backup", rows["scheduled tasks"][1])
        self.assertIn('--name "a,b"', rows["scheduled tasks"][1])
        self.assertNotIn("Update", rows["scheduled tasks"][1])
        self.assertNotIn("Disabled", rows["scheduled tasks"][1])

    def test_task_inventory_localization_limit_and_unknown_state(self):
        self.header[1] = "任务名"
        self.tasks = [task(f"\\Owner\\task-{i:02}", state="已启用") for i in range(41)]
        self.tasks.append(task(r"\Owner\disabled", state="已禁用"))
        _, rows, _ = self.invoke()
        self.assertEqual(rows["scheduled tasks"][0], "LOOK")
        self.assertIn("task-39", rows["scheduled tasks"][1])
        self.assertNotIn("task-40", rows["scheduled tasks"][1])
        self.assertIn("1 more tasks", rows["scheduled tasks"][1])
        self.tasks = [task(r"\Owner\unknown", state="unrecognized")]
        _, rows, _ = self.invoke()
        self.assertEqual(rows["scheduled tasks"][0], "LOOK")
        self.assertIn("unknown scheduled task state", rows["scheduled tasks"][1])

    def test_microsoft_and_disabled_only_tasks_pass(self):
        self.tasks = [task(r"\microsoft\Windows\Update"), task(r"\Owner\disabled", state="Disabled")]
        self.assertEqual(self.invoke()[1]["scheduled tasks"][0], "PASS")

    def test_schtasks_repeated_folder_headers_are_skipped(self):
        self.tasks = [task(r"\Microsoft\Windows\Update"), self.header,
                      task(r"\Owner\Backup"), self.header, task(r"\Owner\Second")]
        _, rows, _ = self.invoke()
        self.assertEqual(rows["scheduled tasks"][0], "LOOK")
        self.assertIn(r"\Owner\Backup: helper.exe", rows["scheduled tasks"][1])
        self.assertIn(r"\Owner\Second: helper.exe", rows["scheduled tasks"][1])
        self.assertNotIn("cannot parse", rows["scheduled tasks"][1])

    def test_run_keys_are_both_inspected_and_listed(self):
        for key in preflight.RUN_KEYS:
            self.registry[key] = "HKEY_CURRENT_USER\\test\n    Owner Helper    REG_SZ    C:\\Helper.exe\n"
        code, rows, _ = self.invoke()
        self.assertEqual((code, rows["startup items"][0]), (0, "LOOK"))
        for key in preflight.RUN_KEYS:
            self.assertIn(key, rows["startup items"][1])
        self.assertIn("Owner Helper = C:\\Helper.exe", rows["startup items"][1])

    def test_empty_run_value_is_listed_and_does_not_hide_later_entries(self):
        self.registry[preflight.RUN_KEYS[0]] = (
            "HKEY_CURRENT_USER\\test\n    Empty Helper    REG_SZ\n"
            "    Later Helper    REG_SZ    later.exe\n")
        _, rows, _ = self.invoke()
        self.assertEqual(rows["startup items"][0], "LOOK")
        self.assertIn("Empty Helper =", rows["startup items"][1])
        self.assertIn("Later Helper = later.exe", rows["startup items"][1])
        self.assertNotIn("cannot parse", rows["startup items"][1])

    def test_missing_run_keys_pass_but_permission_failure_is_look(self):
        for message in ("ERROR: The system was unable to find the specified registry key or value.",
                        "错误: 系统找不到指定的注册表项或值。"):
            with self.subTest(message=message):
                self.overrides["reg"] = subprocess.CompletedProcess([], 1, "", message)
                self.assertEqual(self.invoke()[1]["startup items"][0], "PASS")
        self.overrides["reg"] = subprocess.CompletedProcess([], 1, "", "Access is denied.")
        _, rows, _ = self.invoke()
        self.assertEqual(rows["startup items"][0], "LOOK")
        self.assertIn("Access is denied", rows["startup items"][1])

    def test_two_startup_folders_are_listed_without_reading_entries(self):
        self.exists.side_effect = lambda path: str(path) != "H:\\"
        folders = []

        def listing(folder):
            folders.append(folder)
            return iter([folder / "Owner.lnk"])

        with patch.object(Path, "iterdir", autospec=True, side_effect=listing):
            code, rows, _ = self.invoke()
        self.assertEqual((code, rows["startup items"][0]), (0, "LOOK"))
        self.assertEqual(len(folders), 2)
        self.assertTrue(any("ProgramData" in str(folder) for folder in folders))
        self.assertTrue(any("AppData" in str(folder) for folder in folders))
        self.assertEqual(rows["startup items"][1].count("Owner.lnk"), 2)

    def test_unreadable_or_unlocatable_startup_folders_are_look(self):
        self.exists.side_effect = lambda path: str(path) != "H:\\"
        with patch.object(Path, "iterdir", side_effect=PermissionError("Startup denied")):
            _, rows, _ = self.invoke()
        self.assertEqual(rows["startup items"][0], "LOOK")
        self.assertIn("Startup denied", rows["startup items"][1])
        with patch.dict(os.environ, clear=True):
            _, rows, _ = self.invoke()
        self.assertEqual(rows["startup items"][0], "LOOK")
        self.assertIn("APPDATA unavailable", rows["startup items"][1])

    def test_signing_drive_present_is_look(self):
        self.exists.side_effect = lambda path: str(path) == "H:\\"
        code, rows, last = self.invoke()
        self.assertEqual((code, rows["signing drive"][0]), (0, "LOOK"))
        self.assertEqual(rows["signing drive"][1],
                         "USB already inserted; the checks above should be done before inserting it")
        self.assertEqual(last, "可以插入 U 盘签发（LOOK 项请人工确认）")

    def test_command_missing_timeout_and_nonzero_are_look_for_each_check(self):
        for executable, check in (("powershell", "network"), ("tasklist", "agent processes"),
                                  ("schtasks", "scheduled tasks"), ("reg", "startup items")):
            for error in (FileNotFoundError("missing command"),
                          subprocess.TimeoutExpired(executable, 10),
                          subprocess.CompletedProcess([], 2, "", "inspection failed"),
                          UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid output")):
                with self.subTest(executable=executable, error=type(error).__name__):
                    self.overrides = {executable: error}
                    code, rows, _ = self.invoke()
                    self.assertEqual((code, rows[check][0]), (0, "LOOK"))
                    self.assertIn(executable, rows[check][1])

    def test_unparseable_inventories_are_look_not_pass(self):
        for executable, check, output in (("tasklist", "agent processes", ""),
                                           ("tasklist", "agent processes", "unparseable"),
                                           ("schtasks", "scheduled tasks", "unparseable"),
                                           ("reg", "startup items", "unparseable")):
            with self.subTest(executable=executable, output=output):
                self.overrides = {executable: subprocess.CompletedProcess([], 0, output, "")}
                self.assertEqual(self.invoke()[1][check][0], "LOOK")

    def test_posix_uses_ps_and_only_one_python_is_exempt(self):
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system), patch.object(preflight.platform, "system", return_value=system):
                code, rows, _ = self.invoke()
                self.assertEqual((code, rows["agent processes"][0]), (1, "FAIL"))
                self.assertEqual(rows["agent processes"][1], "python; node")
                for check in ("scheduled tasks", "startup items"):
                    self.assertEqual(rows[check], ("LOOK", "check by hand"))
                self.assertEqual(self.invoke("--allow", "python", "node")[0], 0)

    def test_script_source_uses_no_file_write_key_read_or_killing_api(self):
        source = inspect.getsource(preflight)
        tree = ast.parse(source)
        forbidden = {"open", "write_text", "write_bytes", "read_text", "read_bytes", "unlink",
                     "rmdir", "kill", "terminate", "mkdir", "makedirs"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                self.assertNotIn(name, forbidden)
                if name == "remove" and isinstance(node.func, ast.Attribute):
                    self.assertNotEqual(ast.unparse(node.func.value), "os")
        self.assertNotIn("taskkill", source.casefold())
        self.assertNotIn("shutil", source)


if __name__ == "__main__":
    unittest.main()
