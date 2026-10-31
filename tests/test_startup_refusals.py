"""Startup facts only: preserve checks, messages, and the legacy SystemExit cases."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from m2k import sandbox, trust
from m2k.cli import main
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from tests.helpers import review, step


def assert_refusal(case, path, task, code):
    """Replace only old no-ledger assertions, retaining the prefix across loop iterations."""
    seen = getattr(case, "_startup_refusal_prefixes", {})
    before = seen.get(str(path), [])
    with Ledger(path) as ledger:
        events = ledger.events()
        case.assertTrue(ledger.verify_chain()[0])
    case.assertEqual(events[:-1], before)
    case.assertEqual(len(events), len(before) + 1)
    event = events[-1]
    case.assertEqual((event.task_id, event.kind, event.payload.get("task"), event.payload.get("code")),
                     ("m2k.startup", "startup.refused", task, code))
    case.assertIsInstance(event.payload.get("reason"), str)
    seen[str(path)] = events
    case._startup_refusal_prefixes = seen


class StartupRefusalsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config, self.task, self.path = (self.root / n for n in ("m2k.toml", "task.toml", "ledger.sqlite"))
        self.text = 'ledger = "ledger.sqlite"\n[sandbox]\nbackend = "none"\n[models.worker]\n[models.reviewer]\n'
        self.config.write_text(self.text, encoding="utf-8")
        self.task.write_text('[task]\nid = "startup"\ngoal = "g"\nworkspace = "ws"\n'
                             '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n', encoding="utf-8")

    def invoke(self, *flags):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(["--config", str(self.config), "run", *flags, str(self.task)])
        return code, output.getvalue(), error.getvalue()

    def test_no_trust_preserves_exact_message_and_existing_events(self):
        with Ledger(self.path) as ledger:
            previous = ledger.append("earlier", "fact", {"keep": True})
        self._startup_refusal_prefixes = {str(self.path): [previous]}
        with patch("m2k.cli.sandboxes.choose") as choose, patch("m2k.cli.build_model") as model:
            code, output, error = self.invoke()
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(error, '拒绝启动：没有信任链（[trust]）。两个选择：1) 运行 m2k trust init-dev --out keys 生成测试链，'
                         '并把它打印的 [trust] 段加进 m2k.toml；2) 只这一次不授权运行：m2k run --untrusted <task>（会记入账本）。\n')
        choose.assert_not_called()
        model.assert_not_called()
        assert_refusal(self, self.path, "startup", "NO_TRUST")

    def test_trust_and_untrusted_conflict(self):
        self.config.write_text(self.text + "[trust]\n", encoding="utf-8")
        code, output, error = self.invoke("--untrusted")
        self.assertEqual((code, output, error), (2, "", "已配置 [trust]，不能同时用 --untrusted\n"))
        assert_refusal(self, self.path, "startup", "CONFIG")

    def test_obs_missing_keys_and_wrong_types(self):
        sections = [{}, {"key": "x", "log": "x"}, {"public_key_b64": 2}, [], "wrong", True]
        for section in sections:
            text = self.text + "[obs]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in section.items()) if isinstance(section, dict) else f"obs = {json.dumps(section)}\n" + self.text
            self.config.write_text(text, encoding="utf-8")
            with self.subTest(section=section), patch("m2k.cli.obs.ObsClient") as client:
                code, _, error = self.invoke("--untrusted")
                self.assertEqual(code, 2)
                self.assertTrue(error.startswith("配置错误：[obs] "), error)
                client.assert_not_called()
                assert_refusal(self, self.path, "startup", "OBS_CONFIG")

    def test_obs_workspace_paths_and_mismatched_key_keep_systemexit(self):
        for fields, reason in (({"key": "ws/key.pem", "log": "obs.jsonl"}, "OBS key or log is inside the workspace"),
                               ({"key": "key.pem", "log": "ws/obs.jsonl"}, "OBS key or log is inside the workspace"),
                               ({"key": "key.pem", "log": "obs.jsonl"}, "OBS key does not match public_key_b64")):
            section = fields | {"public_key_b64": "different"}
            self.config.write_text(self.text + "[obs]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in section.items()), encoding="utf-8")
            with self.subTest(fields=fields), patch("m2k.cli.trust.load_private_key", return_value=object()), \
                    patch("m2k.cli.trust.public_key_b64", return_value="actual"), patch("m2k.cli.obs.ObsClient") as client:
                with self.assertRaises(SystemExit) as stopped:
                    self.invoke("--untrusted")
                self.assertEqual(stopped.exception.code, reason)
                client.assert_not_called()
                assert_refusal(self, self.path, "startup", "OBS_CONFIG")

    def test_anchor_workspace_and_subtask_paths_keep_systemexit(self):
        for section in ('anchor_file = "ws/a.jsonl"\n', '[anchor]\nfile = "a.jsonl"\nrepo_dir = "ws/repo"\n',
                        '[anchor]\nfile = "ws.subtasks/a.jsonl"\n'):
            text = section + self.text if section.startswith("anchor_file") else self.text + section
            self.config.write_text(text, encoding="utf-8")
            with self.subTest(section=section), patch("m2k.cli.build_model") as model:
                with self.assertRaisesRegex(SystemExit, "^anchor file or repo-dir is inside the workspace$"):
                    self.invoke("--untrusted")
                model.assert_not_called()
                assert_refusal(self, self.path, "startup", "ANCHOR_CONFIG")

    def test_unavailable_sandbox(self):
        with patch("m2k.cli.sandboxes.choose", return_value=sandbox.Unavailable("no backend")), patch("m2k.cli.build_model") as model:
            code, _, error = self.invoke("--untrusted")
        self.assertEqual(code, 2)
        self.assertIn("拒绝启动：这台电脑没有可用的命令隔离环境（no backend）。\n", error)
        model.assert_not_called()
        assert_refusal(self, self.path, "startup", "SANDBOX_UNAVAILABLE")

    def test_same_model_and_worker_probe(self):
        for same in (True, False):
            worker = ScriptedModel([], "worker")
            reviewer = ScriptedModel([], "worker" if same else "reviewer")
            worker.external, worker.probe = True, lambda: "FAILED (exit 3)"
            with self.subTest(same=same), patch("m2k.cli.build_model", side_effect=[worker, reviewer]):
                code, _, error = self.invoke("--untrusted")
                self.assertEqual(code, 2)
                self.assertIn("Worker 和审查者是同一个模型（worker）" if same else "Worker 命令试启动失败（worker）：FAILED (exit 3)", error)
                self.assertEqual(worker.calls + reviewer.calls, [])
                assert_refusal(self, self.path, "startup", "SAME_MODEL" if same else "WORKER_PROBE")

    def test_unopenable_ledger_adds_one_line_without_traceback(self):
        self.path.mkdir()
        code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(len(error.splitlines()), 2)
        self.assertTrue(error.splitlines()[-1].startswith("（账本打不开，本次拒绝未记账："))
        self.assertNotIn("Traceback", error)

    def test_existing_release_refusal_also_handles_unopenable_ledger(self):
        self.config.write_text(self.text + "[trust]\n", encoding="utf-8")
        with patch("m2k.cli.trust.check_release", side_effect=trust.TrustError("RELEASE_MISSING", "absent")), \
                patch("m2k.cli.Ledger", side_effect=sqlite3.OperationalError("locked")):
            code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("拒绝启动：发行清单核对失败（RELEASE_MISSING）：RELEASE_MISSING: absent", error)
        self.assertEqual(error.splitlines()[-1], "（账本打不开，本次拒绝未记账：locked）")

    def test_precontract_errors_stay_unrecorded(self):
        cases = ('sandbox = true\n[models.worker]\n[models.reviewer]\n',
                 '[models.worker]\nmodel = 1\n[models.reviewer]\n',
                 '[models.worker]\n[models.reviewer]\nallow_same_model = "true"\n')
        for text in cases:
            with self.subTest(text=text):
                self.config.write_text(text, encoding="utf-8")
                self.assertEqual(self.invoke()[0], 2)
                self.assertFalse(self.path.exists())

    def test_invalid_contract_stays_unrecorded(self):
        self.task.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "invalid task contract"):
            self.invoke()
        self.assertFalse(self.path.exists())

    def test_watch_refuses_invalid_table_list_strings_and_missing_paths(self):
        cases = ("watch = true\n", "watch = []\n", "watch = 'wrong'\n")
        cases += tuple("[watch]\npaths = " + value + "\n" for value in
                       ('true', '1', '"folder"', '{}', '[1]', '[""]', '[" "]', '["missing"]'))
        for section in cases:
            text = section + self.text if section.startswith("watch =") else self.text + section
            self.config.write_text(text, encoding="utf-8")
            with self.subTest(section=section), patch("m2k.cli.build_model") as model:
                code, _, error = self.invoke("--untrusted")
                self.assertEqual(code, 2)
                self.assertTrue(error.startswith("配置错误：[watch] "), error)
                model.assert_not_called()
                assert_refusal(self, self.path, "startup", "CONFIG")

    def test_watch_refuses_runtime_file_and_folder_overlaps_before_starting_obs(self):
        folders = [self.root / name for name in ("obs", "logs", "anchors", "repo")]
        for folder in folders:
            folder.mkdir()
            (folder / "child").write_text("data", encoding="utf-8")
        key = folders[0] / "key.pem"
        key.write_text("key", encoding="utf-8")
        anchor = folders[2] / "anchors.jsonl"
        anchor.write_text("", encoding="utf-8")
        with Ledger(self.path): pass
        extra = ('[obs]\nkey = "obs/key.pem"\nlog = "logs/obs.jsonl"\npublic_key_b64 = "unused"\n'
                 '[anchor]\nfile = "anchors/anchors.jsonl"\nrepo_dir = "repo"\n')
        paths = [self.path, key, key.parent, anchor, anchor.parent, self.root,
                 folders[1], folders[1] / "child", folders[3], folders[3] / "child"]
        for path in paths:
            self.config.write_text(self.text + '[watch]\npaths = ' + json.dumps([str(path)]) + '\n' + extra, encoding="utf-8")
            with self.subTest(path=path), patch("m2k.cli.obs.ObsClient") as client, patch("m2k.cli.build_model") as model:
                code, _, error = self.invoke("--untrusted")
                self.assertEqual(code, 2)
                self.assertIn("配置错误：[watch]", error)
                self.assertIn("重叠", error)
                client.assert_not_called()
                model.assert_not_called()
                assert_refusal(self, self.path, "startup", "CONFIG")

    def test_watch_refuses_total_file_limit(self):
        folder = self.root / "watched"
        folder.mkdir()
        for i in range(3):
            (folder / str(i)).write_text("data", encoding="utf-8")
        self.config.write_text(self.text + '[watch]\npaths = ["watched"]\n', encoding="utf-8")
        with patch("m2k.external.WATCH_FILE_LIMIT", 2), patch("m2k.cli.build_model") as model:
            code, _, error = self.invoke("--untrusted")
        self.assertEqual(code, 2)
        self.assertIn("20000", error)
        model.assert_not_called()
        assert_refusal(self, self.path, "startup", "CONFIG")

    def test_watch_resolves_relative_absolute_and_home_paths_and_checks_native_commands(self):
        folder = self.root / "watched"
        folder.mkdir()
        file = folder / "settings.toml"
        workspace = self.root / "ws"
        workspace.mkdir()
        (workspace / "check.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace / "mutate.py").write_text(
            f"from pathlib import Path\nPath({str(file)!r}).write_text('after', encoding='utf-8')\n", encoding="utf-8")
        for i, value in enumerate(("watched/../watched", str(folder), "~/watched")):
            file.write_text("before", encoding="utf-8")
            self.task.write_text(self.task.read_text(encoding="utf-8").replace(f'id = "startup{i - 1}"', f'id = "startup{i}"')
                                 .replace('id = "startup"', f'id = "startup{i}"'), encoding="utf-8")
            self.config.write_text(self.text + '[watch]\npaths = ' + json.dumps([value]) + '\n', encoding="utf-8")
            models = [ScriptedModel([step("run_command", command="{python} mutate.py"), step("finish")], "w"),
                      ScriptedModel([review("GO")], "r")]
            with self.subTest(value=value), patch.dict(os.environ, {"USERPROFILE": str(self.root), "HOME": str(self.root)}), \
                    patch("m2k.cli.build_model", side_effect=models):
                self.assertEqual(self.invoke("--untrusted")[0], 1)
            with Ledger(self.path) as ledger:
                self.assertEqual(ledger.last(f"startup{i}", "task.submitted").payload["watch"], [str(folder)])
                self.assertEqual(ledger.last(f"startup{i}", "test.result").payload["reason"], "OUTSIDE_CHANGED")

    def test_absent_and_empty_watch_preserve_the_task_metadata(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        (workspace / "check.py").write_text("print('ok')\n", encoding="utf-8")
        for i, section in enumerate(("", "[watch]\n", "[watch]\npaths = []\n")):
            self.task.write_text(self.task.read_text(encoding="utf-8").replace(f'id = "startup{i - 1}"', f'id = "startup{i}"')
                                 .replace('id = "startup"', f'id = "startup{i}"'), encoding="utf-8")
            self.config.write_text(self.text + section, encoding="utf-8")
            with patch("m2k.cli.build_model", side_effect=[ScriptedModel([step("finish")], "w"),
                                                         ScriptedModel([review("GO")], "r")]):
                self.assertEqual(self.invoke("--untrusted")[0], 0)
            with Ledger(self.path) as ledger:
                self.assertNotIn("watch", ledger.last(f"startup{i}", "task.submitted").payload)
                self.assertEqual(ledger.events(f"startup{i}", "outside.changed"), [])
