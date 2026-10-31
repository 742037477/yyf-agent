"""T21: a parent task runs its subtasks one after another, each under the full chain; only PASS is merged back."""
import time
import unittest
from pathlib import Path

from m2k import trust
from m2k.contract import Acceptance, Budget, ContractError, Subtask, TaskContract, parse_contract
from m2k.kernel import Kernel, status
from m2k.models import ScriptedModel
from tests.helpers import KernelCase, review, step

CHECK_A = "from a import A\nassert A == 1, 'A must be 1'\nprint('a ok')\n"
CHECK_B = "from b import B\nassert B == 2, 'B must be 2'\nprint('b ok')\n"
CHECK_ALL = "from a import A\nfrom b import B\nassert A + B == 3, 'A + B must be 3'\nprint('all ok')\n"
PY = "{python}"


def sub(sid, writes, check, rounds=4, calls=5, commands=2, tokens=None):
    return Subtask(sid, f"write {writes[0]}", tuple(writes), Acceptance(f"{PY} {check}", 60, (check,)),
                   Budget(max_rounds=rounds, max_model_calls=calls, max_commands=commands, max_tokens=tokens),
                   ("list_files", "read_file", "write_file", "edit_file", "run_command"))


def good_a():
    return [step("write_file", path="a.py", content="A = 1\n"), step("finish", summary="a")]


def good_b():
    return [step("write_file", path="b.py", content="B = 2\n"), step("finish", summary="b")]


class TreeCase(KernelCase):
    def parent(self, tid="p1", subs=None, check_all=CHECK_ALL, budget=None, extra=None):
        ws = self.root / "ws" / tid
        ws.mkdir(parents=True)
        for name, text in {"check_a.py": CHECK_A, "check_b.py": CHECK_B, "check_all.py": check_all, **(extra or {})}.items():
            (ws / name).write_text(text, encoding="utf-8")
        return TaskContract(tid, "build a and b", ws,
                            Acceptance(f"{PY} check_all.py", 60, ("check_a.py", "check_b.py", "check_all.py")),
                            budget or Budget(max_rounds=10, max_model_calls=12, max_commands=4),
                            deliver_to=self.root / "out",
                            subtasks=tuple(subs or (sub("a", ["a.py"], "check_a.py"), sub("b", ["b.py"], "check_b.py"))))

    def run_tree(self, c, worker_steps, reviews, setup=None):
        k = Kernel(self.ledger, ScriptedModel(worker_steps), ScriptedModel(reviews), trust=setup)
        return k, k.run(c)

    def merges(self, tid):
        return [(e.kind, e.op_id) for e in self.ledger.events(tid) if e.payload.get("tool") == "merge_subtask"]


class ParseTest(unittest.TestCase):
    def data(self, **override):
        s = {"id": "a", "goal": "write a", "writes": ["a.py", "pkg/"], "acceptance": "{python} check_a.py",
             "acceptance_files": ["check_a.py"], "budget": {"max_rounds": 3, "max_model_calls": 4, "max_commands": 2}}
        s.update(override)
        return {"task": {"id": "p", "goal": "g", "workspace": "ws", "protected": ["keep.txt", "lib/keep.py"]},
                "acceptance": {"command": "{python} check_all.py", "files": ["check_all.py", "check_a.py"]},
                "budget": {"max_rounds": 5, "max_model_calls": 6, "max_commands": 3}, "subtasks": [s]}

    def parse(self, data):
        return parse_contract(data, Path("."))

    def test_valid_plan(self):
        c = self.parse(self.data())
        self.assertEqual(c.subtasks[0].writes, ("a.py", "pkg/"))
        self.assertEqual(c.subtasks[0].acceptance.files, ("check_a.py",))
        self.assertFalse(c.subtasks[0].network)

    def test_each_rule_rejects(self):
        cases = {
            "bad id": {"id": "A b"},
            "empty writes": {"writes": []},
            "dotdot": {"writes": ["../x.py"]},
            "absolute": {"writes": ["/etc/x"]},
            "windows absolute": {"writes": ["C:\\x.py"]},
            "protected file": {"writes": ["keep.txt"]},
            "acceptance file": {"writes": ["check_all.py"]},
            "folder covering a protected file": {"writes": ["lib/"]},
            "blank path": {"writes": [" "]},
            "dot path": {"writes": ["."]},
            "foreign acceptance file": {"acceptance_files": ["mine.py"], "acceptance": "{python} mine.py"},
            "command not using its files": {"acceptance": "{python} -c pass"},
            "budget over parent": {"budget": {"max_rounds": 6, "max_model_calls": 4, "max_commands": 2}},
            "budget missing key": {"budget": {"max_rounds": 3, "max_model_calls": 4}},
            "tools not parent's": {"tools": ["read_file", "fly"]},
            "network without parent": {"network": True},
        }
        for name, override in cases.items():
            with self.subTest(name), self.assertRaises(ContractError):
                self.parse(self.data(**override))

    def test_duplicate_id_and_sum_over_parent(self):
        d = self.data()
        d["subtasks"].append(dict(d["subtasks"][0]))
        with self.assertRaises(ContractError):
            self.parse(d)
        d["subtasks"][1]["id"] = "b"
        with self.assertRaisesRegex(ContractError, "add up"):
            self.parse(d)

    def test_readme_example_parses(self):
        import tomllib
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        data = tomllib.loads("[[subtasks]]" + readme.split("```toml\n[[subtasks]]", 1)[1].split("```", 1)[0])
        data.update({"task": {"id": "p", "goal": "g", "workspace": "ws"}, "budget": {"max_rounds": 9, "max_model_calls": 9,
                     "max_commands": 9}, "acceptance": {"command": "{python} -m unittest", "files": ["tests/test_parser.py"]}})
        self.assertEqual(self.parse(data).subtasks[0].id, "parser")

    def test_tokens_required_when_parent_sets_them(self):
        d = self.data()
        d["budget"]["max_tokens"] = 1000
        with self.assertRaises(ContractError):
            self.parse(d)


class TreeTest(TreeCase):
    def test_two_subtasks_pass_merge_and_deliver(self):
        c = self.parent()
        _, r = self.run_tree(c, good_a() + good_b(), [review("GO")] * 3)
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        self.assertTrue((r.delivered_to / "a.py").is_file() and (r.delivered_to / "b.py").is_file())
        self.assertEqual(self.merges("p1"), [("effect.intent", "merge-a"), ("effect.result", "merge-a"),
                                             ("effect.intent", "merge-b"), ("effect.result", "merge-b")])
        for child in ("p1.a", "p1.b"):
            self.assertEqual(self.ledger.events(child, "task.submitted")[0].payload["parent"], "p1")
        st = status(self.ledger, "p1")
        self.assertEqual([(s["id"], s["verdict"]) for s in st["subtasks"]], [("a", "PASS"), ("b", "PASS")])
        self.assertEqual(st["worker"]["status"], "FINISHED")
        self.assertEqual(r.usage["model_calls"], 7)   # parent usage includes both children

    def test_first_subtask_fails_and_stops(self):
        c = self.parent()
        _, r = self.run_tree(c, [step("write_file", path="a.py", content="A = 5\n"), step("finish")], [review("GO")])
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertIsNone(r.delivered_to)
        self.assertEqual(self.ledger.events("p1.b"), [])
        self.assertFalse((c.workspace / "a.py").exists())
        self.assertIn("SUBTASK_FAILED", " ".join(r.reasons))

    def test_child_unknown_makes_parent_unknown(self):
        c = self.parent()
        _, r = self.run_tree(c, good_a(), ["not json at all"])
        self.assertEqual(r.verdict.value, "UNKNOWN")
        self.assertIsNone(r.delivered_to)

    def test_write_outside_scope_is_denied(self):
        c = self.parent()
        steps = [step("write_file", path="b.py", content="B = 2\n")] + good_a() + good_b()
        _, r = self.run_tree(c, steps, [review("GO")] * 3)
        denied = [e.payload["gate"] for e in self.ledger.events("p1.a", "gate.denied")]
        self.assertEqual(denied, ["write_scope"])
        self.assertEqual(r.verdict.value, "PASS", r.reasons)

    def test_command_writing_outside_scope_fails_the_child_and_merges_nothing(self):
        c = self.parent()
        side = step("run_command", command=f"{PY} -c \"open('b.py', 'w').write('B = 2')\"")
        _, r = self.run_tree(c, [step("write_file", path="a.py", content="A = 1\n"), side, step("finish")], [])
        test = self.ledger.events("p1.a", "test.result")[0].payload
        self.assertEqual((test["verdict"], test["reason"]), ("FAIL", "OUT_OF_SCOPE"))
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertEqual(self.merges("p1"), [])
        self.assertFalse((c.workspace / "a.py").exists() or (c.workspace / "b.py").exists())

    def test_merge_deletes_inside_scope(self):
        c = self.parent(subs=(sub("a", ["a.py", "old.txt"], "check_a.py"), sub("b", ["b.py"], "check_b.py")),
                        extra={"old.txt": "remove me"})
        rm = step("run_command", command=f"{PY} -c \"import os; os.remove('old.txt')\"")
        _, r = self.run_tree(c, [rm] + good_a() + good_b(), [review("GO")] * 3)
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        self.assertFalse((c.workspace / "old.txt").exists())
        intent = self.ledger.events("p1", "effect.intent")[0].payload["args"]
        self.assertEqual((intent["changed"], intent["removed"]), (["a.py"], ["old.txt"]))

    def test_combined_acceptance_can_fail(self):
        c = self.parent(check_all=CHECK_ALL.replace("== 3", "== 4"))
        _, r = self.run_tree(c, good_a() + good_b(), [review("GO")] * 3)
        self.assertEqual([s["verdict"] for s in status(self.ledger, "p1")["subtasks"]], ["PASS", "PASS"])
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertIsNone(r.delivered_to)

    def test_children_using_the_whole_budget_leave_the_parent_review_unknown(self):
        subs = (sub("a", ["a.py"], "check_a.py", rounds=2, calls=3), sub("b", ["b.py"], "check_b.py", rounds=2, calls=3))
        c = self.parent(subs=subs, budget=Budget(max_rounds=4, max_model_calls=6, max_commands=4))
        _, r = self.run_tree(c, good_a() + good_b(), [review("GO")] * 3)
        self.assertEqual(r.verdict.value, "UNKNOWN")
        self.assertIsNone(r.delivered_to)
        self.assertIn("review not run", " ".join(r.reasons))

    def test_existing_subtask_workspace_is_refused_before_anything_is_written(self):
        c = self.parent()
        d = c.workspace.parent / "p1.subtasks" / "a"
        d.mkdir(parents=True)
        (d / "x.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(Exception):
            self.run_tree(c, good_a() + good_b(), [review("GO")] * 3)
        self.assertEqual(self.ledger.events("p1"), [])

    def test_kernel_rechecks_a_plan_built_in_code(self):
        from dataclasses import replace
        for bad in (replace(sub("a", ["a.py"], "check_a.py"), tools=("read_file", "fly")),
                    replace(sub("a", ["a.py"], "check_a.py"), network=True),
                    replace(sub("a", ["a.py"], "check_a.py"), writes=("check_all.py",)),
                    sub("a", ["a.py"], "check_a.py", calls=13)):
            c = self.parent(tid=f"p{id(bad)}", subs=(bad,))
            with self.subTest(bad), self.assertRaises(Exception) as ctx:
                self.run_tree(c, [], [])
            self.assertIn("subtask", str(ctx.exception))
            self.assertEqual(self.ledger.events(c.task_id), [])

    def test_task_without_subtasks_has_no_subtask_status(self):
        r = Kernel(self.ledger, ScriptedModel([step("write_file", path="calc.py", content="def add(a, b):\n    return a + b\n"),
                                               step("finish")]), ScriptedModel([review("GO")])).run(self.contract())
        self.assertEqual(r.verdict.value, "PASS")
        self.assertNotIn("subtasks", status(self.ledger, "t1"))


class TreeTrustTest(TreeCase):
    def setUp(self):
        super().setUp()
        caps = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"], "max_rounds": 10,
                "max_model_calls": 12, "max_tokens": 100000, "max_commands": 4, "network": False}
        self.setup, self.root_key = trust.dev_setup(self.root / "trust", caps)

    def trusted_parent(self):
        subs = (sub("a", ["a.py"], "check_a.py", tokens=50000), sub("b", ["b.py"], "check_b.py", tokens=50000))
        return self.parent(subs=subs, budget=Budget(max_rounds=10, max_model_calls=12, max_commands=4, max_tokens=100000))

    def test_children_act_under_the_parent_credential(self):
        _, r = self.run_tree(self.trusted_parent(), good_a() + good_b(), [review("GO")] * 3, self.setup)
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        parent = self.ledger.events("p1", "task.submitted")[0].payload["trust"]
        child = self.ledger.events("p1.a", "task.submitted")[0].payload["trust"]
        self.assertEqual(child["parent_credential_id"], parent["credential_id"])   # T22: each child has its own
        self.assertNotEqual(child["credential_id"], parent["credential_id"])
        self.assertEqual(len(self.ledger.events("p1", "credential.issued")), 1)
        self.assertEqual(len(self.ledger.events("p1.a", "credential.issued")), 1)

    def test_revoking_the_parent_stops_the_next_subtask(self):
        holder = {}

        def revoke_then_write(messages):
            self.revoke(holder["k"])
            return step("write_file", path="b.py", content="B = 2\n")
        k = Kernel(self.ledger, ScriptedModel(good_a() + [revoke_then_write, step("finish")]),
                   ScriptedModel([review("GO")] * 3), trust=self.setup)
        holder["k"] = k
        r = k.run(self.trusted_parent())
        self.assertEqual([e.payload["gate"] for e in self.ledger.events("p1.b", "gate.denied")], ["trust_chain"])
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertIsNone(r.delivered_to)
        self.assertFalse((self.trusted_parent_ws() / "b.py").exists())

    def test_revocation_during_the_child_review_blocks_the_merge(self):
        holder, go = {}, review("GO")

        def revoke_then_go(messages):   # the child's last step is done; the owner revokes before the merge
            self.revoke(holder["k"])
            return go(messages)
        k = Kernel(self.ledger, ScriptedModel(good_a() + good_b()), ScriptedModel([revoke_then_go, go, go]), trust=self.setup)
        holder["k"] = k
        r = k.run(self.trusted_parent())
        self.assertEqual(self.ledger.events("p1.a", "verdict")[0].payload["verdict"], "PASS")
        self.assertEqual(self.merges("p1"), [])
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertFalse((self.trusted_parent_ws() / "a.py").exists())
        self.assertEqual(self.ledger.events("p1.b"), [])

    def revoke(self, k):
        self.setup.revocations_path.write_bytes(trust.canonical(trust.sign({
            "schema": trust.REVOCATIONS, "trust_domain": "TEST_ONLY", "version": 1, "revoked_charters": [],
            "revoked_credentials": [k.credential["body"]["credential_id"]], "issued_at": int(time.time())}, self.root_key)))

    def trusted_parent_ws(self):
        return self.root / "ws" / "p1"


if __name__ == "__main__":
    unittest.main()
