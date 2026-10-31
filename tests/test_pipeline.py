"""End-to-end chain with scripted models: PASS / FAIL / UNKNOWN / budget / stall / gate deny."""
import unittest

from m2k.contract import Budget
from m2k.kernel import Kernel, KernelError, status
from m2k.types import Verdict, WorkerStatus
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, review, step

WRITE_GOOD = step("write_file", path="calc.py", content=GOOD_CODE)
WRITE_BAD = step("write_file", path="calc.py", content=BAD_CODE)
FINISH = step("finish", summary="done")


class PipelineTest(KernelCase):
    def test_pass_is_delivered_and_post_verified(self):
        c, r = self.run_task([WRITE_GOOD, FINISH], [review("GO", "fine")])
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assertEqual((r.delivered_to / "calc.py").read_text(), GOOD_CODE)
        self.assertEqual(self.kinds()[-4:], ["verdict", "gate.adjudication", "delivery.intent", "delivery.result"])
        self.assertTrue(self.ledger.last("t1", "delivery.result").payload["post_verified"])
        self.assertEqual(status(self.ledger, "t1")["verdict"], "PASS")
        self.assertTrue(self.ledger.verify_chain()[0])

    def test_failing_acceptance_is_fail_and_not_delivered(self):
        c, r = self.run_task([WRITE_BAD, FINISH], [review("GO")])
        self.assertEqual(r.verdict, Verdict.FAIL)       # a GO review cannot override a failing test
        self.assertIsNone(r.delivered_to)
        self.assertFalse((self.root / "out").exists())
        self.assertIn("add is wrong", r.test.output)

    def test_reviewer_no_go_is_fail(self):
        c, r = self.run_task([WRITE_GOOD, FINISH], [review("NO_GO", "missing docstring required by goal")])
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assertIsNone(r.delivered_to)

    def test_unparseable_review_is_unknown_not_pass(self):
        c, r = self.run_task([WRITE_GOOD, FINISH], ["looks good to me!"])
        self.assertEqual(r.verdict, Verdict.UNKNOWN)
        self.assertIsNone(r.delivered_to)

    def test_invalid_review_decision_is_unknown(self):
        c, r = self.run_task([WRITE_GOOD, FINISH], [review("MAYBE")])
        self.assertEqual(r.verdict, Verdict.UNKNOWN)

    def test_reviewer_crash_is_unknown(self):
        c, r = self.run_task([WRITE_GOOD, FINISH], [])   # scripted reviewer raises ModelError
        self.assertEqual(r.verdict, Verdict.UNKNOWN)

    def test_acceptance_timeout_is_unknown(self):
        c, r = self.run_task([WRITE_GOOD, FINISH], [review("GO")],
                             check="import time\ntime.sleep(5)\n", timeout_s=1)
        self.assertEqual(r.verdict, Verdict.UNKNOWN)
        self.assertEqual(r.test.reason, "TIMEOUT")

    def test_reviewer_sees_files_and_test_result_but_not_worker_chat(self):
        c, r = self.run_task([step("write_file", thought="SECRET-THOUGHT", path="calc.py", content=GOOD_CODE),
                              FINISH], [review("GO")])
        prompt = self.reviewer.calls[0][0]["content"]
        self.assertIn("return a + b", prompt)
        self.assertIn("Acceptance result: PASS", prompt)
        self.assertNotIn("SECRET-THOUGHT", prompt)
        self.assertEqual(len(self.reviewer.calls[0]), 1)

    def test_reviewer_sees_changed_files_in_full_and_unchanged_by_name(self):
        c, r = self.run_task([WRITE_GOOD, FINISH], [review("GO")])
        prompt = self.reviewer.calls[0][0]["content"]
        self.assertIn("--- calc.py\nde", prompt)
        self.assertIn("unchanged files (content not shown): check.py", prompt)
        self.assertNotIn("assert add(2, 3)", prompt)

    def test_gate_denial_is_fed_back_and_loop_continues(self):
        c, r = self.run_task([step("write_file", path="../evil.py", content="x"), WRITE_GOOD, FINISH],
                             [review("GO")])
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assertFalse((self.root / "ws" / "evil.py").exists())
        self.assertIn("gate.denied", self.kinds())
        second_call = self.worker.calls[1]
        self.assertIn("DENIED by gate 'path_scope'", second_call[-1]["content"])

    def test_round_budget_stops_worker_and_skips_review(self):
        steps = [step("list_files", path="."), step("read_file", path="check.py"), step("list_files", path="x")]
        c, r = self.run_task(steps, [review("GO")], budget=Budget(max_rounds=2))
        self.assertEqual(r.worker.status, WorkerStatus.BUDGET_EXHAUSTED)
        self.assertEqual(r.verdict, Verdict.UNKNOWN)
        self.assertEqual(len(self.reviewer.calls), 0)
        self.assertEqual(r.usage["rounds"], 2)

    def test_command_budget_denies_extra_commands(self):
        run = step("run_command", command="{python} check.py")
        c, r = self.run_task([WRITE_GOOD, run, run, FINISH], [review("GO")], budget=Budget(max_commands=1))
        denied = [e.payload for e in self.ledger.events("t1", "gate.denied")]
        self.assertEqual([d["gate"] for d in denied], ["command_budget"])
        self.assertEqual(r.usage["commands"], 1)

    def test_repeated_identical_failure_stalls(self):
        bad = step("read_file", path="missing.py")
        c, r = self.run_task([bad, bad, bad, bad], [review("GO")])
        self.assertEqual(r.worker.status, WorkerStatus.STALLED)
        self.assertEqual(r.verdict, Verdict.FAIL)

    def test_invalid_model_output_stalls_after_three(self):
        c, r = self.run_task(["hello", "hello", "hello"], [review("GO")])
        self.assertEqual(r.worker.status, WorkerStatus.STALLED)
        self.assertEqual(self.kinds().count("proposal.invalid"), 3)

    def test_model_error_stops_worker(self):
        c, r = self.run_task([WRITE_GOOD], [review("GO")])     # runs out of scripted replies
        self.assertEqual(r.worker.status, WorkerStatus.MODEL_ERROR)
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assertIn("model.error", self.kinds())

    def test_task_id_cannot_be_reused(self):
        self.run_task([WRITE_GOOD, FINISH], [review("GO")])
        with self.assertRaises(KernelError):
            self.run_task([WRITE_GOOD, FINISH], [review("GO")])

    def test_acceptance_that_modifies_workspace_is_unknown(self):
        c, r = self.run_task([WRITE_GOOD, FINISH], [review("GO")],
                             check="open('new.txt', 'w').write('x')\n")
        self.assertEqual(r.verdict, Verdict.UNKNOWN)
        self.assertIsNone(r.delivered_to)

    def test_delivery_refuses_existing_target(self):
        (self.root / "out" / "t1").mkdir(parents=True)
        c, r = self.run_task([WRITE_GOOD, FINISH], [review("GO")])
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assertIsNone(r.delivered_to)
        self.assertIn("already exists", self.ledger.last("t1", "delivery.refused").payload["reason"])

    def test_every_effect_has_intent_before_result(self):
        self.run_task([step("list_files", path="."), WRITE_GOOD, FINISH], [review("GO")])
        evs = self.ledger.events("t1")
        for i, e in enumerate(evs):
            if e.kind == "effect.result":
                self.assertEqual(evs[i - 1].kind, "effect.intent")
                self.assertEqual(evs[i - 1].op_id, e.op_id)

    def test_protected_file_write_is_denied(self):
        c, r = self.run_task([step("write_file", path="check.py", content="print('ok')"), WRITE_GOOD, FINISH],
                             [review("GO")], protected=("check.py",))
        self.assertEqual(self.ledger.last("t1", "gate.denied").payload["gate"], "protected_path")
        self.assertEqual(r.verdict, Verdict.PASS)

    def test_protected_file_changed_via_command_fails(self):
        cheat = step("run_command", command='{python} -c "open(\'check.py\',\'w\').write(\'print(1)\')"')
        c, r = self.run_task([WRITE_BAD, cheat, FINISH], [review("GO")], protected=("check.py",))
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assertEqual(r.test.reason, "PROTECTED_FILE_CHANGED")
        self.assertIsNone(r.delivered_to)


class SeedTest(KernelCase):
    def test_seed_is_copied_and_must_target_empty_workspace(self):
        from m2k.contract import Acceptance, TaskContract
        seed = self.root / "seed"
        seed.mkdir()
        (seed / "check.py").write_text("print('ok')")
        c = TaskContract("s1", "g", self.root / "ws-s1", Acceptance("{python} check.py", files=("check.py",)), seed=seed,
                         protected=("check.py",))
        from m2k.models import ScriptedModel
        r = Kernel(self.ledger, ScriptedModel([FINISH]), ScriptedModel([review("GO")])).run(c)
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assertIn("check.py", self.ledger.last("s1", "task.submitted").payload["protected"])
        c2 = TaskContract("s2", "g", self.root / "ws-s1", Acceptance("x", files=("check.py",)), seed=seed)
        with self.assertRaises(KernelError):
            Kernel(self.ledger, ScriptedModel([]), ScriptedModel([])).run(c2)


if __name__ == "__main__":
    unittest.main()
