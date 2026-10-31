import base64
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from m2k import trust
from m2k.contract import Acceptance, Budget, TaskContract
from m2k.trust import TrustError

NOW = 1_800_000_000
CODE = Path(__file__).resolve().parent.parent / "m2k"


def contract(task_id="t1", **kw) -> TaskContract:
    base = dict(budget=Budget(max_rounds=5, max_model_calls=8, max_tokens=1000, max_commands=3),
                tools=("read_file", "write_file"))
    return TaskContract(task_id, "goal", Path("/ws"), Acceptance("{python} c.py", files=("c.py",)), **(base | kw))


class Chain(unittest.TestCase):
    """A fresh Root, issuer and charter per test; nothing from the owner's computer."""

    def setUp(self, domain="TEST_ONLY"):
        self.domain = domain
        self.root, root_b64 = trust.generate_keypair()
        self.issuer, issuer_b64 = trust.generate_keypair()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.pin_path = self.dir / "root-public-pin.json"
        self.pin_path.write_text(json.dumps({"public_key_b64": root_b64, "trust_domain": domain}), encoding="utf-8")
        self.pin = trust.load_pin(self.pin_path)
        self.caps = {"tools": ["read_file", "write_file", "run_command"], "max_rounds": 10, "max_model_calls": 20,
                     "max_tokens": 5000, "max_commands": 5, "network": False}
        self.charter_body = {"schema": trust.CHARTER, "trust_domain": domain, "charter_id": "ch-1",
                             "issuer_public_key_b64": issuer_b64, "release": "dev", "caps": self.caps,
                             "issued_at": NOW}
        self.charter = trust.sign(self.charter_body, self.root)
        self.c = contract()
        self.cred = trust.issue_credential(self.charter_body, self.issuer, self.c, NOW, 600)

    def revocations(self, charters=(), credentials=(), key=None):
        return trust.sign({"schema": trust.REVOCATIONS, "trust_domain": self.domain, "version": 1,
                           "revoked_charters": list(charters), "revoked_credentials": list(credentials),
                           "issued_at": NOW}, key or self.root)

    def verify(self, charter=None, cred=None, revocations=None, c=None, now=NOW + 1, domain=None, pin=None):
        return trust.verify_chain(pin or self.pin, domain or self.domain, charter or self.charter, cred or self.cred,
                                  revocations, c or self.c, now, CODE)

    def assertCode(self, code, **kw):
        with self.assertRaises(TrustError) as e:
            self.verify(**kw)
        self.assertEqual(e.exception.code, code, str(e.exception))

    def resign_credential(self, **changes):
        body = dict(self.cred["body"]) | changes
        return trust.sign(body, self.issuer)


class FormatTest(unittest.TestCase):
    def test_canonical_is_sorted_compact_utf8(self):
        self.assertEqual(trust.canonical({"b": 1, "a": "中"}), '{"a":"中","b":1}'.encode("utf-8"))

    def test_strict_json_rejects_float_duplicates_bom_nan(self):
        for text in ('{"a": 1.5}', '{"a": 1, "a": 2}', '﻿{"a": 1}', '{"a": NaN}'):
            with self.subTest(text=text), self.assertRaises(TrustError):
                trust.loads_strict(text)
        with self.assertRaises(TrustError):
            trust.canonical({"a": 0.5})

    def test_envelope_round_trip_and_tamper(self):
        key, _ = trust.generate_keypair()
        env = trust.sign({"schema": "x/1", "n": 1}, key)
        self.assertEqual(trust.open_envelope(env, key.public_key()), {"schema": "x/1", "n": 1})
        tampered = json.loads(json.dumps(env))
        tampered["body"]["n"] = 2
        other, _ = trust.generate_keypair()
        for bad, pub in ((tampered, key.public_key()), (env, other.public_key()),
                         (env | {"extra": 1}, key.public_key()), (env | {"schema": "y/1"}, key.public_key())):
            with self.assertRaises(TrustError):
                trust.open_envelope(bad, pub)

    def test_old_project_pin_with_full_sha256_fingerprint(self):
        """The old TEST_ONLY Root pin (M2_CONSTRUCTION_PIN_1) stores the full 64-hex sha256 as "fingerprint"."""
        key, b64 = trust.generate_keypair()
        raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        full = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pin_path.json"
            for value, ok in ((full, True), (full[:16], True), (full[:20], False), ("0" * 64, False)):
                p.write_text(json.dumps({"fingerprint": value, "public_key_b64": b64, "schema": "M2_CONSTRUCTION_PIN_1",
                                         "trust_domain": "TEST_ONLY"}), encoding="utf-8")
                with self.subTest(fingerprint=value):
                    if ok:
                        self.assertEqual(trust.load_pin(p).fingerprint, full[:16])
                    else:
                        with self.assertRaises(TrustError):
                            trust.load_pin(p)

    def test_old_project_pin_format_and_fingerprint_field(self):
        key, b64 = trust.generate_keypair()
        fp = trust.fingerprint(key.public_key())
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pin.json"
            p.write_text(json.dumps({"schema": "m2.root_pin.v1", "trust_domain": "TEST_ONLY", "public_key_b64": b64,
                                     "fingerprint": fp}), encoding="utf-8")
            pin = trust.load_pin(p)
            self.assertEqual((pin.trust_domain, pin.fingerprint), ("TEST_ONLY", fp))
            p.write_text(json.dumps({"trust_domain": "TEST_ONLY", "public_key_b64": b64, "fingerprint": "0" * 16}),
                         encoding="utf-8")
            with self.assertRaises(TrustError):
                trust.load_pin(p)
            full = hashlib.sha256(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                                 serialization.PublicFormat.Raw)).hexdigest()
            for value, ok in ((full, True), ("f" * 64, False)):
                p.write_text(json.dumps({"trust_domain": "TEST_ONLY", "public_key_b64": b64, "public_key_sha256": value}),
                             encoding="utf-8")
                with self.subTest(public_key_sha256=value):
                    if ok:
                        self.assertEqual(trust.load_pin(p).fingerprint, fp)
                    else:
                        with self.assertRaises(TrustError) as e:
                            trust.load_pin(p)
                        self.assertEqual(e.exception.code, "ROOT_PIN")
            p.write_text(json.dumps({"trust_domain": "TEST_ONLY", "public_key_b64": base64.b64encode(b"x" * 31).decode()}),
                         encoding="utf-8")
            with self.assertRaises(TrustError):
                trust.load_pin(p)

    def test_private_key_pem_round_trip(self):
        key, b64 = trust.generate_keypair()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "k.pem"
            p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
            self.assertEqual(trust.public_key_b64(trust.load_private_key(p)), b64)

    def test_release_digest_changes_with_code(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.py").write_text("x = 1\n", encoding="utf-8")
            first = trust.release_digest(d)
            (Path(d) / "a.py").write_text("x = 2\n", encoding="utf-8")
            self.assertNotEqual(first, trust.release_digest(d))


class ChainTest(Chain):
    def test_valid_chain_returns_the_credential(self):
        body = self.verify()
        self.assertEqual((body["task_id"], body["contract_sha256"]), ("t1", self.c.digest()))
        self.assertEqual(self.verify(revocations=self.revocations())["credential_id"], body["credential_id"])

    def test_domain_test_root_refused_in_production(self):
        self.assertCode("DOMAIN", domain="PRODUCTION")

    def test_charter_signed_by_another_key(self):
        other, _ = trust.generate_keypair()
        self.assertCode("CHARTER_SIGNATURE", charter=trust.sign(self.charter_body, other))

    def test_revocations_must_be_signed_by_root(self):
        other, _ = trust.generate_keypair()
        self.assertCode("REVOCATIONS_SIGNATURE", revocations=self.revocations(key=other))

    def test_revoked_charter_and_credential(self):
        self.assertCode("CHARTER_REVOKED", revocations=self.revocations(charters=["ch-1"]))
        self.assertCode("CREDENTIAL_REVOKED", revocations=self.revocations(credentials=[self.cred["body"]["credential_id"]]))

    def test_release_must_match_code(self):
        self.assertCode("RELEASE", charter=trust.sign(self.charter_body | {"release": "0" * 64}, self.root))
        good = trust.sign(self.charter_body | {"release": trust.release_digest(CODE)}, self.root)
        self.assertEqual(self.verify(charter=good)["task_id"], "t1")

    def test_credential_not_signed_by_the_charter_issuer(self):
        other, _ = trust.generate_keypair()
        self.assertCode("CREDENTIAL_SIGNATURE", cred=trust.sign(self.cred["body"], other))
        self.assertCode("CREDENTIAL_SIGNATURE", cred=trust.sign(self.cred["body"], self.root))   # Root is not the issuer

    def test_credential_for_another_charter(self):
        self.assertCode("WRONG_CHARTER", cred=self.resign_credential(charter_id="ch-other"))

    def test_time_window(self):
        self.assertCode("NOT_YET_VALID", now=NOW - 1)
        self.assertCode("EXPIRED", now=NOW + 600)

    def test_credential_moved_to_another_task_or_edited_contract(self):
        self.assertCode("WRONG_TASK", c=contract("t2"))
        self.assertCode("CONTRACT_MISMATCH", c=replace(self.c, goal="something else"))

    def test_credential_granting_more_than_the_charter(self):
        self.assertCode("EXCEEDS_CHARTER", cred=self.resign_credential(tools=["read_file", "delete_everything"]))
        self.assertCode("EXCEEDS_CHARTER", cred=self.resign_credential(network=True))
        self.assertCode("EXCEEDS_CHARTER", cred=self.resign_credential(budget=self.cred["body"]["budget"] | {"max_tokens": None}))

    def test_contract_asking_more_than_the_credential(self):
        bigger = replace(self.c, budget=Budget(max_rounds=6, max_model_calls=8, max_tokens=1000, max_commands=3))
        cred = self.resign_credential(contract_sha256=bigger.digest())
        self.assertCode("EXCEEDS_CREDENTIAL", cred=cred, c=bigger)

    def test_issue_refuses_beyond_caps(self):
        for c in (contract(tools=("run_command", "edit_file")), contract(network=True),
                  contract(budget=Budget(max_rounds=11)), contract(budget=Budget(max_tokens=None))):
            with self.subTest(c=c), self.assertRaises(TrustError) as e:
                trust.issue_credential(self.charter_body, self.issuer, c, NOW, 600)
            self.assertEqual(e.exception.code, "EXCEEDS_CHARTER")

    def test_malformed_signed_document_is_format_error_not_crash(self):
        body = {k: v for k, v in self.charter_body.items() if k != "caps"}
        self.assertCode("FORMAT", charter=trust.sign(body, self.root))

    def test_caps_must_be_explicit_never_unlimited(self):
        missing = {k: v for k, v in self.caps.items() if k != "max_tokens"}
        for caps in (missing, self.caps | {"max_tokens": None}, self.caps | {"network": "yes"},
                     self.caps | {"tools": "read_file"}, self.caps | {"max_rounds": True}):
            body = self.charter_body | {"caps": caps}
            with self.subTest(caps=caps):
                self.assertCode("FORMAT", charter=trust.sign(body, self.root))
                with self.assertRaises(TrustError) as e:
                    trust.issue_credential(body, self.issuer, self.c, NOW, 600)
                self.assertEqual(e.exception.code, "FORMAT")


class ProductionTest(Chain):
    def setUp(self):
        super().setUp("PRODUCTION")

    def test_dev_release_refused_in_production(self):
        self.assertCode("RELEASE")

    def test_production_chain_with_exact_release_passes(self):
        charter = trust.sign(self.charter_body | {"release": trust.release_digest(CODE)}, self.root)
        self.assertEqual(self.verify(charter=charter)["trust_domain"], "PRODUCTION")


if __name__ == "__main__":
    unittest.main()
