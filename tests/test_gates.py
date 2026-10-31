import tempfile
import unittest
from pathlib import Path

from m2k.contract import Acceptance, Budget, TaskContract
from m2k.gates import DEFAULT_GATES, GateContext, check_all, model_budget_denial
from m2k.tools import Workspace
from m2k.types import Proposal

ZERO = {"rounds": 0, "model_calls": 0, "tokens": 0, "tokens_unknown_calls": 0, "commands": 0}


class GateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws_path = Path(self._tmp.name) / "ws"
        self.ws_path.mkdir()
        self.c = TaskContract("t", "g", self.ws_path, Acceptance("true"),
                              Budget(max_commands=2, max_rounds=3, max_model_calls=4, max_tokens=100),
                              tools=("read_file", "write_file", "run_command"))

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, tool, usage=None, **args):
        return check_all(DEFAULT_GATES, GateContext(self.c, Workspace(self.ws_path), usage or ZERO),
                         Proposal(tool, args))

    def test_allowed_action_passes_every_gate(self):
        self.assertTrue(self.check("write_file", path="a/b.py", content="x").allow)

    def test_tool_not_in_contract_is_denied(self):
        d = self.check("list_files", path=".")
        self.assertEqual((d.allow, d.gate), (False, "tool_allowlist"))

    def test_unknown_tool_is_denied(self):
        self.assertEqual(self.check("rm_rf").gate, "tool_allowlist")

    def test_missing_extra_and_mistyped_args_are_denied(self):
        self.assertEqual(self.check("write_file", path="a").gate, "args")
        self.assertEqual(self.check("read_file", path="a", mode="r").gate, "args")
        self.assertEqual(self.check("read_file", path=3).gate, "args")
        self.assertEqual(self.check("run_command", command="x", timeout_s="5").gate, "args")
        self.assertTrue(self.check("run_command", command="x", timeout_s=5).allow)

    def test_paths_outside_workspace_are_denied(self):
        for bad in ("../x", "/etc/passwd", "a/../../x"):
            d = self.check("read_file", path=bad)
            self.assertEqual((d.allow, d.gate), (False, "path_scope"), bad)
        self.assertTrue(self.check("read_file", path="a/../x").allow)

    def test_command_budget(self):
        self.assertFalse(self.check("run_command", usage=dict(ZERO, commands=2), command="x").allow)
        self.assertTrue(self.check("write_file", usage=dict(ZERO, commands=2), path="a", content="").allow)

    def test_model_budget(self):
        self.assertIsNone(model_budget_denial(self.c, ZERO, "worker"))
        self.assertIn("round", model_budget_denial(self.c, dict(ZERO, rounds=3), "worker"))
        self.assertIsNone(model_budget_denial(self.c, dict(ZERO, rounds=3), "reviewer"))
        self.assertIn("model call", model_budget_denial(self.c, dict(ZERO, model_calls=4), "reviewer"))
        self.assertIn("token", model_budget_denial(self.c, dict(ZERO, tokens=100), "worker"))


if __name__ == "__main__":
    unittest.main()
