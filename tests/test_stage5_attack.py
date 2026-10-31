"""T46 stage 5 attack drill: an agent attacks the framework itself; every attack is refused or exposed."""
import unittest
from unittest.mock import patch

import tests.test_fingerprint as fp
import tests.test_release as release
from m2k import trust
from m2k.ledger import Ledger
from tests.helpers import step


class _Config(release.ReleaseTest):
    pass


class _Framework(fp.FingerprintTest):
    pass


for _base, _parent in ((_Config, release.ReleaseTest), (_Framework, fp.FingerprintTest)):
    for _name in [n for n in dir(_parent) if n.startswith("test_")]:
        setattr(_base, _name, None)   # do not re-run the parent module's tests here

del _base, _parent, _name   # a TestCase left in a module-level name would run all its tests here


class FrameworkAttacks(_Framework):
    def test_a01_framework_code_changed_mid_task(self):
        kernel = self.kernel([fp.GOOD, self.changed(self.code / "cli.py", step("write_file", path="second.txt", content="x"))])
        result = kernel.run(self.contract(task_id="a01", budget=fp.BUDGET))
        self.assert_blocked(result, "code", "a01")


class ConfigAttacks(_Config):
    def test_a02_reviewer_swapped_without_resigning(self):
        self.config.write_bytes(self.config.read_bytes().replace(b'model = "reviewer"', b'model = "attacker-reviewer"'))
        self.refuse_then_recover("RELEASE_CONFIG")

    def test_a03_root_pin_or_charter_replaced(self):
        other, other_key = trust.dev_setup(self.root / "other", release.CAPS)
        self.pin.write_bytes(trust.canonical({"trust_domain": "TEST_ONLY", "public_key_b64": trust.public_key_b64(other_key)}))
        with patch("m2k.cli.build_model") as models:
            code, out, err = self.cli("run", self.task)
        self.assertNotEqual(code, 0)
        models.assert_not_called()
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.events("t43", "task.submitted"), [])
        self.assertIn("RELEASE_SIGNATURE", err)
        self.restore()
        (self.keys / "charter.json").write_bytes(trust.canonical(other.charter_env))
        worker = release.ScriptedModel([step("finish")], "worker")
        reviewer = release.ScriptedModel([release.review("GO")], "reviewer")
        with patch("m2k.cli.build_model", side_effect=[worker, reviewer]) as models:
            code, out, err = self.cli("run", self.task)
        self.assertEqual(code, 2, out + err)
        self.assertIn("拒绝启动：信任链核对失败（CHARTER_SIGNATURE）：", err)
        models.assert_not_called()
        self.assertFalse(worker.calls)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.events("t43", "task.submitted"), [])
            self.assertEqual(ledger.last("m2k.startup", "startup.refused").payload["code"], "CHARTER_SIGNATURE")

    def test_a04_config_rolled_back_with_old_manifest(self):
        self.rollback_and_recover(False)

    def test_a05_revoked_manifest_and_revocation_list_rollback(self):
        self.write_revocations(3)
        self.sign(slot="r", manifest_id="r1")
        self.run_scripted()
        self.write_revocations(2)   # an older revocation list must not be able to un-revoke anything
        self.set_task_id("old-list")
        self.startup_refused("RELEASE_ROLLBACK", "revocations rollback", "old-list")
        self.write_revocations(4, ["r1"])
        self.set_task_id("revoked")
        with patch("m2k.cli.build_model") as models:
            code, out, err = self.cli("run", self.task)
        self.assertEqual(code, 2, out + err)
        self.assertIn("RELEASE_REVOKED", err)
        models.assert_not_called()

    def test_a18_notary_swapped_in_config(self):
        key, public = trust.generate_keypair()
        (self.keys / "attacker-obs.pem").write_bytes(self.pem(key))
        swapped = ('[obs]\nkey = "keys/attacker-obs.pem"\nlog = "obs.jsonl"\npublic_key_b64 = '
                   + json.dumps(public) + '\n').encode("utf-8")
        self.assertIn(self.obs_block, self.config.read_bytes())
        self.config.write_bytes(self.config.read_bytes().replace(self.obs_block, swapped))   # no self.sign()
        self.refuse_then_recover("RELEASE_CONFIG")


# ---- part 2: ledger and OBS ---------------------------------------------------------------------------------------
import json

import tests.helpers as helpers
import tests.test_obs as obs_tests
from m2k import obs
from m2k.adjudicate import adjudicate


class ObsAttacks(obs_tests.WitnessFiles, helpers.KernelCase):
    def test_a06_kernel_pass_not_backed_by_facts(self):
        observer, _, public, _ = self.client()
        bad = [step("write_file", path="calc.py", content=helpers.BAD_CODE), step("finish")]
        self.assertEqual(self.run_observed(observer, steps=bad).verdict.value, "FAIL")
        forged = []
        for kind in ("candidate.frozen", "test.result", "audit.result", "verdict"):
            payload = self.ledger.last("t1", kind).payload.copy()
            if kind == "test.result":
                payload.update(verdict="PASS", exit_code=0, reason=None)
            if kind == "verdict":
                payload.update(verdict="PASS", reasons=[])
            forged.append(self.ledger.append("t1", kind, payload))
        self.assertTrue(self.ledger.verify_chain()[0])
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "PASS")   # what an attacker hopes for
        result = adjudicate(self.ledger, "t1", obs_public_key=public)
        self.assertEqual(result.verdict, "FAIL")
        for event in forged:
            self.assertIn(f"no valid OBS receipt for {event.kind} at seq {event.seq}", result.reasons)

    def test_a07_receipts_signed_by_another_key(self):
        _, configured = trust.generate_keypair()
        observer, _, other_public, _ = self.client("other")
        self.run_observed(observer)
        self.assertEqual(adjudicate(self.ledger, "t1", obs_public_key=other_public).verdict, "PASS")
        result = adjudicate(self.ledger, "t1", obs_public_key=configured)
        self.assertEqual(result.verdict, "FAIL")
        self.assertEqual(len(result.reasons), 6)

    def test_a08_receipt_missing_from_log_or_log_rechained(self):
        observer, key, public, log = self.client("drop")   # case 1: the log line is deleted, the log re-signed
        self.run_observed(observer, contract=self.contract(task_id="drop")).verdict.value
        observer.close()
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        removed = rows.pop(2)
        self.resign_log(log, rows, key)
        self.assertEqual(obs.check_log(log, public), [])
        result = adjudicate(self.ledger, "drop", obs_public_key=public, obs_log=log)
        self.assertEqual(result.verdict, "FAIL")
        self.assertIn(f"OBS receipt {removed['seq']} missing or different in log", result.reasons)

        observer, _, public, log = self.client("edit")   # case 2: a log row is edited and re-chained without the key
        self.assertEqual(self.run_observed(observer, contract=self.contract(task_id="edit")).verdict.value, "PASS")
        observer.close()
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        rows[1]["request"]["task_id"] = "edited-task"
        self.rewrite_log(log, rows)
        self.assertEqual(obs.check_log(log), [])
        self.assertTrue(obs.check_log(log, public))
        result = adjudicate(self.ledger, "edit", obs_public_key=public, obs_log=log)
        self.assertEqual(result.verdict, "FAIL")

    def test_a09_replay_to_the_witness(self):
        _, public, key, log = self.keys()
        req = dict(task_id="task", kind="verdict", ledger_id="a" * 64, ledger_seq=3, ledger_hash="b" * 64)

        def send(requests):
            result = obs_tests.subprocess.run(self.command(key, log),
                                              input="".join(json.dumps(r) + "\n" for r in requests),
                                              text=True, encoding="utf-8", capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return [json.loads(line) for line in result.stdout.splitlines()]

        responses = send([req, req, dict(req, ledger_seq=2), dict(req, ledger_seq=4),
                          dict(req, ledger_id="c" * 64, ledger_seq=1)])
        self.assertEqual(["error" in response for response in responses], [False, True, True, False, False])
        self.assertEqual(len(log.read_bytes().splitlines()), 3)
        before = log.read_bytes()
        self.assertIn("error", send([dict(req, ledger_seq=4)])[0])
        self.assertEqual(log.read_bytes(), before)
        self.assertNotIn("error", send([dict(req, ledger_seq=5)])[0])
        self.assertEqual(obs.check_log(log, public), [])

    def test_a17_witness_killed_mid_task(self):
        observer, _, _, _ = self.client()

        def finish_after_kill(messages):
            observer.process.kill()
            observer.process.wait()
            return step("finish")

        result = self.run_observed(observer, steps=[obs_tests.GOOD[0], finish_after_kill])
        verdict = self.ledger.last("t1", "verdict").payload
        self.assertEqual(verdict["verdict"], "UNKNOWN")
        self.assertIn("OBS_UNAVAILABLE:", " ".join(verdict["reasons"]))
        self.assertNotEqual(result.reported_verdict.value, "PASS")
        self.assertIsNone(result.delivered_to)
        self.assertEqual(self.ledger.events("t1", "delivery.intent"), [])


# ---- part 3: anchors, and replacing the whole Root ----------------------------------------------------------------
import shutil

import tests.test_anchor as anchor_tests


@unittest.skipUnless(shutil.which("git"), "needs git")
class AnchorAttacks(anchor_tests.SignedAnchorTest):
    def test_a10_ledger_and_obs_log_deleted_and_restarted(self):
        old = self.checkpoint(3)
        self.publish()
        self.fresh("restarted")          # delete the ledger, the OBS log and the local anchors; same OBS key
        self.checkpoint(5)
        self.publish()
        problems = self.remote()
        self.assertIn(f"OBS log replaced or truncated: obs_seq 3 in anchors/{old['ledger_id'][:12]}.jsonl", problems)

    def test_a11_ledger_and_obs_log_rewritten_with_the_key(self):
        self.checkpoint(3)
        self.publish()
        self.fresh("rewritten")
        self.checkpoint(3)
        self.assertEqual(self.ledger.verify_chain(), (True, None))
        self.assertEqual(obs.check_log(self.log, self.public), [])
        self.assertTrue(any("OBS log replaced or truncated" in p for p in self.remote()))

    def test_a12_anchor_history_rewritten(self):
        record = self.checkpoint()
        self.publish()
        file = f"anchors/{record['ledger_id'][:12]}.jsonl"
        with self.subTest(case="anchor file deleted by an ordinary push"):
            self.git("rm", "--", file)
            self.assert_history_problem_after_push(file)
        with self.subTest(case="history rewritten according to the API"):
            for kind in ("force_push", "branch_deletion"):
                self.api.return_value = [{"activity_type": kind, "actor": {"login": "x"}, "timestamp": "t"}]
                with self.subTest(kind=kind):
                    self.assertIn(f"{kind} by x at t", self.remote())


for _name in [n for n in dir(anchor_tests.SignedAnchorTest) if n.startswith("test_")]:
    setattr(AnchorAttacks, _name, None)   # do not re-run the parent module's tests here
del _name


class RootSwap(_Config):
    def test_a21_whole_root_replaced(self):
        self.run_scripted()   # a normal run under the owner's Root
        first = self.setup.pin.fingerprint
        self.replace_root()   # re-sign everything, but retain the same OBS key, log, slot and version
        self.set_task_id("swapped")
        self.startup_refused("ROOT_CHANGED",
                             f"root changed: TEST_ONLY first seen {first}, now {self.setup.pin.fingerprint}", "swapped")

# ---- part 4: delivery --------------------------------------------------------------------------------------------
from dataclasses import replace

import tests.test_delivery as delivery_tests
from m2k.delivery import SUFFIX
from m2k.kernel import Kernel, KernelError
from m2k.models import ScriptedModel
from m2k.verify import freeze_one


class DeliveryAttacks(delivery_tests.DeliveryTest):
    def test_a13_files_planted_in_delivery_folder(self):
        self.run_delivery("real")
        self.run_delivery("t1")
        (self.out / "fake").mkdir()
        (self.out / "fake" / "payload.py").write_text("print('planted')\n", encoding="utf-8")
        (self.out / "t1" / "extra.py").write_text("print('planted')\n", encoding="utf-8")
        self.assert_problem("fake", "missing receipt")
        self.assert_problem("t1", "extra file: extra.py")
        (self.out / "t1" / "extra.py").unlink()
        (self.out / "t1" / "calc.py").write_bytes(b"changed\n")
        self.assert_problem("t1", "changed file: calc.py")
        for full in (False, True):
            self.assertEqual(self.check(full)["real"]["problems"], [])   # the untouched real delivery stays OK

    def test_a19_staging_tampered_in_the_release_window(self):
        record = Kernel._record

        def tamper(kernel, tid, kind, payload):
            if kind == "anchor.checkpoint":   # after the delivery receipt, before the folder is renamed
                stage = self.out / (".m2k-staging-" + tid)
                (stage / "calc.py").write_bytes(b"tampered\n")
                (stage / "added.py").write_text("print('added')\n", encoding="utf-8")
            return record(kernel, tid, kind, payload)

        with patch.object(Kernel, "_record", tamper):
            self.run_delivery()
        self.assertTrue((self.out / "t1" / "added.py").is_file())
        self.assert_problem("t1", "changed file: calc.py", "extra file: added.py")

    def test_a14_portable_receipt_forged(self):
        self.run_delivery()
        path = self.out / ("t1" + SUFFIX)
        original_receipt = path.read_bytes()
        calc = self.out / "t1" / "calc.py"
        original_calc = calc.read_bytes()
        calc.write_bytes(b"tampered\n")                     # case 1: the receipt is edited to bless the tampered file
        doc = json.loads(original_receipt)
        doc["event"]["payload"]["files"]["calc.py"] = freeze_one(calc)
        path.write_bytes(trust.canonical(doc))
        self.assert_problem("t1", "event hash mismatch")
        path.write_bytes(original_receipt)                  # case 2: the receipt body is re-signed with another key
        calc.write_bytes(original_calc)
        doc = json.loads(original_receipt)
        doc["receipt"] = trust.sign(doc["receipt"]["body"], trust.generate_keypair()[0])
        path.write_bytes(trust.canonical(doc))
        self.assert_problem("t1", "signature does not verify")
        path.write_bytes(original_receipt)                  # case 3: a real receipt copied onto a fake folder
        (self.out / "fake").mkdir()
        (self.out / "fake" / "fake.txt").write_text("fake", encoding="utf-8")
        shutil.copyfile(path, self.out / ("fake" + SUFFIX))
        self.assert_problem("fake", "task name mismatch")

    def test_a15_deliver_to_inside_the_workspace(self):
        contract = self.contract()
        for target in (contract.workspace, contract.workspace / "out", contract.workspace.parent):
            with self.subTest(target=target), self.assertRaisesRegex(KernelError, "overlaps the workspace"):
                Kernel(self.ledger, ScriptedModel([], "w"), ScriptedModel([], "r")).run(replace(contract, deliver_to=target))
            self.assertEqual(self.ledger.events("t1"), [])


for _name in [n for n in dir(delivery_tests.DeliveryTest) if n.startswith("test_")]:
    setattr(DeliveryAttacks, _name, None)   # do not re-run the parent module's tests here
del _name


class CliAttacks(delivery_tests.DeliveryCliTest):
    def test_a16_ci_release_with_forged_tail(self):
        def append_tail():
            with Ledger(self.f.ledger_path) as ledger:
                event = ledger.last("t43", "verdict")
                ledger.append("t43", "verdict", event.payload)
                self.assertEqual(adjudicate(ledger, "t43").verdict, "PASS")

        code, output, pristine = self.task_result(changes=append_tail)
        self.assertEqual(code, 1, output)
        self.assertIn("::error::adjudication FAIL", output)
        self.assertIn("no valid OBS receipt for verdict", output)
        self.assertFalse((pristine / "result.txt").exists())

    def test_a20_untrusted_self_delivery(self):
        f = self.f
        f.config.write_bytes(f.config.read_bytes().split(b"[trust]", 1)[0])
        with patch("m2k.cli.build_model", side_effect=[ScriptedModel([step("finish")], "w"), ScriptedModel([delivery_tests.review("GO")], "r")]):
            code, out, err = f.cli("run", "--untrusted", f.task)
        self.assertEqual(code, 0, out + err)
        self.assertIn("警告：本次交付没有回执（未配置 OBS），m2k check-delivery 会把它列为未经核验。", err)
        self.assertIn("1 项未经核验", err)
        self.assertTrue((f.root / "out/t43").is_dir())
        code, out, err = f.cli("check-delivery", f.root / "out", "--standalone", "--public-key", f.obs_public)
        self.assertEqual(code, 1, out + err)
        self.assertIn("未经核验 t43: missing receipt", out)


for _name in [n for n in dir(delivery_tests.DeliveryCliTest) if n.startswith("test_")]:
    setattr(CliAttacks, _name, None)   # do not re-run the parent module's tests here
del _name


if __name__ == "__main__":
    unittest.main()
