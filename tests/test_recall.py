"""T24: recall a subtask, reassign it, the successor starts from ledger facts; budget never reset; no repeats."""
import time
import unittest
from dataclasses import replace

from m2k import trust
from m2k.cli import main
from m2k.contract import Budget
from m2k.kernel import Kernel, KernelError, recall, status
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from tests.helpers import review, step
from tests.test_task_tree import TreeCase, good_a, good_b, sub

PY = "{python}"
GO = review("GO")
CAPS = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"], "max_rounds": 12,
        "max_model_calls": 16, "max_tokens": 200000, "max_commands": 6, "network": False}


def wrong_a(summary="done"):
    return [step("write_file", path="a.py", content="A = 5\n"), step("finish", summary=summary)]


class RecallCase(TreeCase):
    def tree(self, reassign=1, a_budget=(6, 8, 2), tokens=None, tid="p1"):
        rounds, calls, commands = a_budget
        a = replace(sub("a", ["a.py"], "check_a.py", rounds=rounds, calls=calls, commands=commands, tokens=tokens),
                    max_reassign=reassign)
        b = sub("b", ["b.py"], "check_b.py", tokens=tokens)
        budget = Budget(max_rounds=12, max_model_calls=16, max_commands=6, max_tokens=200000 if tokens else None)
        return self.parent(tid=tid, subs=(a, b), budget=budget)

    def kernel(self, worker_steps, reviews, setup=None):
        self.k = Kernel(self.ledger, ScriptedModel(worker_steps, "w"), ScriptedModel(reviews, "r"), trust=setup)
        return self.k

    def recalls(self, tid="p1"):
        return [e.payload for e in self.ledger.events(tid, "subtask.recalled")]

    def denied(self, tid):
        return [e.payload["reason"].split(":", 1)[0] for e in self.ledger.events(tid, "gate.denied")]

    def merged(self, tid="p1"):
        return [e.payload["args"]["subtask"] for e in self.ledger.events(tid, "effect.intent")
                if e.payload.get("tool") == "merge_subtask"]


class RecallTest(RecallCase):
    def test_refusals_write_nothing(self):
        self.assertIn("unknown task", recall(self.ledger, "nobody", "x"))   # T30 point 4
        r = self.kernel(good_a() + good_b(), [GO] * 3).run(self.tree(reassign=0))
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        self.assertIn("not a subtask", recall(self.ledger, "p1", "x"))
        self.assertIn("already has a verdict", recall(self.ledger, "p1.a", "x"))
        self.assertEqual(self.recalls(), [])

    def test_recalling_twice_is_refused(self):
        answers = []

        def twice(messages):
            answers.extend([recall(self.ledger, "p1.a", "first"), recall(self.ledger, "p1.a", "second")])
            return step("write_file", path="a.py", content="A = 1\n")
        self.kernel([twice, step("finish", summary="x")], []).run(self.tree(reassign=0))
        self.assertEqual(answers[0], None)
        self.assertIn("already recalled", answers[1])
        self.assertEqual([p["reason"] for p in self.recalls()], ["first"])

    def check_recalled_mid_run(self, setup=None, how="kernel"):
        def recall_then_write(messages):
            if how == "kernel":
                self.assertIsNone(recall(self.ledger, "p1.a", "owner says stop"))
            elif how == "connection":
                with Ledger(self.ledger.path) as other:      # as `m2k recall` would, from another process
                    self.assertIsNone(recall(other, "p1.a", "owner says stop"))
            else:
                self.assertEqual(main(["--ledger", str(self.ledger.path), "recall", "p1.a", "--reason", "cli"]), 0)
            return step("write_file", path="a.py", content="A = 1\n")
        tokens = 50000 if setup else None
        r = self.kernel([step("list_files", path="."), recall_then_write, step("finish", summary="x")], [],
                        setup).run(self.tree(reassign=0, tokens=tokens))
        self.assertEqual(self.denied("p1.a"), ["RECALLED"])
        self.assertEqual(status(self.ledger, "p1.a")["worker"]["status"], "TRUST_BROKEN")
        self.assertEqual(self.merged(), [])
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertFalse((self.root / "ws" / "p1" / "a.py").exists())

    def test_recall_mid_run_without_a_root_chain(self):
        self.check_recalled_mid_run()

    def test_recall_mid_run_with_a_root_chain(self):
        setup, _ = trust.dev_setup(self.root / "keys", CAPS)
        self.check_recalled_mid_run(setup)

    def test_recall_from_another_connection(self):
        self.check_recalled_mid_run(how="connection")

    def test_recall_through_the_cli(self):
        self.check_recalled_mid_run(how="cli")
        self.assertEqual(main(["--ledger", str(self.ledger.path), "recall", "p1.a"]), 1)   # already has a verdict

    def test_a_recalled_attempt_that_finishes_anyway_is_kept_but_never_adopted(self):
        def recall_then_finish(messages):
            recall(self.ledger, "p1.a", "too late")
            return step("finish", summary="correct work")
        r = self.kernel([step("write_file", path="a.py", content="A = 1\n"), recall_then_finish], []).run(
            self.tree(reassign=0))
        frozen = self.ledger.events("p1.a", "candidate.frozen")[0].payload["files"]
        test = self.ledger.events("p1.a", "test.result")[0].payload
        self.assertIn("a.py", frozen)
        self.assertEqual((test["verdict"], test["reason"]), ("FAIL", "TRUST_RECALLED"))
        self.assertNotEqual(self.ledger.events("p1.a", "verdict")[0].payload["verdict"], "PASS")
        self.assertEqual(self.merged(), [])
        self.assertEqual(r.verdict.value, "FAIL")


class ReassignTest(RecallCase):
    def test_successor_starts_from_ledger_facts_and_passes(self):
        first = [step("run_command", thought="SECRET-THOUGHT", command=f"{PY} check_a.py"),
                 *wrong_a(summary="SECRET-SUMMARY all good")]
        r = self.kernel(first + good_a() + good_b(), [GO] * 4).run(self.tree())
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        self.assertEqual(self.merged(), ["a", "b"])
        goal = self.ledger.events("p1.a.e2", "task.submitted")[0].payload["context"]   # T32: facts live in context
        self.assertIn("Facts from the ledger about earlier attempts", goal)
        self.assertIn("A must be 1", goal)                 # the first attempt's acceptance output
        self.assertIn("check_a.py", goal)                  # the command it ran
        self.assertNotIn("SECRET-SUMMARY", goal)
        self.assertNotIn("SECRET-THOUGHT", goal)
        started = [(e.payload["task_id"], e.payload["epoch"]) for e in self.ledger.events("p1", "subtask.started")]
        self.assertEqual(started, [("p1.a", 1), ("p1.a.e2", 2), ("p1.b", 1)])

    def test_successor_has_its_own_epoch_credential(self):
        setup, _ = trust.dev_setup(self.root / "keys", CAPS)
        r = self.kernel(wrong_a() + good_a() + good_b(), [GO] * 4, setup).run(self.tree(tokens=50000))
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        cred = self.ledger.events("p1.a.e2", "credential.issued")[0].payload["credential"]["body"]
        self.assertEqual((cred["epoch"], cred["task_id"]), (2, "p1.a.e2"))

    def test_successor_budget_is_what_is_left(self):
        self.kernel(wrong_a() + good_a() + good_b(), [GO] * 4).run(self.tree())
        reserved = {e.payload["child"]: e.payload["amounts"] for e in self.ledger.events("p1", "budget.reserved")}
        used = self.ledger.usage("p1.a")
        self.assertEqual(reserved["p1.a.e2"]["rounds"], 6 - used["rounds"])
        self.assertEqual(reserved["p1.a.e2"]["model_calls"], 8 - used["model_calls"])
        self.assertEqual(reserved["p1.a.e2"]["commands"], 2 - used["commands"])

    def test_no_successor_when_the_plan_budget_is_spent(self):
        r = self.kernel(wrong_a() + good_a(), [GO] * 2).run(self.tree(a_budget=(2, 3, 1)))
        self.assertEqual(self.ledger.events("p1.a.e2"), [])
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertIn("subtask a", status(self.ledger, "p1")["worker"]["reason"])

    def test_handing_in_the_same_failure_again_is_refused_without_running_the_test(self):
        r = self.kernel(wrong_a() + wrong_a(), [GO] * 2).run(self.tree())
        test = self.ledger.events("p1.a.e2", "test.result")[0].payload
        self.assertEqual((test["verdict"], test["reason"], test["exit_code"]), ("FAIL", "REPEATED_FAILURE", None))
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertEqual(self.merged(), [])

    def test_a_different_attempt_is_not_a_repeat(self):
        r = self.kernel(wrong_a() + [step("write_file", path="a.py", content="A = 7\n"), step("finish", summary="x")],
                        [GO] * 2).run(self.tree())
        self.assertEqual(self.ledger.events("p1.a.e2", "test.result")[0].payload["reason"], "")
        self.assertEqual(r.verdict.value, "FAIL")

    def test_unknown_effect_is_flagged_in_the_facts(self):
        hang = step("run_command", command=f"{PY} -c \"import time; time.sleep(5)\"", timeout_s=1)
        r = self.kernel([hang, *wrong_a()] + good_a() + good_b(), [GO] * 4).run(self.tree())
        goal = self.ledger.events("p1.a.e2", "task.submitted")[0].payload["context"]   # T32: facts live in context
        self.assertIn("UNKNOWN — outcome unknown — that workspace was discarded", goal)
        self.assertEqual(r.verdict.value, "PASS", r.reasons)

    def test_every_attempt_id_and_workspace_is_checked_at_submit(self):
        self.ledger.append("p1.a.e2", "note", {})
        with self.assertRaises(KernelError):
            self.kernel([], []).run(self.tree())
        self.assertEqual(self.ledger.events("p1"), [])
        c = self.tree(tid="p2")
        d = c.workspace.parent / "p2.subtasks" / "a.e2"
        d.mkdir(parents=True)
        (d / "x").write_text("x", encoding="utf-8")
        with self.assertRaises(KernelError):
            self.kernel([], []).run(c)
        self.assertEqual(self.ledger.events("p2"), [])

    def test_parent_usage_counts_every_attempt(self):
        r = self.kernel(wrong_a() + good_a() + good_b(), [GO] * 4).run(self.tree())
        total = sum(self.ledger.usage(t)["model_calls"] for t in ("p1", "p1.a", "p1.a.e2", "p1.b"))
        self.assertEqual(r.usage["model_calls"], total)


if __name__ == "__main__":
    unittest.main()
