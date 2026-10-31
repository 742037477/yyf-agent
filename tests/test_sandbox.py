import io
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from m2k import sandbox
from m2k.cli import main
from m2k.contract import Acceptance, ContractError, TaskContract, parse_contract
from m2k.kernel import Kernel, status
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.tools import run_shell
from m2k.types import EffectStatus
from m2k.verify import run_test
from m2k.tools import Workspace
from tests.helpers import review, step

BWRAP_OK, BWRAP_WHY = sandbox.Bwrap().available()


def _host_can_connect() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), 3).close()
        return True
    except OSError:
        return False


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()


class ArgvAndChoiceTest(Temp):
    @unittest.skipUnless(sys.platform == "linux", "bubblewrap argv is Linux-only")
    def test_bwrap_argv_binds_only_runtime_and_workspace(self):
        a = sandbox.Bwrap("bwrap").argv("echo hi", self.ws, False, info_fd=7)
        self.assertEqual(a[:4], ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"])
        self.assertNotIn("--share-net", a)
        self.assertEqual(a[-3:], ["/bin/sh", "-c", "echo hi"])
        joined = " ".join(a)
        self.assertIn("--ro-bind /usr /usr", joined)
        self.assertIn("--ro-bind /etc /etc", joined)
        self.assertIn(f"--bind {self.ws} {self.ws}", joined)
        self.assertIn(f"--chdir {self.ws}", joined)
        self.assertIn("--info-fd 7", joined)
        self.assertIn("--tmpfs /tmp", joined)
        for hidden in ("/root", "/home", "/srv", str(self.outside)):
            self.assertNotIn(f" {hidden} ", f" {joined} ")
        rw = [a[i + 1] for i, x in enumerate(a) if x == "--bind"]
        self.assertEqual(rw, [str(self.ws)])          # the workspace is the only writable bind

    def test_network_only_when_granted(self):
        self.assertIn("--share-net", sandbox.Bwrap("bwrap").argv("true", self.ws, True))

    def test_choose(self):
        self.assertIsInstance(sandbox.choose("none"), sandbox.NoSandbox)
        self.assertIsInstance(sandbox.choose("docker"), sandbox.Unavailable)   # a typo fails closed
        with patch.object(sandbox.Bwrap, "available", return_value=(False, "no bwrap here")):
            box = sandbox.choose("auto")
            self.assertIsInstance(box, sandbox.Unavailable)
            self.assertEqual(box.reason, "no bwrap here")
            self.assertIsInstance(sandbox.choose("bwrap"), sandbox.Unavailable)

    def test_contract_network_defaults_off_and_must_be_bool(self):
        base = {"task": {"id": "t", "goal": "g", "workspace": "ws"},
                "acceptance": {"command": "{python} c.py", "files": ["c.py"]}}
        self.assertFalse(parse_contract(base, self.root).network)
        base["task"]["network"] = True
        self.assertTrue(parse_contract(base, self.root).network)
        base["task"]["network"] = "yes"
        with self.assertRaises(ContractError):
            parse_contract(base, self.root)


class RefusalAndRecordTest(Temp):
    def _task(self, sandbox_line: str) -> tuple[Path, Path]:
        config = self.root / "m2k.toml"
        config.write_text(f'ledger = "ledger.sqlite"\n{sandbox_line}[models.worker]\n[models.reviewer]\n',
                          encoding="utf-8")
        (self.ws / "check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        task = self.root / "task.toml"
        task.write_text('[task]\nid = "box"\ngoal = "g"\nworkspace = "ws"\n'
                        '[acceptance]\nfiles = ["check.py"]\ncommand = "{python} check.py"\n', encoding="utf-8")
        return config, task

    def test_auto_without_backend_refuses_before_submitting(self):
        config, task = self._task("")
        models = [ScriptedModel([], "worker"), ScriptedModel([], "reviewer")]
        err = io.StringIO()
        with patch.object(sandbox.Bwrap, "available", return_value=(False, "no bwrap here")), \
                patch("m2k.cli.build_model", side_effect=models), redirect_stderr(err):
            code = main(["--config", str(config), "run", "--untrusted", str(task)])
        self.assertEqual(code, 2)
        self.assertIn("拒绝启动", err.getvalue())
        self.assertIn('backend = "none"', err.getvalue())
        self.assertIn("GitHub", err.getvalue())
        with Ledger(self.root / "ledger.sqlite") as ledger:
            from tests.test_startup_refusals import assert_refusal
            assert_refusal(self, self.root / "ledger.sqlite", "box", "SANDBOX_UNAVAILABLE")
        self.assertTrue(all(not m.calls for m in models))

    def test_none_runs_warns_and_is_recorded(self):
        config, task = self._task('[sandbox]\nbackend = "none"\n')
        models = [ScriptedModel([step("finish")], "worker"), ScriptedModel([review("GO")], "reviewer")]
        err = io.StringIO()
        with patch("m2k.cli.build_model", side_effect=models), redirect_stderr(err), redirect_stdout_null():
            self.assertEqual(main(["--config", str(config), "run", "--untrusted", str(task)]), 0)
        self.assertIn("不隔离", err.getvalue())
        with Ledger(self.root / "ledger.sqlite") as ledger:
            submitted = ledger.events("box", "task.submitted")[0].payload
            self.assertEqual((submitted["sandbox"], submitted["network"]), ("none", False))
            self.assertEqual((status(ledger, "box")["sandbox"], status(ledger, "box")["network"]), ("none", False))


class FailClosedTest(Temp):
    def test_unavailable_backend_refuses_every_command(self):
        marker = self.ws / "ran.txt"
        eff = run_shell('{python} -c "open(\'ran.txt\', \'w\').write(\'x\')"', self.ws, 10,
                        sandbox=sandbox.Unavailable("none here"))
        self.assertEqual((eff.status, eff.detail.get("reason")), (EffectStatus.FAILED, "START_FAILED"))
        self.assertFalse(marker.exists())

    def test_acceptance_is_unknown_when_the_backend_cannot_start(self):
        (self.ws / "check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        c = TaskContract("t", "g", self.ws, Acceptance("{python} check.py", 10, files=("check.py",)))
        result = run_test(c, Workspace(self.ws, sandbox.Unavailable("none here")))
        self.assertEqual((result.verdict.value, result.reason), ("UNKNOWN", "START_FAILED"))

    @unittest.skipUnless(os.name == "posix" and Path("/bin/false").exists(), "needs /bin/false")
    def test_backend_that_never_comes_up_is_start_failed_not_run(self):
        marker = self.ws / "ran.txt"
        eff = run_shell('{python} -c "open(\'ran.txt\', \'w\').write(\'x\')"', self.ws, 10,
                        sandbox=sandbox.Bwrap("/bin/false"))
        self.assertEqual((eff.status, eff.detail.get("reason")), (EffectStatus.FAILED, "START_FAILED"))
        self.assertFalse(marker.exists())

    def test_kernel_run_with_unavailable_backend_never_passes(self):
        (self.ws / "check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        c = TaskContract("t", "g", self.ws, Acceptance("{python} check.py", 10, files=("check.py",)))
        worker = ScriptedModel([step("run_command", command="{python} -c \"print(1)\""), step("finish")])
        with Ledger(self.root / "ledger.sqlite") as ledger:
            report = Kernel(ledger, worker, ScriptedModel([review("GO")]), sandbox=sandbox.Unavailable("x")).run(c)
            results = [e.payload for e in ledger.events("t", "effect.result")]
        self.assertNotEqual(report.verdict.value, "PASS")
        self.assertEqual(report.test.reason, "START_FAILED")
        self.assertTrue(any(r.get("detail", {}).get("reason") == "START_FAILED" or "START_FAILED" in str(r)
                            for r in results))


@unittest.skipUnless(BWRAP_OK, f"bubblewrap not usable here: {BWRAP_WHY}")
class BwrapIsolationTest(Temp):
    def sh(self, command: str, network: bool = False):
        return run_shell(command, self.ws, 20, sandbox=sandbox.Bwrap(), network=network)

    def test_file_outside_the_workspace_is_unreadable(self):
        canary = self.outside / "secret.txt"
        canary.write_text("CANARY-T08", encoding="utf-8")
        eff = self.sh(f"cat {canary}")
        self.assertEqual(eff.status, EffectStatus.FAILED)
        self.assertNotIn("CANARY-T08", eff.output)

    def test_write_outside_fails_and_workspace_is_writable(self):
        eff = self.sh(f"echo x > {self.outside / 'w.txt'}; echo y > inside.txt")
        self.assertFalse((self.outside / "w.txt").exists())
        self.assertEqual((self.ws / "inside.txt").read_text().strip(), "y")
        self.assertIsNot(eff.detail.get("reason"), "START_FAILED")

    def test_root_home_and_ledger_folder_are_invisible(self):
        ledger_dir = self.root / "state"
        with Ledger(ledger_dir / "ledger.sqlite") as ledger:
            ledger.append("t", "x", {})
        eff = self.sh(f"ls /root; ls {ledger_dir}; ls /home")
        self.assertNotIn("ledger.sqlite", eff.output)
        self.assertEqual(eff.status, EffectStatus.FAILED)

    def test_other_processes_are_invisible(self):
        eff = self.sh(f"test -e /proc/{os.getpid()} && echo VISIBLE || echo hidden")
        self.assertIn("hidden", eff.output)

    def test_python_works_and_network_is_cut(self):
        eff = self.sh('{python} -c "print(6 * 7)"')
        self.assertEqual((eff.status, eff.output.strip()), (EffectStatus.COMPLETED, "42"))
        net = self.sh("{python} -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3); print('NET' + '-OK')\"")
        self.assertNotIn("NET-OK", net.output)
        self.assertEqual(net.status, EffectStatus.FAILED)

    @unittest.skipUnless(_host_can_connect(), "the host itself has no outbound network")
    def test_control_the_same_connection_works_unisolated_and_when_granted(self):
        cmd = "{python} -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3); print('NET' + '-OK')\""
        self.assertIn("NET-OK", run_shell(cmd, self.ws, 20).output)
        self.assertIn("NET-OK", self.sh(cmd, network=True).output)


class redirect_stdout_null:
    def __enter__(self):
        self._old, sys.stdout = sys.stdout, io.StringIO()

    def __exit__(self, *exc):
        sys.stdout = self._old


if __name__ == "__main__":
    unittest.main()
