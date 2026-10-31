import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from m2k.cli import main
from m2k.contract import load_contract
from m2k.kernel import Kernel, KernelError
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from tests.helpers import review, step


class AuthorReviewerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "m2k.toml"
        self.ledger_path = self.root / "ledger.sqlite"
        self.task = self.root / "task.toml"
        self.task.write_text('[task]\nid = "t26"\ngoal = "independent review"\nworkspace = "ws"\n'
                             '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n',
                             encoding="utf-8")
        workspace = self.root / "ws"
        workspace.mkdir()
        (workspace / "check.py").write_text('print("ok")\n', encoding="utf-8")

    def run_cli(self, worker_model, reviewer_model, worker_name="worker", reviewer_name="reviewer", *,
                allow_same_model=None, worker_steps=None, reviewer_steps=None):
        flag = "" if allow_same_model is None else f"allow_same_model = {json.dumps(allow_same_model)}\n"
        self.config.write_text('ledger = "ledger.sqlite"\n[sandbox]\nbackend = "none"\n'
                               f'[models.worker]\nmodel = {json.dumps(worker_model)}\n'
                               f'[models.reviewer]\nmodel = {json.dumps(reviewer_model)}\n{flag}', encoding="utf-8")
        worker = ScriptedModel([step("finish")] if worker_steps is None else worker_steps, name=worker_name)
        reviewer = ScriptedModel([review("GO")] if reviewer_steps is None else reviewer_steps, name=reviewer_name)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("m2k.cli.build_model", side_effect=[worker, reviewer]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--config", str(self.config), "run", "--untrusted", str(self.task)])
        return code, stdout.getvalue(), stderr.getvalue(), worker, reviewer

    def assert_refused(self, *args, **kwargs):
        code, output, error, worker, reviewer = self.run_cli(*args, **kwargs)
        self.assertEqual(code, 2, output + error)
        self.assertEqual(output, "")
        self.assertEqual(error.splitlines()[-1],
                         f"拒绝启动：Worker 和审查者是同一个模型（{worker.name}），不能自己审自己。"
                         "请在 m2k.toml 给 [models.reviewer] 换一个模型。")
        self.assertNotIn("warning: worker and reviewer use the same model", error)
        from tests.test_startup_refusals import assert_refusal
        assert_refusal(self, self.ledger_path, "t26", "SAME_MODEL")
        self.assertEqual(worker.calls, [])
        self.assertEqual(reviewer.calls, [])

    def test_same_configured_model_with_different_adapter_names_is_refused(self):
        self.assert_refused("shared-model", "shared-model")

    def test_configured_model_comparison_ignores_case(self):
        self.assert_refused("Shared-Model", "sHARED-mODEL")

    def test_configured_model_comparison_ignores_surrounding_spaces(self):
        self.assert_refused("  Shared-Model ", " shared-model  ")

    def test_different_configured_models_with_same_adapter_name_are_refused(self):
        self.assert_refused("worker-model", "reviewer-model", "shared-adapter", "shared-adapter")

    def test_different_models_complete_the_run(self):
        code, output, error, worker, reviewer = self.run_cli("worker-model", "reviewer-model")
        self.assertEqual(code, 0, output + error)
        self.assertIn("t26: PASS", output)
        self.assertNotIn("同一个模型", error)
        self.assertNotIn("warning: worker and reviewer use the same model", error)
        self.assertTrue(worker.calls)
        self.assertTrue(reviewer.calls)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("t26", "verdict").payload["verdict"], "PASS")
            self.assertTrue(ledger.verify_chain()[0])

    def test_kernel_refuses_the_same_adapter_object_without_events_or_calls(self):
        model = ScriptedModel([])
        with Ledger(self.ledger_path) as ledger:
            with self.assertRaises(KernelError) as error:
                Kernel(ledger, model, model)
            self.assertEqual(str(error.exception), "worker and reviewer must be different models")
            self.assertEqual(ledger.events(), [])
        self.assertEqual(model.calls, [])

    def test_kernel_accepts_distinct_objects_even_with_the_same_name(self):
        worker, reviewer = ScriptedModel([]), ScriptedModel([])
        with Ledger(self.ledger_path) as ledger:
            kernel = Kernel(ledger, worker, reviewer)
            self.assertIs(kernel.worker_model, worker)
            self.assertIs(kernel.reviewer_model, reviewer)
            self.assertEqual(ledger.events(), [])
        self.assertEqual(worker.calls, [])
        self.assertEqual(reviewer.calls, [])

    def assert_same_model_pass(self, *args, **kwargs):
        code, output, error, worker, reviewer = self.run_cli(*args, allow_same_model=True, **kwargs)
        self.assertEqual(code, 0, output + error)
        self.assertIn("t26: PASS", output)
        warning = (f"\033[33m注意：Worker 和审查者是同一个模型（{worker.name}），按配置 allow_same_model "
                   "以新会话独立审查（同源审查，已记入账本）。\033[0m")
        self.assertEqual([line for line in error.splitlines() if "同源审查" in line], [warning])
        self.assertTrue(worker.calls)
        self.assertTrue(reviewer.calls)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("t26", "task.submitted").payload["review"], "same-model")
            self.assertEqual(ledger.last("t26", "verdict").payload["verdict"], "PASS")
            self.assertTrue(ledger.verify_chain()[0])
        return worker, reviewer

    def test_false_keeps_the_original_refusal(self):
        self.assert_refused("shared-model", "shared-model", allow_same_model=False)

    def test_same_configured_model_opt_in_passes(self):
        self.assert_same_model_pass("shared-model", "shared-model")

    def test_same_configured_model_opt_in_ignores_case_and_spaces(self):
        self.assert_same_model_pass(" Shared-Model ", "sHARED-mODEL")

    def test_same_adapter_name_opt_in_passes(self):
        self.assert_same_model_pass("worker-model", "reviewer-model", "shared-adapter", "shared-adapter")

    def test_different_models_ignore_opt_in(self):
        code, output, error, _, _ = self.run_cli("worker-model", "reviewer-model", allow_same_model=True)
        self.assertEqual(code, 0, output + error)
        self.assertNotIn("同一个模型", error)
        self.assertNotIn("allow_same_model", error)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("t26", "task.submitted").payload["review"], "cross-model")

    def test_non_boolean_opt_in_is_a_configuration_error_before_model_calls(self):
        for value in ("true", "false", "", 1, 0, [], {}):
            with self.subTest(value=value):
                code, output, error, worker, reviewer = self.run_cli("shared", "shared", allow_same_model=value)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertEqual(error, "配置错误：[models.reviewer] 的 allow_same_model 必须是布尔值（true 或 false）\n")
                self.assertFalse(self.ledger_path.exists())
                self.assertEqual(worker.calls, [])
                self.assertEqual(reviewer.calls, [])

    def test_same_model_reviewer_receives_none_of_the_worker_messages(self):
        private = "T72 worker-only conversation marker"
        worker, reviewer = self.assert_same_model_pass("shared", "shared", worker_steps=[
            step("read_file", thought=private, path="check.py"), step("finish", summary=private)])
        self.assertEqual(len(worker.calls), 2)
        self.assertTrue(any(m["role"] == "assistant" for m in worker.calls[-1]))
        self.assertEqual(len(reviewer.calls), 1)
        received = "\n".join(m["content"] for m in reviewer.calls[0])
        self.assertNotIn(private, received)
        for messages in worker.calls:
            for message in messages:
                self.assertNotIn(message["content"], received)

    def test_subtasks_record_the_same_review_mode_as_the_parent(self):
        with self.task.open("a", encoding="utf-8") as f:
            f.write('\n[[subtasks]]\nid = "part"\ngoal = "check existing files"\nwrites = ["result.txt"]\n'
                    'acceptance = "{python} check.py"\nacceptance_files = ["check.py"]\n'
                    'budget = {max_rounds = 2, max_model_calls = 3, max_commands = 1}\n')
        self.assert_same_model_pass("shared", "shared", reviewer_steps=[review("GO"), review("GO")])
        with Ledger(self.ledger_path) as ledger:
            submitted = ledger.events(kind="task.submitted")
            self.assertEqual({e.task_id for e in submitted}, {"t26", "t26.part"})
            self.assertTrue(all(e.payload["review"] == "same-model" for e in submitted))

    def test_same_model_opt_in_does_not_override_review_failure(self):
        code, output, error, _, _ = self.run_cli("shared", "shared", allow_same_model=True,
                                               reviewer_steps=[review("NO_GO")])
        self.assertEqual(code, 1, output + error)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("t26", "verdict").payload["verdict"], "FAIL")

    def test_same_model_opt_in_does_not_override_unknown_review(self):
        code, output, error, _, _ = self.run_cli("shared", "shared", allow_same_model=True,
                                               reviewer_steps=["unparseable review"])
        self.assertEqual(code, 2, output + error)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.last("t26", "verdict").payload["verdict"], "UNKNOWN")

    def test_kernel_records_mode_after_the_worker_is_assigned(self):
        reviewer = ScriptedModel([review("GO")], "shared")
        with Ledger(self.ledger_path) as ledger:
            kernel = Kernel(ledger, None, reviewer)
            kernel.worker_model = ScriptedModel([step("finish")], "shared")
            result = kernel.run(load_contract(self.task))
            self.assertEqual(result.verdict.value, "PASS", result.reasons)
            self.assertEqual(ledger.last("t26", "task.submitted").payload["review"], "same-model")


if __name__ == "__main__":
    unittest.main()
