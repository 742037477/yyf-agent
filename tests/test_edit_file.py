import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from m2k.contract import Acceptance, TaskContract
from m2k.gates import DEFAULT_GATES, GateContext, check_all
from m2k.kernel import Kernel
from m2k.models import ScriptedModel
from m2k.tools import TOOLS, Workspace, execute
from m2k.types import EffectStatus, Proposal, Verdict
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, review, step


class EditFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = Workspace(self.root)
        self.path = self.root / "calc.py"
        self.contract = TaskContract("edit", "edit calc.py", self.root,
                                     Acceptance("{python} calc.py", files=("calc.py",)), protected=("check.py",))

    def edit(self, old, new):
        self.assertIn("edit_file", TOOLS)
        return execute(self.ws, "edit_file", {"path": "calc.py", "old": old, "new": new})

    def check(self, args, contract=None):
        ctx = GateContext(contract or self.contract, self.ws, {"commands": 0})
        return check_all(DEFAULT_GATES, ctx, Proposal("edit_file", args))

    def test_single_match_preserves_surroundings_and_writes_utf8_lf(self):
        self.path.write_bytes("header\r\n旧一\r\n旧二\r\n旧三\r\ntail\r\n".encode("utf-8"))
        result = self.edit("旧一\n旧二\n旧三\n", "新一\n新二\n新三\n新四\n新五\n")
        self.assertEqual(result.status, EffectStatus.COMPLETED)
        self.assertEqual(self.path.read_bytes(), "header\n新一\n新二\n新三\n新四\n新五\ntail\n".encode("utf-8"))
        self.assertIn("edited calc.py: replaced 1 occurrence (-3 +5 lines)", result.output)

    def test_zero_matches_fails_without_changing_bytes(self):
        original = b"unchanged\r\n"
        self.path.write_bytes(original)
        result = self.edit("missing", "replacement")
        self.assertEqual(result.status, EffectStatus.FAILED)
        self.assertIn("0 occurrences", result.output)
        self.assertEqual(self.path.read_bytes(), original)

    def test_two_matches_fail_without_changing_bytes(self):
        original = b"old\r\nold\r\n"
        self.path.write_bytes(original)
        result = self.edit("old", "new")
        self.assertEqual(result.status, EffectStatus.FAILED)
        self.assertIn("2 occurrences", result.output)
        self.assertEqual(self.path.read_bytes(), original)

    def test_overlapping_matches_are_ambiguous_and_leave_file_unchanged(self):
        self.path.write_bytes(b"aaa")
        result = self.edit("aa", "b")
        self.assertEqual(result.status, EffectStatus.FAILED)
        self.assertIn("2 occurrences", result.output)
        self.assertEqual(self.path.read_bytes(), b"aaa")

    def test_empty_old_fails_without_changing_file(self):
        self.path.write_bytes(b"keep\r\n")
        result = self.edit("", "new")
        self.assertEqual(result.status, EffectStatus.FAILED)
        self.assertIn("non-empty", result.output)
        self.assertEqual(self.path.read_bytes(), b"keep\r\n")

    def test_missing_file_fails_without_creating_it(self):
        result = self.edit("old", "new")
        self.assertEqual(result.status, EffectStatus.FAILED)
        self.assertIn("calc.py", result.output)
        self.assertFalse(self.path.exists())

    def test_empty_new_deletes_the_match(self):
        self.path.write_bytes(b"header\nremove\ntail\n")
        result = self.edit("remove\n", "")
        self.assertEqual(result.status, EffectStatus.COMPLETED)
        self.assertEqual(self.path.read_bytes(), b"header\ntail\n")
        self.assertIn("(-1 +0 lines)", result.output)

    def test_protected_path_is_denied(self):
        protected = self.root / "check.py"
        protected.write_bytes(b"unchanged\n")
        decision = self.check({"path": "sub/../check.py", "old": "unchanged", "new": "changed"})
        self.assertEqual((decision.allow, decision.gate), (False, "protected_path"))
        self.assertEqual(protected.read_bytes(), b"unchanged\n")

    def test_path_outside_workspace_is_denied(self):
        for path in ("../outside.py", str(self.root.parent / "outside.py")):
            with self.subTest(path=path):
                decision = self.check({"path": path, "old": "old", "new": "new"})
                self.assertEqual((decision.allow, decision.gate), (False, "path_scope"))

    def test_required_string_params_are_checked_by_gate(self):
        valid = {"path": "calc.py", "old": "old", "new": "new"}
        for key in valid:
            for args in ({k: v for k, v in valid.items() if k != key}, dict(valid, **{key: 1})):
                with self.subTest(args=args):
                    decision = self.check(args)
                    self.assertEqual((decision.allow, decision.gate), (False, "args"))
        decision = self.check(dict(valid, extra="unexpected"))
        self.assertEqual((decision.allow, decision.gate), (False, "args"))

    def test_contract_can_exclude_edit_file(self):
        contract = replace(self.contract, tools=("read_file",))
        decision = self.check({"path": "calc.py", "old": "old", "new": "new"}, contract)
        self.assertEqual((decision.allow, decision.gate), (False, "tool_allowlist"))


class EditFileKernelTest(KernelCase):
    def test_scripted_worker_edits_existing_file_and_delivers_pass(self):
        contract = self.contract(protected=("check.py",))
        (contract.workspace / "calc.py").write_text(BAD_CODE, encoding="utf-8", newline="\n")
        worker = ScriptedModel([
            step("edit_file", path="calc.py", old="return a - b", new="return a + b"),
            step("finish", summary="fixed add"),
        ])
        reviewer = ScriptedModel([review("GO", "correct addition")])
        result = Kernel(self.ledger, worker, reviewer).run(contract)
        self.assertEqual(result.verdict, Verdict.PASS)
        self.assertIsNotNone(result.delivered_to)
        self.assertEqual((result.delivered_to / "calc.py").read_bytes(), GOOD_CODE.encode("utf-8"))
        intent = self.ledger.last("t1", "effect.intent")
        effect = self.ledger.last("t1", "effect.result")
        self.assertEqual(intent.payload["tool"], "edit_file")
        self.assertEqual(effect.payload["status"], "COMPLETED")
        self.assertEqual(intent.op_id, effect.op_id)
        self.assertLess(intent.seq, effect.seq)
        self.assertTrue(self.ledger.verify_chain()[0])


if __name__ == "__main__":
    unittest.main()
