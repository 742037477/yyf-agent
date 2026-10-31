import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from m2k.cli import main
from m2k.contract import ContractError, load_contract

TASK = """
[task]
id = "demo-1"
goal = "Write calc.py"
workspace = "ws"
deliver_to = "out"
constraints = ["stdlib only"]

[acceptance]
files = ["check.py"]
command = "{python} check.py"
timeout_s = 30

[budget]
max_rounds = 5
"""


class ContractTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text):
        p = self.dir / "task.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_load_resolves_paths_relative_to_file(self):
        c = load_contract(self.write(TASK))
        self.assertEqual(c.workspace, (self.dir / "ws").resolve())
        self.assertEqual(c.deliver_to, (self.dir / "out").resolve())
        self.assertEqual(c.budget.max_rounds, 5)
        self.assertEqual(c.constraints, ("stdlib only",))
        self.assertEqual(c.acceptance.files, ("check.py",))
        self.assertEqual(len(c.digest()), 64)

    def test_rejects_bad_contracts(self):
        for bad in (TASK.replace('id = "demo-1"', ""),
                    TASK.replace("[acceptance]", "[x]"),
                    TASK.replace("max_rounds", "max_rouns"),
                    TASK.replace('constraints', 'tools = ["shell"]\nconstraints')):
            with self.assertRaises(ContractError):
                load_contract(self.write(bad))


class CliTest(unittest.TestCase):
    def test_demo_runs_offline_and_logs(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["demo", "--dir", d])
            self.assertEqual(code, 0, out.getvalue())
            self.assertIn("PASS", out.getvalue())
            self.assertIn("gate.denied", out.getvalue())
            ledger = str(Path(d) / "ledger.sqlite")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--ledger", ledger, "check-ledger"]), 0)
            self.assertIn("OK", out.getvalue())


if __name__ == "__main__":
    unittest.main()
