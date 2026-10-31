from dataclasses import replace
from unittest import mock

from m2k import trust
from m2k.contract import Budget
from m2k.kernel import Kernel, KernelError, status
from m2k.models import ScriptedModel
from tests.helpers import GOOD_CODE, KernelCase, review, step

CAPS = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"], "max_rounds": 20,
        "max_model_calls": 30, "max_tokens": 100000, "max_commands": 30, "network": False}
BUDGET = Budget(max_rounds=10, max_model_calls=12, max_tokens=50000, max_commands=5)


class TrustKernelTest(KernelCase):
    def setUp(self):
        super().setUp()
        self.setup, self.root_key = trust.dev_setup(self.root / "keys", CAPS)

    def revoke(self, credentials=(), charters=(), version=1):
        body = {"schema": trust.REVOCATIONS, "trust_domain": "TEST_ONLY", "version": version,
                "revoked_charters": list(charters), "revoked_credentials": list(credentials), "issued_at": 1}
        self.setup.revocations_path.write_bytes(trust.canonical(trust.sign(body, self.root_key)))

    def kernel(self, worker_steps, reviewer_steps=(), setup="default"):
        self.worker, self.reviewer = ScriptedModel(list(worker_steps), "w"), ScriptedModel(list(reviewer_steps), "r")
        self.k = Kernel(self.ledger, self.worker, self.reviewer, trust=self.setup if setup == "default" else setup)
        return self.k

    def credential_id(self):
        return self.k.credential["body"]["credential_id"]

    def test_valid_chain_passes_and_is_recorded(self):
        c = self.contract(budget=BUDGET)
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")], [review("GO")]).run(c)
        self.assertEqual(r.verdict.value, "PASS")
        self.assertIsNotNone(r.delivered_to)
        rec = self.ledger.events("t1", "task.submitted")[0].payload["trust"]
        self.assertEqual((rec["domain"], rec["credential_id"]), ("TEST_ONLY", self.credential_id()))
        self.assertEqual(self.ledger.events("t1", "credential.issued")[0].payload["credential"], self.k.credential)
        self.assertEqual(status(self.ledger, "t1")["trust"]["charter_id"], self.setup.charter_env["body"]["charter_id"])

    def test_issuer_key_must_match_the_charter(self):
        other, _ = trust.generate_keypair()
        pem = self.root / "keys" / "other.pem"
        pem.write_bytes(other.private_bytes(trust.serialization.Encoding.PEM, trust.serialization.PrivateFormat.PKCS8,
                                            trust.serialization.NoEncryption()))
        with self.assertRaisesRegex(KernelError, "ISSUER_KEY"):
            self.kernel([], setup=replace(self.setup, issuer_key_path=pem)).run(self.contract(budget=BUDGET))
        self.assertEqual(self.ledger.events(), [])

    def test_revoked_charter_refuses_to_start(self):
        self.revoke(charters=[self.setup.charter_env["body"]["charter_id"]])
        with self.assertRaisesRegex(KernelError, "CHARTER_REVOKED"):
            self.kernel([]).run(self.contract(budget=BUDGET))
        self.assertEqual(self.ledger.events(), [])

    def test_contract_beyond_the_charter_refuses_to_start(self):
        with self.assertRaisesRegex(KernelError, "EXCEEDS_CHARTER"):
            self.kernel([]).run(self.contract())          # default budget has no token limit: never fits a charter
        self.assertEqual(self.ledger.events(), [])

    def assert_stopped_by_trust(self, r, code):
        denied = [e.payload for e in self.ledger.events("t1", "gate.denied")]
        self.assertEqual([d["gate"] for d in denied], ["trust_chain"])
        self.assertIn(code, denied[0]["reason"])
        self.assertEqual(r.worker.status.value, "TRUST_BROKEN")
        self.assertNotEqual(r.verdict.value, "PASS")
        self.assertIsNone(r.delivered_to)
        self.assertFalse((self.root / "ws" / "t1" / "second.txt").exists())

    def test_credential_revoked_mid_run_stops_every_further_action(self):
        def revoke_then_write(messages):
            self.revoke(credentials=[self.credential_id()])
            return step("write_file", path="second.txt", content="x")
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), revoke_then_write, step("finish")],
                        [review("GO")]).run(self.contract(budget=BUDGET))
        self.assert_stopped_by_trust(r, "CREDENTIAL_REVOKED")
        self.assertEqual(len(self.worker.steps), 1)   # finish was never asked for: the worker stopped

    def test_expired_credential_stops_the_worker(self):
        k = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE),
                         lambda m: (setattr(k, "clock", lambda: start + 10_000), step("write_file", path="second.txt",
                                                                                      content="x"))[1],
                         step("finish")], [review("GO")])
        start = k.clock()
        self.assert_stopped_by_trust(k.run(self.contract(budget=BUDGET)), "EXPIRED")

    def test_charter_replaced_mid_run_is_denied(self):
        other = trust.sign(self.setup.charter_env["body"] | {"charter_id": "another"}, self.root_key)

        def swap(messages):
            self.k.trust = replace(self.k.trust, charter_env=other)
            return step("write_file", path="second.txt", content="x")
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), swap, step("finish")],
                        [review("GO")]).run(self.contract(budget=BUDGET))
        self.assert_stopped_by_trust(r, "WRONG_CHARTER")

    def test_revocation_after_finish_blocks_test_and_delivery(self):
        def revoke_then_finish(messages):
            self.revoke(credentials=[self.credential_id()])
            return step("finish")
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), revoke_then_finish],
                        [review("GO")]).run(self.contract(budget=BUDGET))
        self.assertEqual((r.verdict.value, r.test.reason), ("FAIL", "TRUST_CREDENTIAL_REVOKED"))
        self.assertIsNone(r.delivered_to)
        self.assertEqual(self.reviewer.calls, [])        # no review spent on a task the chain no longer vouches for

    def test_revocation_between_verdict_and_delivery_blocks_delivery(self):
        k = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")], [review("GO")])
        real = k._audit

        def audit_then_revoke(*a, **kw):
            result = real(*a, **kw)
            self.revoke(credentials=[self.credential_id()])
            return result
        with mock.patch.object(k, "_audit", audit_then_revoke):
            r = k.run(self.contract(budget=BUDGET))
        self.assertEqual(r.verdict.value, "PASS")
        self.assertIsNone(r.delivered_to)
        refused = self.ledger.events("t1", "delivery.refused")[0].payload["reason"]
        self.assertIn("CREDENTIAL_REVOKED", refused)

    def test_without_trust_nothing_changes(self):
        c = self.contract()
        r = Kernel(self.ledger, ScriptedModel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")]),
                   ScriptedModel([review("GO")])).run(c)
        self.assertEqual(r.verdict.value, "PASS")
        self.assertIsNone(self.ledger.events("t1", "task.submitted")[0].payload["trust"])
        self.assertEqual(self.ledger.events("t1", "credential.issued"), [])
