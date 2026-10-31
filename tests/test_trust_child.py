"""T22: each subtask gets its own child credential, never wider than its parent, its plan or its own contract."""
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from m2k import trust
from m2k.contract import Acceptance, Budget, Subtask, TaskContract
from m2k.kernel import Kernel, status
from m2k.models import ScriptedModel
from tests.helpers import review, step
from tests.test_task_tree import TreeCase, good_a, good_b, sub

TOOLS = ("list_files", "read_file", "write_file", "edit_file", "run_command")
CAPS = {"tools": list(TOOLS), "max_rounds": 10, "max_model_calls": 12, "max_tokens": 100000, "max_commands": 4,
        "network": False}
CODE = Path(trust.__file__).resolve().parent
NOW = 1_800_000_000


class ChildLibraryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.setup, self.root_key = trust.dev_setup(Path(self._tmp.name) / "keys", CAPS)
        plan = Subtask("a", "write a", ("a.py",), Acceptance("{python} check_a.py", 60, ("check_a.py",)),
                       Budget(max_rounds=3, max_model_calls=4, max_commands=2, max_tokens=40000), ("read_file", "write_file"))
        self.parent = TaskContract("p", "goal", Path(self._tmp.name) / "ws", Acceptance("{python} check_a.py", 60, ("check_a.py",)),
                                   Budget(max_rounds=10, max_model_calls=12, max_commands=4, max_tokens=100000), TOOLS,
                                   subtasks=(plan,))
        self.child = TaskContract("p.a", "write a", Path(self._tmp.name) / "ws.subtasks" / "a", plan.acceptance, plan.budget,
                                  plan.tools, writes=plan.writes)
        charter, _ = trust.verify_charter(self.setup.pin, "TEST_ONLY", self.setup.charter_env, None, CODE)
        self.parent_env = trust.issue_credential(charter, self.setup.issuer_key(), self.parent, NOW, 600)
        self.child_env = trust.issue_child_credential(self.setup.issuer_key(), self.parent_env["body"], self.parent,
                                                      self.child, "a", NOW)

    def tearDown(self):
        self._tmp.cleanup()

    def verify(self, child_env=None, revocations=None, contract=None, now=NOW + 1, parent=None):
        return trust.verify_child_chain(self.setup.pin, "TEST_ONLY", self.setup.charter_env, self.parent_env,
                                        child_env or self.child_env, revocations, parent or self.parent,
                                        contract or self.child, now, CODE)

    def resign(self, key=None, **changes):
        return trust.sign(dict(self.child_env["body"]) | changes, key or self.setup.issuer_key())

    def revocations(self, *ids):
        return trust.sign({"schema": trust.REVOCATIONS, "trust_domain": "TEST_ONLY", "version": 1, "revoked_charters": [],
                           "revoked_credentials": list(ids), "issued_at": NOW}, self.root_key)

    def code(self, **kw):
        with self.assertRaises(trust.TrustError) as ctx:
            self.verify(**kw)
        return ctx.exception.code

    def test_valid_child_verifies(self):
        body = self.verify()
        self.assertEqual((body["task_id"], body["subtask"], body["writes"]), ("p.a", "a", ["a.py"]))
        self.assertEqual(body["parent_credential_id"], self.parent_env["body"]["credential_id"])
        self.assertEqual(body["expires_at"], self.parent_env["body"]["expires_at"])

    def test_every_code(self):
        other_key, _ = trust.generate_keypair()
        b = self.child_env["body"]
        parent_exp = self.parent_env["body"]["expires_at"]
        cases = {
            "CREDENTIAL_SIGNATURE": {"child_env": self.resign(other_key)},
            "CREDENTIAL_REVOKED": {"revocations": self.revocations(b["credential_id"])},
            "PARENT_CREDENTIAL_REVOKED": {"revocations": self.revocations(self.parent_env["body"]["credential_id"])},
            "PARENT_EXPIRED": {"now": parent_exp},
            "NOT_YET_VALID": {"child_env": self.resign(not_before=NOW + 5), "now": NOW + 2},
            "PARENT_NOT_YET_VALID": {"now": NOW - 1},
            "EXPIRED": {"child_env": self.resign(expires_at=NOW + 5), "now": NOW + 10},
            "WRONG_TASK": {"child_env": self.resign(task_id="p.b")},
            "CONTRACT_MISMATCH": {"contract": replace(self.child, goal="something else")},
            "EXCEEDS_CREDENTIAL": {"child_env": self.resign(tools=["read_file"])},
        }
        wider = {"WRONG_PARENT": [{"parent_credential_id": "another"}, {"subtask": "zz"}, {"charter_id": "another"}],
                 "EXCEEDS_PARENT": [{"tools": ["read_file", "write_file", "fly"]}, {"network": True},
                                    {"budget": dict(b["budget"], max_commands=5)}, {"expires_at": parent_exp + 1},
                                    {"writes": []}],
                 "EXCEEDS_PLAN": [{"tools": list(TOOLS)}, {"budget": dict(b["budget"], max_commands=3)},
                                  {"writes": ["b.py"]}, {"writes": ["a.py", "b.py"]}]}
        for code, kw in cases.items():
            with self.subTest(code):
                self.assertEqual(self.code(**kw), code)
        for code, changes in wider.items():
            for change in changes:
                with self.subTest(code, change=change):
                    self.assertEqual(self.code(child_env=self.resign(**change)), code)

    def test_malformed_child_is_format(self):
        body = dict(self.child_env["body"])
        del body["writes"]
        self.assertEqual(self.code(child_env=trust.sign(body, self.setup.issuer_key())), "FORMAT")

    def test_child_credential_never_stands_alone(self):
        with self.assertRaises(trust.TrustError) as ctx:
            trust.verify_chain(self.setup.pin, "TEST_ONLY", self.setup.charter_env, self.child_env, None, self.child,
                               NOW + 1, CODE)
        self.assertEqual(ctx.exception.code, "WRONG_PARENT")

    def test_issuer_refuses_anything_wider(self):
        cases = {"EXCEEDS_PLAN": replace(self.child, tools=TOOLS),
                 "EXCEEDS_PARENT": replace(self.child, tools=("read_file", "fly")),
                 "WRONG_TASK": replace(self.child, task_id="p.zz")}
        for code, contract in cases.items():
            with self.subTest(code), self.assertRaises(trust.TrustError) as ctx:
                trust.issue_child_credential(self.setup.issuer_key(), self.parent_env["body"], self.parent, contract, "a", NOW)
            self.assertEqual(ctx.exception.code, code)


class ChildKernelTest(TreeCase):
    def setUp(self):
        super().setUp()
        self.setup, self.root_key = trust.dev_setup(self.root / "keys", CAPS)

    def tree(self):
        subs = (sub("a", ["a.py"], "check_a.py", tokens=50000), sub("b", ["b.py"], "check_b.py", tokens=50000))
        return self.parent(subs=subs, budget=Budget(max_rounds=10, max_model_calls=12, max_commands=4, max_tokens=100000))

    def kernel(self, worker_steps, reviews):
        self.k = Kernel(self.ledger, ScriptedModel(worker_steps, "w"), ScriptedModel(reviews, "r"), trust=self.setup)
        return self.k

    def revoke(self, *ids):
        self.setup.revocations_path.write_bytes(trust.canonical(trust.sign({
            "schema": trust.REVOCATIONS, "trust_domain": "TEST_ONLY", "version": 1, "revoked_charters": [],
            "revoked_credentials": list(ids), "issued_at": int(time.time())}, self.root_key)))

    def child_id(self, tid):
        return self.k.child_credentials[tid]["body"]["credential_id"]

    def denied(self, tid):
        return [e.payload["reason"].split(":", 1)[0] for e in self.ledger.events(tid, "gate.denied")]

    def test_each_child_has_its_own_credential(self):
        r = self.kernel(good_a() + good_b(), [review("GO")] * 3).run(self.tree())
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        parent = self.ledger.events("p1", "task.submitted")[0].payload["trust"]
        ids = set()
        for child in ("p1.a", "p1.b"):
            rec = self.ledger.events(child, "task.submitted")[0].payload["trust"]
            issued = self.ledger.events(child, "credential.issued")[0].payload["credential"]
            self.assertEqual(rec["parent_credential_id"], parent["credential_id"])
            self.assertEqual(rec["credential_id"], issued["body"]["credential_id"])
            ids.add(rec["credential_id"])
        self.assertEqual(len(ids | {parent["credential_id"]}), 3)

    def test_revoking_one_child_stops_only_that_child(self):
        def revoke_a(messages):
            self.revoke(self.child_id("p1.a"))
            return step("write_file", path="a.py", content="A = 1\n")
        r = self.kernel([step("list_files", path="."), revoke_a, step("finish")], []).run(self.tree())
        self.assertEqual(self.denied("p1.a"), ["CREDENTIAL_REVOKED"])
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertEqual(self.merges("p1"), [])
        self.assertIsNone(r.delivered_to)

    def test_revoking_the_parent_stops_the_running_child(self):
        def revoke_parent(messages):
            self.revoke(self.k.credential["body"]["credential_id"])
            return step("write_file", path="a.py", content="A = 1\n")
        r = self.kernel([revoke_parent, step("finish")], []).run(self.tree())
        self.assertEqual(self.denied("p1.a"), ["PARENT_CREDENTIAL_REVOKED"])
        self.assertEqual(r.verdict.value, "FAIL")

    def test_a_widened_child_credential_is_refused(self):
        def widen(messages):
            body = self.k.child_credentials["p1.a"]["body"]
            self.k.child_credentials["p1.a"] = trust.sign(dict(body, budget=dict(body["budget"], max_commands=9)),
                                                          self.setup.issuer_key())
            return step("write_file", path="a.py", content="A = 1\n")
        r = self.kernel([widen, step("finish")], []).run(self.tree())
        self.assertIn(self.denied("p1.a")[0], ("EXCEEDS_PARENT", "EXCEEDS_PLAN"))
        self.assertEqual(r.verdict.value, "FAIL")

    def test_revoking_a_child_after_its_last_action_blocks_the_merge(self):
        go = review("GO")

        def revoke_then_go(messages):
            self.revoke(self.child_id("p1.a"))
            return go(messages)
        r = self.kernel(good_a() + good_b(), [revoke_then_go, go, go]).run(self.tree())
        self.assertEqual(self.ledger.events("p1.a", "verdict")[0].payload["verdict"], "PASS")
        self.assertEqual(self.merges("p1"), [])
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertIn("CREDENTIAL_REVOKED", status(self.ledger, "p1")["worker"]["reason"])

    def test_a_child_credential_that_cannot_be_issued_fails_the_subtask(self):
        with mock.patch.object(trust, "issue_child_credential", side_effect=trust.TrustError("EXCEEDS_PLAN", "no")):
            r = self.kernel(good_a() + good_b(), [review("GO")] * 3).run(self.tree())
        self.assertEqual(r.verdict.value, "FAIL")
        st = status(self.ledger, "p1")
        self.assertEqual(st["worker"]["status"], "SUBTASK_FAILED")
        self.assertIn("EXCEEDS_PLAN", st["worker"]["reason"])
        self.assertEqual(self.ledger.events("p1.a"), [])


if __name__ == "__main__":
    unittest.main()
