"""T27: a planner model proposes the split; the plan is a proposal that must pass every plan rule before it runs."""
import json
import unittest
from dataclasses import replace
from pathlib import Path

from m2k import trust
from m2k.contract import Budget, ContractError, parse_contract
from m2k.kernel import Kernel, status
from m2k.models import ScriptedModel
from tests.helpers import review
from tests.test_task_tree import TreeCase, good_a, good_b

GO = review("GO")
PARTS = (("check_a.py", "{python} check_a.py"), ("check_b.py", "{python} check_b.py"))
CAPS = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"], "max_rounds": 12,
        "max_model_calls": 16, "max_tokens": 200000, "max_commands": 6, "network": False}


def entry(sid, write, check, calls=5, **extra):
    return dict({"id": sid, "goal": f"write {write}", "writes": [write], "acceptance_files": [check],
                 "budget": {"max_rounds": 4, "max_model_calls": calls, "max_commands": 2}}, **extra)


def plan(*subs):
    return json.dumps({"subtasks": list(subs)})


GOOD_PLAN = plan(entry("a", "a.py", "check_a.py"), entry("b", "b.py", "check_b.py"))


class PlanCase(TreeCase):
    def planned(self, budget=None, tid="p1"):
        c = self.parent(tid=tid, budget=budget)
        return replace(c, subtasks=(), plan="model", acceptance=replace(c.acceptance, parts=PARTS))

    def run_plan(self, worker_steps, reviews, contract=None, setup=None):
        self.worker = ScriptedModel(worker_steps, "w")
        self.k = Kernel(self.ledger, self.worker, ScriptedModel(reviews, "r"), trust=setup)
        return self.k.run(contract or self.planned())

    def rejected(self):
        return [e.payload["reason"] for e in self.ledger.events("p1", "plan.rejected")]


class ContractTest(unittest.TestCase):
    def data(self, **acc):
        return {"task": {"id": "p", "goal": "g", "workspace": "ws", "plan": "model"},
                "acceptance": dict({"command": "{python} check_all.py", "files": ["check_all.py", "check_a.py"],
                                    "parts": {"check_a.py": "{python} check_a.py"}}, **acc)}

    def test_plan_and_parts_parse(self):
        c = parse_contract(self.data(), Path("."))
        self.assertEqual((c.plan, c.acceptance.parts), ("model", (("check_a.py", "{python} check_a.py"),)))

    def test_bad_plan_contracts(self):
        cases = {"with subtasks": dict(self.data(), subtasks=[{}]),
                 "without parts": self.data(parts={}),
                 "foreign part": self.data(parts={"mine.py": "{python} mine.py"}),
                 "command without its file": self.data(parts={"check_a.py": "{python} -c pass"})}
        other = self.data()
        other["task"] = dict(other["task"], plan="other")
        cases["other plan"] = other
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(ContractError):
                parse_contract(data, Path("."))


class PlanTest(PlanCase):
    def test_a_valid_model_plan_runs_to_pass(self):
        r = self.run_plan([GOOD_PLAN] + good_a() + good_b(), [GO] * 3)
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        kinds = [e.kind for e in self.ledger.events("p1")]
        self.assertLess(kinds.index("plan.proposed"), kinds.index("plan.accepted"))
        self.assertEqual([e.payload["role"] for e in self.ledger.events("p1", "model.called")], ["planner", "reviewer"])
        accepted = self.ledger.events("p1", "plan.accepted")[0].payload["subtasks"]
        self.assertEqual([s["acceptance"]["command"] for s in accepted], ["{python} check_a.py", "{python} check_b.py"])
        st = status(self.ledger, "p1")
        self.assertEqual((st["plan"], st["planned"]), ("model", ["a", "b"]))
        self.assertEqual([s["verdict"] for s in st["subtasks"]], ["PASS", "PASS"])

    def test_under_a_root_chain_children_follow_the_plan_and_the_parent_its_contract(self):
        setup, _ = trust.dev_setup(self.root / "keys", CAPS)
        c = self.planned(budget=Budget(max_rounds=10, max_model_calls=12, max_commands=4, max_tokens=100000))
        steps = [plan(*(dict(entry(x, f"{x}.py", f"check_{x}.py"), budget={"max_rounds": 4, "max_model_calls": 5,
                                                                         "max_commands": 2, "max_tokens": 40000})
                        for x in "ab"))]
        r = self.run_plan(steps + good_a() + good_b(), [GO] * 3, c, setup)
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        parent = self.ledger.events("p1", "credential.issued")[0].payload["credential"]["body"]
        self.assertEqual(parent["contract_sha256"], c.digest())
        child = self.ledger.events("p1.a", "credential.issued")[0].payload["credential"]["body"]
        self.assertEqual((child["subtask"], child["parent_credential_id"]), ("a", parent["credential_id"]))

    def test_each_rule_rejects_the_plan(self):
        cases = {
            "keys not allowed": plan(entry("a", "a.py", "check_a.py", acceptance="{python} -c pass")),
            "keys not allowed ": plan(entry("a", "a.py", "check_a.py", secret=1)),
            "owner checks": plan(entry("a", "a.py", "mine.py")),
            "1 to 6": plan(*(entry(f"s{i}", f"f{i}.py", "check_a.py", calls=1) for i in range(7))),
            "protected or acceptance": plan(entry("a", "check_all.py", "check_a.py")),
            "the plan needs": plan(entry("a", "a.py", "check_a.py", calls=6), entry("b", "b.py", "check_b.py", calls=5)),
            "tools must be a subset": plan(entry("a", "a.py", "check_a.py", tools=["read_file", "teleport"])),
            "network may be true": plan(entry("a", "a.py", "check_a.py", network=True)),
            "no JSON": "I think we should split it into two parts.",
        }
        for i, (needle, bad) in enumerate(cases.items()):
            with self.subTest(needle.strip()):
                c = self.planned(tid=f"q{i}")
                r = self.run_plan([bad, bad], [], c)
                reasons = [e.payload["reason"] for e in self.ledger.events(c.task_id, "plan.rejected")]
                self.assertEqual(len(reasons), 2)
                self.assertIn(needle.strip(), reasons[0])
                self.assertEqual(r.verdict.value, "FAIL")
                self.assertEqual(status(self.ledger, c.task_id)["worker"]["status"], "PLAN_REJECTED")
                self.assertEqual(self.ledger.events(c.task_id, "subtask.started"), [])

    def test_a_rejected_plan_gets_one_more_try_with_the_reason(self):
        bad = plan(entry("a", "a.py", "mine.py"))
        smaller = plan(entry("a", "a.py", "check_a.py", calls=4), entry("b", "b.py", "check_b.py", calls=4))
        r = self.run_plan([bad, smaller] + good_a() + good_b(), [GO] * 3)
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        second_prompt = self.worker.calls[1][-1]["content"]
        self.assertIn("Your previous plan was rejected:", second_prompt)
        self.assertIn("owner checks", second_prompt)
        self.assertEqual(len(self.rejected()), 1)

    def test_no_planner_call_without_budget(self):
        c = self.planned(budget=Budget(max_rounds=10, max_model_calls=0, max_commands=4))
        r = self.run_plan([GOOD_PLAN], [], c)
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertEqual(self.ledger.events("p1", "model.called"), [])
        self.assertEqual(status(self.ledger, "p1")["worker"]["status"], "PLAN_REJECTED")

    def test_a_planned_parent_is_never_repaired_as_a_single_task(self):
        c = self.planned()
        (c.workspace / "check_all.py").write_text("from a import A\nfrom b import B\nassert A + B == 4\n", encoding="utf-8")
        r = self.run_plan([GOOD_PLAN] + good_a() + good_b(), [GO] * 3, replace(c, max_repairs=2))
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertEqual(self.ledger.events("p1", "repair.started"), [])

    def test_the_planner_sees_the_owner_checks_but_cannot_choose_others(self):
        self.run_plan([GOOD_PLAN] + good_a() + good_b(), [GO] * 3)
        prompt = self.worker.calls[0][-1]["content"]
        self.assertIn("check_a.py", prompt)
        self.assertIn("A must be 1", prompt)              # the owner check's text
        self.assertNotIn("check_all.py: `", prompt)       # the overall check is not offered as a part


if __name__ == "__main__":
    unittest.main()
