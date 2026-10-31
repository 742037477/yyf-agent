import io
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from m2k.cli import _ledger, main
from m2k.contract import Acceptance, TaskContract
from m2k.kernel import Kernel, KernelError
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.types import Verdict
from tests.helpers import GOOD_CODE, review, step


WARNING = "旧账本仍在 .m2k/ledger.sqlite；查看旧任务请加 --ledger .m2k/ledger.sqlite\n"


class LedgerLocationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.enterContext(patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.root / "local"), "XDG_STATE_HOME": str(self.root / "state"),
            "HOME": str(self.home), "USERPROFILE": str(self.home),
        }))
        self.enterContext(chdir(self.root))

    def default_path(self):
        return self.root / ("local" if sys.platform == "win32" else "state") / "m2k" / "ledger.sqlite"

    def args(self, config=None, ledger=None):
        return Namespace(config=config, ledger=ledger, cmd="check-ledger")

    def test_default_is_in_per_user_folder(self):
        with _ledger(self.args()) as ledger:
            self.assertEqual(ledger.path.resolve(), self.default_path())
            self.assertTrue(ledger.path.is_file())
        self.assertFalse((self.root / ".m2k" / "ledger.sqlite").exists())

    def test_empty_state_environment_falls_back_to_home(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": "", "XDG_STATE_HOME": ""}):
            folder = self.home / ("AppData/Local" if sys.platform == "win32" else ".local/state")
            with _ledger(self.args()) as ledger:
                self.assertEqual(ledger.path.resolve(), folder / "m2k" / "ledger.sqlite")

    def test_config_folder_does_not_change_default(self):
        config = self.root / "config" / "m2k.toml"
        config.parent.mkdir()
        config.write_text("", encoding="utf-8")
        with _ledger(self.args(str(config))) as ledger:
            self.assertEqual(ledger.path.resolve(), self.default_path())

    def test_explicit_ledger_wins_over_config_and_keeps_relative_base(self):
        config = self.root / "config" / "m2k.toml"
        config.parent.mkdir()
        config.write_text('ledger = "configured.sqlite"\n', encoding="utf-8")
        for path in ("chosen.sqlite", str(self.root / "absolute.sqlite")):
            with self.subTest(path=path), _ledger(self.args(str(config), path)) as ledger:
                expected = Path(path) if Path(path).is_absolute() else config.parent / path
                self.assertEqual(ledger.path.resolve(), expected)
        self.assertFalse((config.parent / "configured.sqlite").exists())
        self.assertFalse(self.default_path().exists())

    def test_configured_ledger_wins_over_default(self):
        config = self.root / "config" / "m2k.toml"
        config.parent.mkdir()
        config.write_text('ledger = "configured.sqlite"\n', encoding="utf-8")
        with _ledger(self.args(str(config))) as ledger:
            self.assertEqual(ledger.path.resolve(), config.parent / "configured.sqlite")
        self.assertFalse(self.default_path().exists())

    def test_relative_ledger_without_config_uses_current_folder(self):
        with _ledger(self.args(ledger="chosen.sqlite")) as ledger:
            self.assertEqual(ledger.path.resolve(), self.root / "chosen.sqlite")

    def test_read_commands_warn_once_and_preserve_old_ledger(self):
        old = self.root / ".m2k" / "ledger.sqlite"
        with Ledger(old) as ledger:
            ledger.append("old", "task.submitted", {"goal": "old task"})
        before = old.read_bytes()
        with Ledger(self.default_path()) as ledger:
            ledger.append("current", "task.submitted", {"goal": "current task"})
        commands = [["status", "current"], ["log", "current"],
                    ["report", "current", "--out", str(self.root / "report.html")], ["check-ledger"]]
        for command in commands:
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(command=command), redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main(command), 0)
            self.assertEqual(err.getvalue(), WARNING)
            self.assertEqual(old.read_bytes(), before)

    def test_explicit_paths_suppress_old_ledger_warning(self):
        with Ledger(self.root / ".m2k" / "ledger.sqlite"):
            pass
        config = self.root / "m2k.toml"
        config.write_text('ledger = "configured.sqlite"\n', encoding="utf-8")
        for arguments in (["--ledger", ".m2k/ledger.sqlite"], ["--config", str(config)]):
            err = io.StringIO()
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(main(arguments + ["check-ledger"]), 0)
            self.assertEqual(err.getvalue(), "")

    def test_no_old_ledger_means_no_warning(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(main(["check-ledger"]), 0)
        self.assertEqual(err.getvalue(), "")

    def test_inside_or_equal_resolved_paths_refused_before_any_event(self):
        (self.root / "check.py").write_text("pass\n", encoding="utf-8")
        path = self.root / "records" / "ledger.sqlite"
        with Ledger(path) as ledger:
            ledger.append("existing", "task.submitted", {"goal": "preserve"})
            before, contents = ledger.events(), path.read_bytes()
            workspaces = [self.root, self.root / "records", self.root / "alias" / "..", path]
            for i, workspace in enumerate(workspaces):
                worker, reviewer = ScriptedModel([step("finish")]), ScriptedModel([review("GO")])
                contract = TaskContract(f"refused-{i}", "g", workspace,
                                        Acceptance("{python} check.py", files=("check.py",)))
                with self.subTest(workspace=workspace):
                    with self.assertRaisesRegex(KernelError, "ledger is inside the workspace") as error:
                        Kernel(ledger, worker, reviewer).run(contract)
                    self.assertIn(str(path.resolve()), str(error.exception))
                    self.assertIn(str(workspace.resolve()), str(error.exception))
                    self.assertEqual(ledger.events(), before)
                    self.assertEqual(ledger.events(contract.task_id), [])
                    self.assertEqual(path.read_bytes(), contents)
                    self.assertEqual(worker.calls, [])
                    self.assertEqual(reviewer.calls, [])

    def test_inside_workspace_refused_before_seed_copy(self):
        workspace = self.root / "ws"
        seed = self.root / "seed"
        seed.mkdir()
        (seed / "seed.txt").write_text("seed", encoding="utf-8")
        with Ledger(workspace / "ledger.sqlite") as ledger:
            contract = TaskContract("seed", "g", workspace, Acceptance("unused", files=("seed.txt",)), seed=seed)
            with self.assertRaisesRegex(KernelError, "ledger is inside the workspace"):
                Kernel(ledger, ScriptedModel([]), ScriptedModel([])).run(contract)
            self.assertEqual(ledger.events(), [])
        self.assertFalse((workspace / "seed.txt").exists())

    def test_ledger_next_to_workspace_runs_and_records_resolved_path(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        (workspace / "check.py").write_text(
            "from calc import add\nassert add(2, 3) == 5\n", encoding="utf-8")
        worker = ScriptedModel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")])
        contract = TaskContract("safe", "Write add", workspace, Acceptance("{python} check.py", files=("check.py",)),
                                deliver_to=self.root / "out", protected=("check.py",))
        with Ledger(Path("ws-ledger.sqlite")) as ledger:
            report = Kernel(ledger, worker, ScriptedModel([review("GO")])).run(contract)
            self.assertEqual(report.verdict, Verdict.PASS)
            self.assertEqual((report.delivered_to / "calc.py").read_text(encoding="utf-8"), GOOD_CODE)
            self.assertEqual(ledger.last("safe", "task.submitted").payload["ledger"], str(ledger.path.resolve()))
            self.assertTrue(ledger.verify_chain()[0])

    def test_cli_run_exits_nonzero_with_refusal_and_no_events(self):
        config = self.root / "m2k.toml"
        config.write_text('[sandbox]\nbackend = "none"\n[models.worker]\n[models.reviewer]\n', encoding="utf-8")
        task = self.root / "task.toml"
        task.write_text('[task]\nid = "cli-refused"\ngoal = "g"\nworkspace = "ws"\n'
                        '[acceptance]\nfiles = ["check.py"]\ncommand = "unused"\n', encoding="utf-8")
        ledger_path = self.root / "ws" / "ledger.sqlite"
        models = [ScriptedModel([], "worker"), ScriptedModel([], "reviewer")]
        with patch("m2k.cli.build_model", side_effect=models):
            with self.assertRaises(SystemExit) as error:
                main(["--ledger", str(ledger_path), "run", "--untrusted", str(task)])
        self.assertNotEqual(error.exception.code, 0)
        self.assertIn("ledger is inside the workspace", str(error.exception))
        self.assertIn(str(ledger_path), str(error.exception))
        self.assertIn(str(ledger_path.parent), str(error.exception))
        with Ledger(ledger_path) as ledger:
            self.assertEqual(ledger.events(), [])
        self.assertTrue(all(not model.calls for model in models))


if __name__ == "__main__":
    unittest.main()
