"""T25: bounded repair from ledger facts, with cumulative budgets and integrity checks."""
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from m2k import trust
from m2k.contract import Budget, ContractError, Subtask, load_contract
from m2k.kernel import Kernel
from m2k.models import ScriptedModel
from m2k.types import Verdict, WorkerStatus
from m2k.verify import run_test
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, review, step

WRITE_BAD = step("write_file", path="calc.py", content=BAD_CODE)
WRITE_GOOD = step("write_file", path="calc.py", content=GOOD_CODE)
SUMMARY = "WORKER-STORY-MUST-NOT-BECOME-FACTS"
FINISH = step("finish", summary=SUMMARY)
HEADING = "Facts from the ledger (not opinions):"


class RepairContractTest(unittest.TestCase):
    def test_max_repairs_default_range_and_invalid_types(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "task.toml"
            base = ('[task]\nid = "t"\ngoal = "g"\nworkspace = "ws"\n{}\n'
                    '[acceptance]\ncommand = "{{python}} check.py"\nfiles = ["check.py"]\n')
            for value, expected in [(None, 0), *((str(n), n) for n in range(6))]:
                with self.subTest(value=value):
                    path.write_text(base.format("" if value is None else f"max_repairs = {value}"), encoding="utf-8")
                    self.assertEqual(load_contract(path).max_repairs, expected)
            for value in ("-1", "6", '"2"', "true", "false", "2.0", "[]", "{}"):
                with self.subTest(value=value):
                    path.write_text(base.format(f"max_repairs = {value}"), encoding="utf-8")
                    with self.assertRaisesRegex(ContractError, "max_repairs"):
                        load_contract(path)


class RepairTest(KernelCase):
    def run_repair(self, steps, reviews, *, max_repairs=1, contract=None, trust_setup=None, **kwargs):
        c = replace(contract or self.contract(**kwargs), max_repairs=max_repairs)
        self.worker = ScriptedModel(steps, "worker")
        self.reviewer = ScriptedModel(reviews, "reviewer")
        self.kernel = Kernel(self.ledger, self.worker, self.reviewer, trust=trust_setup)
        return c, self.kernel.run(c)

    def assert_attempts(self, c, report, attempts, repairs):
        self.assertEqual(len(self.ledger.events(c.task_id, "candidate.frozen")), attempts)
        self.assertEqual(len(self.ledger.events(c.task_id, "test.result")), attempts)
        self.assertEqual(len(self.ledger.events(c.task_id, "repair.started")), repairs)
        verdicts = self.ledger.events(c.task_id, "verdict")
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].payload["candidate"], report.candidate.digest)
        self.assertEqual(verdicts[0].payload["verdict"], report.verdict.value)
        self.assertEqual(verdicts[0].payload["reasons"], report.reasons)
        if c.max_repairs and not c.subtasks:
            self.assertEqual(report.reasons[0], f"repair attempts: {repairs}")
        self.assertTrue(self.ledger.verify_chain()[0])

    def test_zero_repairs_preserves_existing_failure_and_prompt(self):
        c, r = self.run_repair([WRITE_BAD, FINISH, WRITE_GOOD, FINISH], [review("GO")], max_repairs=0)
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assertIsNone(r.delivered_to)
        self.assertEqual(len(self.worker.calls), 2)
        self.assertEqual(r.reasons, ["test: FAIL", "review: GO"])
        self.assertNotIn(HEADING, self.worker.calls[0][1]["content"])
        self.assert_attempts(c, r, 1, 0)

    def test_wrong_code_is_repaired_and_only_final_candidate_delivered(self):
        c, r = self.run_repair([WRITE_BAD, FINISH, WRITE_GOOD, FINISH], [review("GO"), review("GO")])
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assertEqual((r.delivered_to / "calc.py").read_text(encoding="utf-8"), GOOD_CODE)
        self.assert_attempts(c, r, 2, 1)
        self.assertEqual(len(self.ledger.events(c.task_id, "task.submitted")), 1)
        self.assertEqual(len(self.ledger.events(c.task_id, "audit.result")), 2)
        first_test = self.ledger.events(c.task_id, "test.result")[0]
        repair = self.ledger.last(c.task_id, "repair.started")
        self.assertEqual(repair.payload["attempt"], 1)
        self.assertEqual(repair.payload["candidate"], first_test.payload["candidate"])
        self.assertIn(f"exit_code={first_test.payload['exit_code']}", repair.payload["facts"])
        self.assertIn(first_test.payload["output"][-1500:], repair.payload["facts"])
        prompt = self.worker.calls[2][1]["content"]
        self.assertIn(HEADING + "\n" + repair.payload["facts"], prompt)
        self.assertLess(prompt.index(c.acceptance.command), prompt.index(HEADING))
        self.assertLess(prompt.index(HEADING), prompt.index("Start by looking at the workspace."))
        self.assertEqual(len(self.worker.calls[2]), 2)  # a fresh conversation, in the same workspace
        self.assertNotIn(SUMMARY, repair.payload["facts"])
        self.assertNotIn(SUMMARY, prompt)
        for call in self.reviewer.calls:
            self.assertNotIn(SUMMARY, call[0]["content"])
        frozen = self.ledger.events(c.task_id, "candidate.frozen")
        self.assertNotEqual(frozen[0].payload["digest"], frozen[1].payload["digest"])
        delivery = self.ledger.last(c.task_id, "delivery.intent")
        self.assertEqual(delivery.payload["candidate"], frozen[1].payload["digest"])
        self.assertLess(first_test.seq, repair.seq)
        self.assertLess(repair.seq, frozen[1].seq)

    def test_no_go_reasons_are_ledger_facts_and_repair_keeps_files(self):
        fix = step("edit_file", path="calc.py", old="def add", new="# documented\ndef add")
        c, r = self.run_repair([WRITE_GOOD, FINISH, fix, FINISH],
                               [review("NO_GO", "missing documentation", "explain addition"), review("GO")])
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assert_attempts(c, r, 2, 1)
        result = self.ledger.events(c.task_id, "audit.result")[0].payload
        facts = self.ledger.last(c.task_id, "repair.started").payload["facts"]
        self.assertIn("decision=" + result["decision"], facts)
        for reason in result["reasons"]:
            self.assertIn(reason, facts)
            self.assertIn(reason, self.worker.calls[2][1]["content"])
        self.assertEqual((c.workspace / "calc.py").read_text(encoding="utf-8"), "# documented\n" + GOOD_CODE)

    def test_limit_stops_after_exactly_three_attempts(self):
        c, r = self.run_repair([WRITE_BAD, FINISH] * 4, [review("GO")] * 4, max_repairs=2)
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assertIsNone(r.delivered_to)
        self.assert_attempts(c, r, 3, 2)
        self.assertEqual(len(self.worker.calls), 6)
        self.assertEqual(len(self.reviewer.calls), 3)
        self.assertEqual([e.payload["attempt"] for e in self.ledger.events(c.task_id, "repair.started")], [1, 2])
        self.assertEqual(r.usage["rounds"], 6)
        self.assertEqual(r.usage["model_calls"], 9)
        self.assertEqual(r.usage["tokens"], 180)

    def test_briefing_uses_last_attempt_not_earlier_facts(self):
        check = "from calc import add\nresult = add(2, 3)\nassert result == 5, f'got {result}'\n"
        zero = step("write_file", path="calc.py", content="def add(a, b):\n    return 0\n")
        c, r = self.run_repair([WRITE_BAD, FINISH, zero, FINISH, WRITE_GOOD, FINISH],
                               [review("NO_GO", "first reason"), review("NO_GO", "second reason"), review("GO")],
                               max_repairs=2, check=check)
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assert_attempts(c, r, 3, 2)
        facts = self.ledger.last(c.task_id, "repair.started").payload["facts"]
        self.assertIn("got 0", facts)
        self.assertIn("second reason", facts)
        self.assertNotIn("got -1", facts)
        self.assertNotIn("first reason", facts)

    def test_acceptance_output_briefing_has_only_last_1500_characters(self):
        check = "print('OLD-OUTPUT-' + 'x' * 2200 + 'OUTPUT-TAIL')\nraise SystemExit(1)\n"
        c, r = self.run_repair([FINISH, FINISH], [review("GO"), review("GO")], check=check)
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assert_attempts(c, r, 2, 1)
        output = self.ledger.events(c.task_id, "test.result")[0].payload["output"]
        facts = self.ledger.last(c.task_id, "repair.started").payload["facts"]
        self.assertEqual(len(output), 2000)
        self.assertIn(output[-1500:], facts)
        self.assertNotIn(output[-1501:], facts)
        self.assertNotIn("OLD-OUTPUT-", facts)

    def test_unknown_review_never_repairs_even_when_acceptance_failed(self):
        for i, code in enumerate((WRITE_GOOD, WRITE_BAD)):
            with self.subTest(code=code):
                c, r = self.run_repair([code, FINISH, WRITE_GOOD, FINISH], ["not a review"], task_id=f"unknown-{i}")
                self.assertEqual(r.verdict, Verdict.UNKNOWN if code == WRITE_GOOD else Verdict.FAIL)
                self.assertEqual(r.audit.verdict, Verdict.UNKNOWN)
                self.assertIsNone(r.delivered_to)
                self.assertEqual(len(self.worker.calls), 2)
                self.assert_attempts(c, r, 1, 0)

    def test_stalled_worker_never_repairs(self):
        missing = step("read_file", path="missing.py")
        c, r = self.run_repair([missing] * 3 + [WRITE_GOOD, FINISH], [review("GO")])
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assertEqual(r.worker.status, WorkerStatus.STALLED)
        self.assertEqual(len(self.reviewer.calls), 0)
        self.assertIsNone(r.delivered_to)
        self.assert_attempts(c, r, 1, 0)

    def test_worker_model_error_never_repairs(self):
        c, r = self.run_repair([WRITE_BAD], [review("GO")])
        self.assertEqual(r.worker.status, WorkerStatus.MODEL_ERROR)
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assert_attempts(c, r, 1, 0)

    def test_integrity_failures_never_repair_in_first_or_later_attempt(self):
        cases = (("check.py", "PROTECTED_FILE_CHANGED"), ("json.py", "SHADOWED_MODULE"),
                 ("outside.txt", "OUT_OF_SCOPE"))
        for name, reason in cases:
            for later in (False, True):
                with self.subTest(reason=reason, later=later):
                    c = replace(self.contract(task_id=f"{reason}-{later}"),
                                writes=("calc.py",) if reason == "OUT_OF_SCOPE" else ())

                    def fixture_change(messages):
                        # A benign on-disk fixture change; no attack code or executable shadow payload.
                        (c.workspace / name).write_text("", encoding="utf-8")
                        return FINISH

                    steps = ([WRITE_BAD, FINISH] if later else []) + [WRITE_GOOD, fixture_change, WRITE_GOOD, FINISH]
                    with patch("m2k.kernel.run_test", wraps=run_test) as test_call:
                        c, r = self.run_repair(steps, [review("GO")] * 3, max_repairs=5, contract=c)
                    self.assertEqual(test_call.call_count, int(later))
                    self.assertEqual(r.verdict, Verdict.FAIL)
                    self.assertEqual(r.test.reason, reason)
                    self.assertIsNone(r.delivered_to)
                    self.assert_attempts(c, r, 2 if later else 1, int(later))

    def test_changed_candidate_never_repairs_with_no_go_review(self):
        check = "from pathlib import Path\nPath('test-output.txt').write_text('result', encoding='utf-8')\n"
        c, r = self.run_repair([WRITE_GOOD, FINISH, WRITE_GOOD, FINISH], [review("NO_GO", "reject")], check=check)
        self.assertEqual(r.test.reason, "CANDIDATE_CHANGED")
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assertIsNone(r.delivered_to)
        self.assert_attempts(c, r, 1, 0)

    def test_unknown_acceptance_never_repairs_with_no_go_review(self):
        c, r = self.run_repair([WRITE_GOOD, FINISH, WRITE_GOOD, FINISH], [review("NO_GO", "reject")],
                               check="import time\ntime.sleep(5)\n", timeout_s=1)
        self.assertEqual(r.test.verdict, Verdict.UNKNOWN)
        self.assertEqual(r.test.reason, "TIMEOUT")
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assert_attempts(c, r, 1, 0)

    def test_budget_prevents_repair_start_without_reset(self):
        for i, budget in enumerate((Budget(max_model_calls=3), Budget(max_rounds=2), Budget(max_tokens=60))):
            with self.subTest(budget=budget):
                c, r = self.run_repair([WRITE_BAD, FINISH, WRITE_GOOD, FINISH], [review("GO"), review("GO")],
                                       budget=budget, task_id=f"budget-{i}")
                self.assertEqual(r.verdict, Verdict.FAIL)
                self.assert_attempts(c, r, 1, 0)
                self.assertEqual(r.usage["model_calls"], 3)
                self.assertEqual(r.usage["tokens"], 60)
                self.assertEqual(len(self.worker.calls), 2)

    def test_repair_stops_at_remaining_budget(self):
        for limit in (4, 5):
            with self.subTest(limit=limit):
                c, r = self.run_repair([WRITE_BAD, FINISH, WRITE_GOOD, FINISH], [review("GO"), review("GO")],
                                       max_repairs=5, budget=Budget(max_model_calls=limit), task_id=f"mid-budget-{limit}")
                self.assertEqual(r.verdict, Verdict.UNKNOWN)
                self.assertEqual(r.worker.status, WorkerStatus.BUDGET_EXHAUSTED if limit == 4 else WorkerStatus.FINISHED)
                self.assertIsNone(r.delivered_to)
                self.assert_attempts(c, r, 2, 1)
                self.assertEqual(r.usage["model_calls"], limit)
                events = self.ledger.events(c.task_id, "model.called")
                self.assertEqual(r.usage["tokens"], sum(e.payload["input_tokens"] + e.payload["output_tokens"] for e in events))
                self.assertEqual(r.usage["tokens"], limit * 20)

    def test_command_budget_is_shared_across_repairs(self):
        command = step("run_command", command="{python} check.py")
        c, r = self.run_repair([WRITE_BAD, command, FINISH, WRITE_GOOD, command, FINISH],
                               [review("GO"), review("GO")], budget=Budget(max_commands=1))
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assert_attempts(c, r, 2, 1)
        self.assertEqual(r.usage["commands"], 1)
        self.assertEqual(self.ledger.last(c.task_id, "gate.denied").payload["gate"], "command_budget")
        self.assertIn("DENIED", self.worker.calls[-1][-1]["content"])

    def test_successful_first_attempt_never_repairs(self):
        c, r = self.run_repair([WRITE_GOOD, FINISH, WRITE_BAD], [review("GO")], max_repairs=5)
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assert_attempts(c, r, 1, 0)
        self.assertEqual(len(self.worker.calls), 2)

    def trust_setup(self):
        caps = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"],
                "max_rounds": 20, "max_model_calls": 30, "max_tokens": 100000, "max_commands": 30, "network": False}
        setup, _ = trust.dev_setup(self.root / "keys", caps)
        return setup

    def test_trust_failure_never_repairs(self):
        setup = self.trust_setup()

        def expire(messages):
            self.kernel.clock = lambda: setup.ttl_s + 10000 + now
            return FINISH

        now = int(time.time())
        c, r = self.run_repair([WRITE_BAD, expire, WRITE_GOOD, FINISH], [review("GO")],
                               trust_setup=setup, budget=Budget(max_tokens=50000))
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assertEqual(r.test.reason, "TRUST_EXPIRED")
        self.assertIsNone(r.audit)
        self.assertIsNone(r.delivered_to)
        self.assert_attempts(c, r, 1, 0)

    def test_trusted_repair_keeps_one_credential(self):
        c, r = self.run_repair([WRITE_BAD, FINISH, WRITE_GOOD, FINISH], [review("GO"), review("GO")],
                               trust_setup=self.trust_setup(), budget=Budget(max_tokens=50000))
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assertIsNotNone(r.delivered_to)
        self.assert_attempts(c, r, 2, 1)
        self.assertEqual(len(self.ledger.events(c.task_id, "credential.issued")), 1)

    def test_subtasks_do_not_inherit_parent_repairs(self):
        c = self.contract()
        sub = Subtask("calc", "write calc.py", ("calc.py",), c.acceptance,
                      Budget(max_rounds=4, max_model_calls=5, max_commands=0), c.tools)
        c = replace(c, subtasks=(sub,))
        c, r = self.run_repair([WRITE_BAD, FINISH, WRITE_GOOD, FINISH], [review("GO"), review("GO")],
                               contract=c, max_repairs=5)
        self.assertEqual(r.verdict, Verdict.FAIL)
        self.assertEqual(r.worker.status, WorkerStatus.SUBTASK_FAILED)
        self.assertEqual(self.ledger.events(kind="repair.started"), [])
        self.assertEqual(len(self.worker.calls), 2)
        self.assert_attempts(c, r, 1, 0)
        self.assertEqual(len(self.ledger.events(c.task_id + ".calc", "candidate.frozen")), 1)


if __name__ == "__main__":
    unittest.main()
