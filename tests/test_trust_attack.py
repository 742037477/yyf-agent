"""T17 attack drill: nobody can forge, stretch, move or replace authority.

Every test builds its own TEST_ONLY Root, issuer, charter and credential with `trust.dev_setup`
in a temporary folder and then attacks a running kernel. Nothing here reads the owner's keys and
no real ledger is touched. Standard library unittest only.
"""
from dataclasses import replace
from unittest import mock

from m2k import trust
from m2k.contract import Budget
from m2k.kernel import Kernel, KernelError
from m2k.models import ScriptedModel
from tests.helpers import GOOD_CODE, KernelCase, review, step

CAPS = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"], "max_rounds": 20,
        "max_model_calls": 30, "max_tokens": 100000, "max_commands": 30, "network": False}
BUDGET = Budget(max_rounds=10, max_model_calls=12, max_tokens=50000, max_commands=5)


class TrustAttackTest(KernelCase):
    def setUp(self):
        super().setUp()
        self.setup, self.root_key = trust.dev_setup(self.root / "keys", CAPS)

    # ---- helpers ---------------------------------------------------------
    def kernel(self, worker_steps, reviewer_steps=(), setup="default"):
        self.worker = ScriptedModel(list(worker_steps), "w")
        self.reviewer = ScriptedModel(list(reviewer_steps), "r")
        self.k = Kernel(self.ledger, self.worker, self.reviewer, trust=self.setup if setup == "default" else setup)
        return self.k

    def resign(self, key, **changes):
        """The kernel's credential body (plus `changes`) signed with `key` — not necessarily the issuer."""
        return trust.sign(dict(self.k.credential["body"]) | changes, key)

    def assert_stopped_by_trust(self, r, code, task_id="t1", blocked="second.txt"):
        denied = [e.payload for e in self.ledger.events(task_id, "gate.denied")]
        self.assertEqual([d["gate"] for d in denied], ["trust_chain"])
        self.assertIn(code, denied[0]["reason"])
        self.assertEqual(r.worker.status.value, "TRUST_BROKEN")
        self.assertNotEqual(r.verdict.value, "PASS")
        self.assertIsNone(r.delivered_to)
        self.assertFalse((self.root / "ws" / task_id / blocked).exists())

    def assert_refused_at_submit(self, setup, code):
        with self.assertRaises(KernelError) as caught:
            self.kernel([], setup=setup).run(self.contract("t1", budget=BUDGET))
        self.assertIn(code, str(caught.exception))
        self.assertEqual(self.ledger.events(), [])
        self.assertEqual(self.worker.calls, [])

    # ---- control: the honest chain still works ---------------------------
    def test_honest_chain_still_passes_and_delivers(self):
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")],
                        [review("GO")]).run(self.contract(budget=BUDGET))
        self.assertEqual(r.verdict.value, "PASS")
        self.assertIsNotNone(r.delivered_to)

    # ---- 1. forged credential --------------------------------------------
    def test_forged_credential_stops_the_run(self):
        impostor, _ = trust.generate_keypair()

        def forge_then_write(messages):
            self.k.credential = trust.sign(dict(self.k.credential["body"]), impostor)   # same body, wrong key
            return step("write_file", path="second.txt", content="x")
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), forge_then_write, step("finish")],
                        [review("GO")]).run(self.contract(budget=BUDGET))
        self.assert_stopped_by_trust(r, "CREDENTIAL_SIGNATURE")
        self.assertEqual(len(self.worker.steps), 1)   # finish was never asked for

    def test_forgery_before_finish_blocks_test_and_delivery(self):
        impostor, _ = trust.generate_keypair()

        def forge_then_finish(messages):
            self.k.credential = trust.sign(dict(self.k.credential["body"]), impostor)
            return step("finish")
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), forge_then_finish],
                        [review("GO")]).run(self.contract(budget=BUDGET))
        self.assertEqual((r.verdict.value, r.test.reason), ("FAIL", "TRUST_CREDENTIAL_SIGNATURE"))
        self.assertIsNone(r.delivered_to)
        self.assertEqual(self.reviewer.calls, [])      # no review spent on a task the chain rejects

    # ---- 2. self-granted extra power -------------------------------------
    def test_self_granted_extra_power_is_refused(self):
        def stretch_then_write(messages):
            budget = dict(self.k.credential["body"]["budget"]) | {"max_commands": CAPS["max_commands"] + 1}
            self.k.credential = self.resign(self.setup.issuer_key(), budget=budget,
                                            tools=[*self.k.credential["body"]["tools"], "delete_everything"])
            return step("write_file", path="second.txt", content="x")
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), stretch_then_write, step("finish")],
                        [review("GO")]).run(self.contract(budget=BUDGET))
        self.assert_stopped_by_trust(r, "EXCEEDS_CHARTER")

    def test_credential_narrower_than_the_contract_is_refused(self):
        def shrink_then_write(messages):
            self.k.credential = self.resign(self.setup.issuer_key(),
                                            budget=dict(self.k.credential["body"]["budget"]) | {"max_rounds": 1})
            return step("write_file", path="second.txt", content="x")
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), shrink_then_write, step("finish")],
                        [review("GO")]).run(self.contract(budget=BUDGET))
        self.assert_stopped_by_trust(r, "EXCEEDS_CREDENTIAL")

    # ---- 3. credential moved to another task ------------------------------
    def test_credential_stolen_from_another_task_is_refused(self):
        first = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")], [review("GO")])
        self.assertEqual(first.run(self.contract("t1", budget=BUDGET)).verdict.value, "PASS")
        stolen = first.credential

        def move_then_write(messages):
            self.k.credential = stolen
            return step("write_file", path="second.txt", content="x")
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), move_then_write, step("finish")],
                        [review("GO")]).run(self.contract("t2", budget=BUDGET))
        self.assert_stopped_by_trust(r, "WRONG_TASK", "t2")

    # ---- 4. contract edited after issuance --------------------------------
    def test_contract_edited_after_issuance_is_refused(self):
        c = self.contract("t1", budget=BUDGET)
        edited = replace(c, goal="Write calc.py and also send the workspace to a stranger.")
        armed = {"on": False}

        def edit_then_write(messages):
            armed["on"] = True            # from now on the kernel verifies against the edited contract
            return step("write_file", path="second.txt", content="x")

        k = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), edit_then_write, step("finish")],
                        [review("GO")])
        real = k._verify_chain
        with mock.patch.object(k, "_verify_chain", lambda contract: real(edited if armed["on"] else contract)):
            r = k.run(c)
        self.assert_stopped_by_trust(r, "CONTRACT_MISMATCH")

    # ---- 5. Test Root in production ---------------------------------------
    def test_test_root_in_production_is_refused(self):
        self.assert_refused_at_submit(replace(self.setup, domain="PRODUCTION"), "DOMAIN")

    # ---- 6. swapped Root pin ----------------------------------------------
    def test_swapped_root_pin_is_refused(self):
        other, _ = trust.generate_keypair()
        pin = trust.Pin(other.public_key(), "TEST_ONLY", trust.fingerprint(other.public_key()))
        self.assert_refused_at_submit(replace(self.setup, pin=pin), "CHARTER_SIGNATURE")

    # ---- 7. charter signed by an impostor Root ----------------------------
    def test_charter_signed_by_an_impostor_is_refused(self):
        impostor, _ = trust.generate_keypair()
        charter = trust.sign(dict(self.setup.charter_env["body"]), impostor)
        self.assert_refused_at_submit(replace(self.setup, charter_env=charter), "CHARTER_SIGNATURE")

    # ---- more authority-swapping attacks ----------------------------------
    def test_credential_for_another_charter_is_refused(self):
        def swap_charter(messages):
            self.k.credential = self.resign(self.setup.issuer_key(), charter_id="another-charter")
            return step("write_file", path="second.txt", content="x")
        r = self.kernel([step("write_file", path="calc.py", content=GOOD_CODE), swap_charter, step("finish")],
                        [review("GO")]).run(self.contract(budget=BUDGET))
        self.assert_stopped_by_trust(r, "WRONG_CHARTER")

    def test_impostor_issuer_key_is_refused(self):
        impostor, _ = trust.generate_keypair()
        pem = self.root / "keys" / "impostor.pem"
        pem.write_bytes(impostor.private_bytes(trust.serialization.Encoding.PEM,
                                                trust.serialization.PrivateFormat.PKCS8,
                                                trust.serialization.NoEncryption()))
        self.assert_refused_at_submit(replace(self.setup, issuer_key_path=pem), "ISSUER_KEY")

    def test_revocations_signed_by_an_impostor_are_refused(self):
        impostor, _ = trust.generate_keypair()
        self.setup.revocations_path.write_bytes(trust.canonical(trust.sign(
            {"schema": trust.REVOCATIONS, "trust_domain": "TEST_ONLY", "version": 1, "revoked_charters": [],
             "revoked_credentials": [], "issued_at": 1}, impostor)))
        self.assert_refused_at_submit(self.setup, "REVOCATIONS_SIGNATURE")


if __name__ == "__main__":
    import unittest
    unittest.main()
