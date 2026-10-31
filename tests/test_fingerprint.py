"""T40: pin code/config/trust inputs; tampering touches temporary copies only."""
import hashlib
import io
import json
import shutil
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from m2k import trust
from m2k.adjudicate import adjudicate
from m2k.cli import main
from m2k.contract import Budget
from m2k.fingerprint import framework_fingerprint
from m2k.kernel import Kernel, status
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.verify import run_test
from tests.helpers import GOOD_CODE, KernelCase, review, step
from tests.test_task_tree import TreeCase, good_a, good_b, sub

CAPS = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"], "max_rounds": 100,
        "max_model_calls": 100, "max_tokens": 1000000, "max_commands": 100, "network": False}
BUDGET = Budget(max_rounds=10, max_model_calls=12, max_tokens=100000, max_commands=4)
GOOD = step("write_file", path="calc.py", content=GOOD_CODE)


class FingerprintTest(KernelCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.code = self.root / "code"
        shutil.copytree(Path(trust.__file__).parent, self.code, ignore=shutil.ignore_patterns("__pycache__"))
        self.setup, self.root_key = trust.dev_setup(self.root / "keys", CAPS)
        self.config = self.root / "m2k.toml"
        self.config.write_text('private setting = "do-not-record-this-content"\r\n', encoding="utf-8")
        self.pin, self.charter = self.root / "keys/root-pin.json", self.root / "keys/charter.json"
        self.pin.write_bytes(trust.canonical({"trust_domain": "TEST_ONLY", "public_key_b64": trust.public_key_b64(self.root_key)}))
        self.charter.write_bytes(trust.canonical(self.setup.charter_env))
        self.files = [self.config, self.pin, self.charter, self.setup.revocations_path, self.setup.issuer_key_path]

    def kernel(self, steps, reviews=None, trusted=True, pinned=True):
        self.worker = ScriptedModel(steps, "worker")
        self.reviewer = ScriptedModel(reviews if reviews is not None else [review("GO")], "reviewer")
        self.framework = framework_fingerprint(self.code, self.files) if pinned else None
        return Kernel(self.ledger, self.worker, self.reviewer, trust=self.setup if trusted else None, framework=self.framework)

    def changed(self, path, reply):
        def change(messages):
            path.write_bytes(path.read_bytes() + b"\n")
            return reply(messages) if callable(reply) else reply
        return change

    def assert_blocked(self, result, part, tid="t1"):
        self.assertEqual((result.verdict.value, result.worker.status.value), ("FAIL", "TRUST_BROKEN"), result.reasons)
        self.assertIn("FRAMEWORK_CHANGED", result.worker.reason)
        self.assertIn(part, result.worker.reason)
        self.assertIsNone(result.delivered_to)
        self.assertEqual(self.ledger.events(tid, "delivery.intent"), [])
        self.assertEqual(status(self.ledger, tid)["worker"]["status"], "TRUST_BROKEN")

    def test_hashes_exact_bytes_canonical_order_and_missing_files_without_contents(self):
        missing = self.root / "missing"
        fingerprint = framework_fingerprint(self.code, [*self.files, missing])
        self.assertEqual(fingerprint["code"], trust.release_digest(self.code))
        self.assertEqual(fingerprint["files"][str(self.config)], hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertIsNone(fingerprint["files"][str(missing)])
        body = {"code": fingerprint["code"], "files": fingerprint["files"]}
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(fingerprint["digest"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(fingerprint["digest"], framework_fingerprint(self.code, reversed([*self.files, missing]))["digest"])
        self.assertNotIn("do-not-record-this-content", json.dumps(fingerprint))
        self.assertNotIn("BEGIN PRIVATE KEY", json.dumps(fingerprint))
        missing.write_bytes(b"now present")
        self.assertNotEqual(fingerprint["digest"], framework_fingerprint(self.code, [*self.files, missing])["digest"])

    def test_unchanged_trusted_and_untrusted_tasks_pass_and_status_shows_digest(self):
        for trusted in (True, False):
            tid = f"stable-{trusted}"
            with self.subTest(trusted=trusted):
                kernel = self.kernel([GOOD, step("finish")], trusted=trusted)
                result = kernel.run(self.contract(task_id=tid, budget=BUDGET))
                self.assertEqual(result.verdict.value, "PASS", result.reasons)
                self.assertIsNotNone(result.delivered_to)
                submitted = self.ledger.last(tid, "task.submitted").payload
                self.assertEqual(submitted["framework"], self.framework)
                self.assertEqual(status(self.ledger, tid)["framework_sha256"], self.framework["digest"])
                if trusted:
                    body = trust.open_envelope(kernel.credential, self.setup.issuer_key().public_key())
                    self.assertEqual(body["framework_sha256"], self.framework["digest"])
                else:
                    self.assertEqual(self.ledger.events(tid, "credential.issued"), [])
                self.assertEqual(adjudicate(self.ledger, tid).verdict, "PASS")

    def test_none_preserves_legacy_behavior_when_the_temporary_framework_changes(self):
        kernel = self.kernel([GOOD, self.changed(self.config, step("finish"))], pinned=False)
        result = kernel.run(self.contract(budget=BUDGET))
        self.assertEqual(result.verdict.value, "PASS", result.reasons)
        self.assertIsNotNone(result.delivered_to)
        self.assertNotIn("framework", self.ledger.last("t1", "task.submitted").payload)
        self.assertNotIn("framework_sha256", kernel.credential["body"])

    def test_changed_code_config_and_each_trust_file_stop_the_next_action(self):
        for index, path in enumerate([self.code / "cli.py", *self.files]):
            tid = f"action-{index}"
            original = path.read_bytes()
            with self.subTest(path=path.name):
                kernel = self.kernel([GOOD, self.changed(path, step("write_file", path="second.txt", content="x"))])
                with patch("m2k.kernel.run_test", wraps=run_test) as acceptance:
                    result = kernel.run(self.contract(task_id=tid, budget=BUDGET))
                acceptance.assert_not_called()
                self.assert_blocked(result, "code" if path.parent == self.code else path.name, tid)
                self.assertEqual(self.reviewer.calls, [])
                self.assertFalse((self.root / "ws" / tid / "second.txt").exists())
                denied = self.ledger.last(tid, "gate.denied").payload
                self.assertEqual(denied["gate"], "trust_chain")
                self.assertIn("FRAMEWORK_CHANGED", denied["reason"])
            path.write_bytes(original)

    def test_change_between_final_action_and_test_stops_with_and_without_chain(self):
        for trusted in (True, False):
            tid = f"test-{trusted}"
            with self.subTest(trusted=trusted):
                kernel = self.kernel([GOOD, self.changed(self.code / "cli.py", step("finish"))], trusted=trusted)
                with patch("m2k.kernel.run_test", wraps=run_test) as acceptance:
                    result = kernel.run(self.contract(task_id=tid, budget=BUDGET))
                acceptance.assert_not_called()
                self.assert_blocked(result, "code", tid)
                self.assertIn("FRAMEWORK_CHANGED", result.test.reason)
                self.assertEqual(self.reviewer.calls, [])

    def test_change_during_review_makes_verdict_fail_before_delivery_with_and_without_chain(self):
        for trusted in (True, False):
            tid = f"delivery-{trusted}"
            with self.subTest(trusted=trusted):
                kernel = self.kernel([GOOD, step("finish")],
                                     [self.changed(self.config, review("GO"))], trusted=trusted)
                result = kernel.run(self.contract(task_id=tid, budget=BUDGET))
                self.assert_blocked(result, self.config.name, tid)
                self.assertEqual((result.test.verdict.value, result.audit.decision), ("PASS", "GO"))
                self.assertEqual(self.ledger.last(tid, "verdict").payload["verdict"], "FAIL")
                self.assertIn("FRAMEWORK_CHANGED", self.ledger.last(tid, "delivery.refused").payload["reason"])
                self.assertEqual(adjudicate(self.ledger, tid).verdict, "FAIL")

    def test_deleted_file_is_reported_by_name_and_never_delivered(self):
        def remove(messages):
            self.config.unlink()
            return step("finish")
        result = self.kernel([GOOD, remove]).run(self.contract(budget=BUDGET))
        self.assert_blocked(result, self.config.name)

    def test_adjudicator_rejects_a_credential_framework_mismatch_or_missing_digest(self):
        kernel = self.kernel([GOOD, step("finish")])
        kernel.run(self.contract(budget=BUDGET))
        body = kernel.credential["body"].copy()
        for value in ("different", None):
            with self.subTest(value=value):
                if value is None:
                    body.pop("framework_sha256")
                else:
                    body["framework_sha256"] = value
                credential = trust.sign(body, self.setup.issuer_key())
                self.ledger.append("t1", "credential.issued", {"credential": credential})
                before = self.ledger.events()
                result = adjudicate(self.ledger, "t1")
                self.assertEqual(result.verdict, "FAIL")
                self.assertIn("framework_sha256", " ".join(result.reasons))
                self.assertEqual(before, self.ledger.events())

    def test_cli_hashes_config_and_trust_paths_before_opening_ledger(self):
        for trusted in (True, False):
            tid, ledger_path = f"cli-{trusted}", self.root / f"cli-{trusted}.sqlite"
            self.contract(task_id=tid, budget=BUDGET)
            task = self.root / f"{tid}.toml"
            task.write_text(f'[task]\nid = "{tid}"\ngoal = "write calc"\nworkspace = "ws/{tid}"\n'
                            '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n'
                            '[budget]\nmax_tokens = 100000\n', encoding="utf-8")
            config = f'ledger = "{ledger_path.name}"\n[sandbox]\nbackend = "none"\n[models.worker]\n[models.reviewer]\n'
            if trusted:
                key, public = trust.generate_keypair()
                (self.root / "keys/obs.pem").write_bytes(key.private_bytes(trust.serialization.Encoding.PEM,
                    trust.serialization.PrivateFormat.PKCS8, trust.serialization.NoEncryption()))
                config += ('[obs]\nkey = "keys/obs.pem"\nlog = "obs.jsonl"\npublic_key_b64 = '
                           + json.dumps(public) + '\n')
                config += ('[trust]\ndomain = "TEST_ONLY"\nroot_pin = "keys/root-pin.json"\ncharter = "keys/charter.json"\n'
                           'revocations = "keys/revocations.json"\nissuer_key = "keys/issuer.pem"\ncredential_ttl_s = 600\n'
                           'release = "keys/release.json"\n')
            self.config.write_text(config, encoding="utf-8")
            release = self.root / "keys/release.json"
            if trusted:
                release.write_bytes(trust.canonical(trust.sign({"schema": trust.RELEASE, "trust_domain": "TEST_ONLY",
                    "manifest_id": tid, "slot": "test", "version": 1, "code": "dev", "config": trust.config_digest(self.config.read_bytes()),
                    "issued_at": 1}, self.root_key)))

            def compute(code_dir, files, contents):
                self.assertFalse(ledger_path.exists(), "fingerprint must precede ledger creation")
                self.assertEqual(set(files), set([*self.files, release] if trusted else [self.config]))
                return framework_fingerprint(self.code, files, contents)

            worker, reviewer = ScriptedModel([GOOD, step("finish")], "w"), ScriptedModel([review("GO")], "r")
            with chdir(self.root), patch("m2k.cli.framework_fingerprint", side_effect=compute) as hashed, \
                    patch("m2k.cli.build_model", side_effect=[worker, reviewer]), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                flags = ["--config", "m2k.toml"] if trusted else []
                self.assertEqual(main([*flags, "run", *([] if trusted else ["--untrusted"]), str(task)]), 0)
            hashed.assert_called_once()
            with Ledger(ledger_path) as ledger:
                digest = ledger.last(tid, "task.submitted").payload["framework"]["digest"]
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--ledger", str(ledger_path), "status", tid]), 0)
            self.assertEqual(json.loads(out.getvalue())["framework_sha256"], digest)


class FingerprintTreeTest(TreeCase):
    def kernel(self, steps, trusted=True):
        setup, _ = trust.dev_setup(self.root / "keys", CAPS)
        self.code = self.root / "code"
        shutil.copytree(Path(trust.__file__).parent, self.code, ignore=shutil.ignore_patterns("__pycache__"))
        self.framework = framework_fingerprint(self.code, [setup.issuer_key_path])
        return Kernel(self.ledger, ScriptedModel(steps, "w"), ScriptedModel([review("GO")] * 3, "r"),
                      trust=setup if trusted else None, framework=self.framework)

    def parent_contract(self):
        return self.parent(subs=(sub("a", ["a.py"], "check_a.py", tokens=40000),
                                 sub("b", ["b.py"], "check_b.py", tokens=40000)), budget=BUDGET)

    def test_parent_and_children_have_the_same_signed_framework_digest(self):
        kernel = self.kernel(good_a() + good_b())
        result = kernel.run(self.parent_contract())
        self.assertEqual(result.verdict.value, "PASS", result.reasons)
        for tid in ("p1", "p1.a", "p1.b"):
            submitted = self.ledger.last(tid, "task.submitted").payload["framework"]
            credential = self.ledger.last(tid, "credential.issued").payload["credential"]
            body = trust.open_envelope(credential, kernel.trust.issuer_key().public_key())
            self.assertEqual(body["framework_sha256"], submitted["digest"])
            self.assertEqual(submitted, self.framework)
            self.assertEqual(adjudicate(self.ledger, tid).verdict, "PASS")

    def test_child_detects_changed_framework_before_another_action_and_never_merges(self):
        def change(messages):
            (self.code / "cli.py").write_text("# changed temporary copy\n", encoding="utf-8")
            return step("write_file", path="a.py", content="A = 1\n")
        kernel = self.kernel([good_a()[0], change])
        result = kernel.run(self.parent_contract())
        self.assertEqual((result.verdict.value, result.worker.status.value), ("FAIL", "TRUST_BROKEN"))
        self.assertIn("FRAMEWORK_CHANGED", result.worker.reason)
        self.assertEqual(status(self.ledger, "p1.a")["worker"]["status"], "TRUST_BROKEN")
        self.assertEqual(self.merges("p1"), [])
        self.assertIsNone(result.delivered_to)

    def test_untrusted_child_checks_fingerprint_at_test_boundary_only(self):
        def change(messages):
            (self.code / "cli.py").write_text("# changed temporary copy\n", encoding="utf-8")
            return step("write_file", path="a.py", content="A = 1\n")
        kernel = self.kernel([good_a()[0], change, step("finish")], trusted=False)
        result = kernel.run(self.parent_contract())
        self.assertEqual((result.verdict.value, result.worker.status.value), ("FAIL", "TRUST_BROKEN"))
        self.assertEqual(self.ledger.events("p1.a", "gate.denied"), [])
        self.assertEqual(len(self.ledger.events("p1.a", "effect.result")), 2)
        self.assertIn("FRAMEWORK_CHANGED", self.ledger.last("p1.a", "test.result").payload["reason"])
        self.assertEqual(self.merges("p1"), [])
