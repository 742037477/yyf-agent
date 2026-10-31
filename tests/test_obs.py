"""T42: real local witness processes, signed facts, and fail-closed delivery."""
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from m2k import obs, trust
from m2k.adjudicate import adjudicate
from m2k.cli import main
from m2k.contract import Budget
from m2k.kernel import Kernel, status
from m2k.ledger import GENESIS, Ledger
from m2k.models import ScriptedModel
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, review, step
from tests.test_task_tree import CHECK_A, TreeCase, good_a, good_b, sub

GOOD = [step("write_file", path="calc.py", content=GOOD_CODE), step("finish")]
CAPS = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"], "max_rounds": 100,
        "max_model_calls": 100, "max_tokens": 1000000, "max_commands": 100, "network": False}
BUDGET = Budget(max_rounds=10, max_model_calls=12, max_tokens=100000, max_commands=4)


class WitnessFiles:
    def keys(self, name="obs"):
        key, public = trust.generate_keypair()
        path, log = self.root / f"{name}.pem", self.root / f"{name}.jsonl"
        path.write_bytes(key.private_bytes(trust.serialization.Encoding.PEM, trust.serialization.PrivateFormat.PKCS8,
                                          trust.serialization.NoEncryption()))
        return key, public, path, log

    def command(self, key_path, log_path):
        return [sys.executable, "-m", "m2k", "obs", "serve", "--key", str(key_path), "--log", str(log_path)]

    def client(self, name="obs", timeout_s=10):
        key, public, path, log = self.keys(name)
        observer = obs.ObsClient(self.command(path, log), public, timeout_s, log_path=log)
        self.addCleanup(observer.close)
        return observer, key, public, log

    def run_observed(self, observer, contract=None, steps=None, setup=None):
        return Kernel(self.ledger, ScriptedModel(GOOD if steps is None else steps, "w"),
                      ScriptedModel([review("GO")], "r"), trust=setup, obs=observer).run(contract or self.contract())

    def rewrite_log(self, log, rows):
        raw, prev = [], GENESIS
        for row in rows:
            row["prev"] = prev
            line = trust.canonical(row) + b"\n"
            raw.append(line)
            prev = hashlib.sha256(line).hexdigest()
        log.write_bytes(b"".join(raw))

    def resign_log(self, log, rows, key):
        prev, lines = GENESIS, []
        for row in rows:
            row["prev"] = prev
            row["receipt"] = trust.sign(dict(row["receipt"]["body"], obs_prev=prev), key)
            line = trust.canonical(row) + b"\n"
            lines.append(line)
            prev = hashlib.sha256(line).hexdigest()
        log.write_bytes(b"".join(lines))


class ObsTest(WitnessFiles, KernelCase):
    def test_reconcile_rejects_a_ledger_receipt_absent_from_a_clean_resigned_log(self):
        observer, key, public, log = self.client()
        self.run_observed(observer)
        observer.close()
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        removed = rows.pop(2)
        self.resign_log(log, rows, key)
        self.assertEqual(obs.check_log(log, public), [])
        self.assertEqual(adjudicate(self.ledger, "t1", obs_public_key=public).verdict, "PASS")
        result = adjudicate(self.ledger, "t1", obs_public_key=public, obs_log=log)
        self.assertEqual(result.verdict, "FAIL")
        self.assertIn(f"OBS receipt {removed['seq']} missing or different in log", result.reasons)

    def test_reconcile_rejects_changed_missing_or_retyped_witnessed_ledger_events(self):
        observer, _, public, log = self.client()
        self.run_observed(observer)
        event = self.ledger.last("t1", "test.result")
        for change in ("hash", "kind", "remove"):
            with self.subTest(change=change), closing(sqlite3.connect(self.ledger.path)) as db:
                if change == "hash":
                    db.execute("UPDATE events SET hash = ? WHERE seq = ?", ("f" * 64, event.seq))
                elif change == "kind":
                    db.execute("UPDATE events SET hash = ?, kind = ? WHERE seq = ?", (event.hash, "changed", event.seq))
                else:
                    db.execute("DELETE FROM events WHERE seq = ?", (event.seq,))
                db.commit()
                result = adjudicate(self.ledger, "t1", check_chain=False, obs_public_key=public, obs_log=log)
                self.assertEqual(result.verdict, "FAIL")
                self.assertIn(f"OBS ledger event {event.seq} missing or changed for t1", result.reasons)

    def test_reconcile_rejects_log_receipts_removed_from_the_ledger(self):
        observer, _, public, log = self.client()
        self.run_observed(observer)
        receipt = self.ledger.events("t1", "obs.receipt")[0]
        seq = receipt.payload["receipt"]["body"]["obs_seq"]
        with closing(sqlite3.connect(self.ledger.path)) as db:
            db.execute("DELETE FROM events WHERE seq = ?", (receipt.seq,))
            db.commit()
        result = adjudicate(self.ledger, "t1", check_chain=False, obs_public_key=public, obs_log=log)
        self.assertEqual(result.verdict, "FAIL")
        self.assertIn(f"no ledger OBS receipt for log seq {seq} of t1", result.reasons)

    def test_reconcile_detects_same_task_witnessed_under_another_ledger(self):
        observer, _, public, log = self.client()
        self.run_observed(observer)
        original_id = self.ledger.ledger_id
        with Ledger(self.root / "fresh.sqlite") as fresh:
            report = Kernel(fresh, ScriptedModel(GOOD, "w"), ScriptedModel([review("GO")], "r"),
                            obs=observer).run(self.contract())
            self.assertNotEqual(fresh.ledger_id, original_id)
            self.assertEqual(report.verdict.value, "PASS")
            self.assertEqual(report.reported_verdict.value, "UNKNOWN")
            result = adjudicate(fresh, "t1", obs_public_key=public, obs_log=log)
            self.assertEqual(result.verdict, "FAIL")
            self.assertIn(f"OBS witnessed t1 in another ledger {original_id[:12]}", result.reasons)
            self.assertEqual(fresh.events("t1", "delivery.intent"), [])

    def test_rechained_unrelated_and_own_rows_are_reported_as_log_tampering(self):
        observer, _, public, log = self.client()
        self.run_observed(observer, self.contract(task_id="other"))
        self.run_observed(observer)
        observer.close()
        original = log.read_bytes()
        for task_id in ("other", "t1"):
            with self.subTest(task_id=task_id):
                rows = [json.loads(line) for line in original.splitlines()]
                row = next(row for row in rows if row["request"]["task_id"] == task_id)
                row["request"]["kind"] = "changed"
                self.rewrite_log(log, rows)
                self.assertEqual(obs.check_log(log), [])
                self.assertTrue(obs.check_log(log, public))
                result = adjudicate(self.ledger, "t1", obs_public_key=public, obs_log=log)
                self.assertEqual(result.verdict, "FAIL")
                self.assertIn("OBS 日志被改动", " ".join(result.reasons))
                self.assertIn(f"OBS log seq {rows[-1]['seq']}", " ".join(result.reasons))

    def test_last_receipt_for_another_task_requires_the_key_and_position_binding(self):
        observer, key, public, log = self.client()
        self.run_observed(observer)
        self.run_observed(observer, self.contract(task_id="other"))
        observer.close()
        original = log.read_bytes()
        wrong_key, _ = trust.generate_keypair()
        cases = (("key", {}, wrong_key), ("obs_seq", {"obs_seq": 999}, key),
                 ("obs_prev", {"obs_prev": "e" * 64}, key))
        for name, changes, signer in cases:
            with self.subTest(change=name):
                rows = [json.loads(line) for line in original.splitlines()]
                tail = rows[-1]
                tail["receipt"] = trust.sign(dict(tail["receipt"]["body"], **changes), signer)
                self.rewrite_log(log, rows)
                self.assertEqual(obs.check_log(log), [])
                self.assertEqual(adjudicate(self.ledger, "t1", obs_public_key=public).verdict, "PASS")
                result = adjudicate(self.ledger, "t1", obs_public_key=public, obs_log=log)
                self.assertEqual(result.verdict, "FAIL")
                self.assertTrue(any(f"OBS 日志被改动：OBS log seq {tail['seq']}" in p for p in result.reasons), result.reasons)

    def test_own_log_rows_still_require_signatures_when_the_tail_is_valid(self):
        observer, key, public, log = self.client()
        self.run_observed(observer)
        self.run_observed(observer, self.contract(task_id="other"))
        observer.close()
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        own = rows[0]
        own["receipt"] = trust.sign(own["receipt"]["body"], trust.generate_keypair()[0])
        self.rewrite_log(log, rows)
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        tail = rows[-1]
        tail["receipt"] = trust.sign(dict(tail["receipt"]["body"], obs_prev=tail["prev"]), key)
        self.rewrite_log(log, rows)
        self.assertEqual(obs.check_log(log), [])
        self.assertEqual(obs.receipt_body(tail["receipt"], public)["obs_prev"], tail["prev"])
        result = adjudicate(self.ledger, "t1", obs_public_key=public, obs_log=log)
        self.assertEqual(result.verdict, "FAIL")
        self.assertIn(f"OBS 日志被改动：OBS log seq {own['seq']}: SIGNATURE: signature does not verify", result.reasons)

    def test_receipt_pointer_to_another_tasks_log_row_also_requires_its_signature(self):
        observer, key, public, log = self.client()
        self.run_observed(observer)
        self.run_observed(observer, self.contract(task_id="other"))
        observer.close()
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        other = next(row for row in rows if row["request"]["task_id"] == "other")
        other["receipt"]["signature_b64"] = "invalid"
        self.rewrite_log(log, rows)
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        tail = rows[-1]
        tail["receipt"] = trust.sign(dict(tail["receipt"]["body"], obs_prev=tail["prev"]), key)
        self.rewrite_log(log, rows)
        self.assertEqual(obs.receipt_body(tail["receipt"], public)["obs_prev"], tail["prev"])
        self.ledger.append("t1", "obs.receipt", {"receipt": other["receipt"]})
        problems = obs.reconcile(self.ledger, "t1", log, public)
        self.assertTrue(any(f"OBS log seq {other['seq']}" in p for p in problems), problems)

    def test_reconcile_reports_missing_broken_and_malformed_logs(self):
        observer, _, public, log = self.client()
        self.run_observed(observer)
        observer.close()
        self.assertEqual(obs.reconcile(self.ledger, "t1", log, public), [])
        original = log.read_bytes()
        for raw in (b"not-json\n", original.rstrip(b"\n"), original.replace(b'"prev":"', b'"prev":"f', 1)):
            with self.subTest(raw=raw[:30]):
                log.write_bytes(raw)
                result = adjudicate(self.ledger, "t1", obs_public_key=public, obs_log=log)
                self.assertEqual(result.verdict, "FAIL")
                self.assertIn("OBS log line", " ".join(result.reasons))
        log.unlink()
        self.assertEqual(adjudicate(self.ledger, "t1", obs_public_key=public, obs_log=log).verdict, "FAIL")

    def test_happy_path_with_and_without_trust_and_one_receipt_per_decisive_event(self):
        observer, _, public, log = self.client()
        setup, _ = trust.dev_setup(self.root / "trust", CAPS)
        for trusted in (False, True):
            tid = f"happy-{trusted}"
            with self.subTest(trusted=trusted):
                report = self.run_observed(observer, self.contract(task_id=tid, budget=BUDGET),
                                           setup=setup if trusted else None)
                self.assertEqual(report.verdict.value, "PASS", report.reasons)
                self.assertIsNotNone(report.delivered_to)
                self.assertEqual(adjudicate(self.ledger, tid, obs_public_key=public).verdict, "PASS")
                events = self.ledger.events(tid)
                decisive = [event for event in events if event.kind in obs.DECISIVE or event.kind == "anchor.checkpoint"]
                receipts = self.ledger.events(tid, "obs.receipt")
                self.assertEqual(len(receipts), 8 if trusted else 7)
                for event, receipt in zip(decisive, receipts):
                    self.assertEqual(events[events.index(event) + 1], receipt)
                    body = obs.receipt_body(receipt.payload["receipt"], public,
                                            obs.request(event, self.ledger.events()[0].hash))
                    self.assertEqual((body["strength"], body["notary"]), ("local", observer.notary))
                self.assertEqual(self.ledger.last(tid, "task.submitted").payload["obs"],
                                 {"notary": observer.notary, "strength": "local"})
                self.assertEqual((status(self.ledger, tid)["obs_receipts"], status(self.ledger, tid)["obs_failed"]),
                                 (len(decisive), []))
        self.assertEqual(obs.check_log(log, public), [])
        self.assertTrue(self.ledger.verify_chain()[0])

    def test_forged_tail_passes_legacy_adjudication_but_has_no_obs_receipts(self):
        observer, _, public, _ = self.client()
        report = self.run_observed(observer, steps=[step("write_file", path="calc.py", content=BAD_CODE), step("finish")])
        self.assertEqual(report.verdict.value, "FAIL")
        forged = []
        for kind in ("candidate.frozen", "test.result", "audit.result", "verdict"):
            payload = self.ledger.last("t1", kind).payload.copy()
            if kind == "test.result": payload.update(verdict="PASS", exit_code=0, reason=None)
            if kind == "verdict": payload.update(verdict="PASS", reasons=[])
            forged.append(self.ledger.append("t1", kind, payload))
        self.assertTrue(self.ledger.verify_chain()[0])
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "PASS")
        before = self.ledger.events()
        result = adjudicate(self.ledger, "t1", obs_public_key=public)
        self.assertEqual(result.verdict, "FAIL")
        for event in forged:
            self.assertIn(f"no valid OBS receipt for {event.kind} at seq {event.seq}", result.reasons)
        self.assertEqual(self.ledger.events(), before)

    def test_wrong_notary_receipts_are_fail(self):
        _, configured = trust.generate_keypair()
        observer, _, other_public, _ = self.client("other")
        self.run_observed(observer)
        self.assertEqual(adjudicate(self.ledger, "t1", obs_public_key=other_public).verdict, "PASS")
        result = adjudicate(self.ledger, "t1", obs_public_key=configured)
        self.assertEqual(result.verdict, "FAIL")
        self.assertEqual(len(result.reasons), 6)

    def test_witness_death_at_each_decisive_event_records_one_verdict_without_retry_or_delivery(self):
        setup, _ = trust.dev_setup(self.root / "trust", CAPS)
        for kind in obs.DECISIVE:
            with self.subTest(kind=kind):
                observer, _, _, _ = self.client(kind)
                witness, sent = observer.witness, []

                def die(event, ledger_id):
                    sent.append(event.seq)
                    if event.kind == kind:
                        observer.process.kill()
                        observer.process.wait()
                    return witness(event, ledger_id)

                tid = kind.replace(".", "-")
                with patch.object(observer, "witness", side_effect=die):
                    result = self.run_observed(observer, self.contract(task_id=tid, budget=BUDGET), setup=setup)
                verdicts = self.ledger.events(tid, "verdict")
                self.assertEqual(len(verdicts), 1)
                expected = "PASS" if kind in ("verdict", "delivery.result") else "UNKNOWN"
                self.assertEqual(result.verdict.value, expected, result.reasons)
                self.assertEqual(verdicts[0].payload["verdict"], expected)
                if kind == "verdict":
                    gate = self.ledger.last(tid, "gate.adjudication")
                    self.assertEqual(gate.payload["verdict"], "FAIL")
                    self.assertIn(f"no valid OBS receipt for verdict at seq {verdicts[0].seq}", gate.payload["reasons"])
                elif kind == "delivery.result":
                    self.assertEqual(self.ledger.last(tid, "gate.adjudication").payload["verdict"], "PASS")
                    self.assertEqual(result.reported_verdict.value, "UNKNOWN")
                    self.assertIsNotNone(self.ledger.last(tid, "delivery.result"))
                else:
                    self.assertIn("OBS_UNAVAILABLE:", " ".join(verdicts[0].payload["reasons"]))
                    self.assertIn("OBS_UNAVAILABLE:", " ".join(result.reasons))
                self.assertEqual(status(self.ledger, tid)["obs_failed"][0]["kind"], kind)
                self.assertEqual(sent, sorted(set(sent)))
                self.assertIsNone(result.delivered_to)
                if kind == "delivery.result":
                    self.assertEqual(len(self.ledger.events(tid, "delivery.intent")), 1)
                    self.assertFalse((self.root / "out" / tid).exists())
                    self.assertFalse((self.root / "out" / (".m2k-staging-" + tid)).exists())
                else:
                    self.assertEqual(self.ledger.events(tid, "delivery.intent"), [])

    def test_witness_dies_mid_worker(self):
        observer, _, _, _ = self.client()

        def finish_after_death(messages):
            observer.process.kill()
            observer.process.wait()
            return step("finish")

        result = self.run_observed(observer, steps=[GOOD[0], finish_after_death])
        self.assertEqual(result.verdict.value, "UNKNOWN")
        self.assertIn("OBS_UNAVAILABLE:", " ".join(result.reasons))
        self.assertEqual(self.ledger.events("t1", "delivery.intent"), [])

    def test_checkpoint_witness_failure_after_gate_pass_still_refuses_delivery(self):
        observer, _, _, _ = self.client()
        witness = observer.witness

        def send(event, ledger_id):
            if event.kind == "anchor.checkpoint":
                observer.close()
            return witness(event, ledger_id)

        with patch.object(observer, "witness", side_effect=send):
            report = self.run_observed(observer)
        self.assertEqual(report.verdict.value, "PASS")
        self.assertEqual(report.reported_verdict.value, "UNKNOWN")
        self.assertEqual(self.ledger.last("t1", "gate.adjudication").payload["verdict"], "PASS")
        self.assertEqual(self.ledger.last("t1", "obs.failed").payload["kind"], "anchor.checkpoint")
        self.assertIsNotNone(self.ledger.last("t1", "anchor.failed"))
        self.assertIn("ANCHOR_UNAVAILABLE", self.ledger.last("t1", "delivery.refused").payload["reason"])
        self.assertEqual(len(self.ledger.events("t1", "delivery.intent")), 1)
        self.assertTrue(self.ledger.last("t1", "delivery.result").payload["post_verified"])
        self.assertFalse((self.root / "out" / "t1").exists())
        self.assertFalse((self.root / "out" / ".m2k-staging-t1").exists())

    def test_startup_verifies_tail_signature_and_chain_while_public_check_verifies_every_signature(self):
        observer, _, public, log = self.client()
        self.run_observed(observer)
        observer.close()
        with patch("m2k.obs.receipt_body", wraps=obs.receipt_body) as verify:
            obs.serve(self.root / "obs.pem", log, io.StringIO(), io.StringIO())
            self.assertEqual(verify.call_count, 1)
        rows = [json.loads(raw) for raw in log.read_bytes().splitlines()]
        with patch("m2k.obs.receipt_body", wraps=obs.receipt_body) as verify:
            self.assertEqual(obs.check_log(log, public), [])
            self.assertEqual(verify.call_count, len(rows))
        rows[0]["request"]["kind"] = "changed"
        self.rewrite_log(log, rows)
        self.assertEqual(obs.check_log(log), [])
        with self.assertRaisesRegex(obs.ObsError, "position"):
            obs.serve(self.root / "obs.pem", log, io.StringIO(), io.StringIO())

    def test_witness_rejects_replay_and_rollback_across_restart_and_tracks_each_ledger(self):
        _, public, key, log = self.keys()
        req = dict(task_id="任务", kind="verdict", ledger_id="a" * 64, ledger_seq=3, ledger_hash="b" * 64)

        def send(requests):
            result = subprocess.run(self.command(key, log), input="".join(json.dumps(r) + "\n" for r in requests),
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

    def test_malformed_requests_are_refused_without_logging(self):
        _, _, key, log = self.keys()
        req = dict(task_id="t", kind="verdict", ledger_id="a" * 64, ledger_seq=1, ledger_hash="b" * 64)
        bad = [[], {}, dict(req, ledger_seq=True), dict(req, ledger_seq=0), dict(req, kind=1),
               dict(req, ledger_hash="z" * 64), dict(req, ledger_id="a" * 63), dict(req, extra=1)]
        result = subprocess.run(self.command(key, log), input="not-json\n" + "".join(json.dumps(r) + "\n" for r in bad),
                                text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all("error" in json.loads(line) for line in result.stdout.splitlines()))
        self.assertEqual(len(result.stdout.splitlines()), len(bad) + 1)
        self.assertFalse(log.exists())

    def test_edited_log_with_recomputed_chain_requires_the_public_key_to_detect(self):
        observer, _, public, log = self.client()
        self.run_observed(observer)
        observer.close()
        original = log.read_bytes()
        rows = [json.loads(raw) for raw in original.splitlines()]
        self.assertEqual(obs.check_log(log), [])
        self.assertEqual(obs.check_log(log, public), [])
        rows[1]["request"]["task_id"] = "edited-task"
        self.rewrite_log(log, rows)
        self.assertEqual(obs.check_log(log), [])
        self.assertTrue(obs.check_log(log, public))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["obs", "check", "--log", str(log)]), 0)
            self.assertEqual(main(["obs", "check", "--log", str(log), "--public-key", public]), 1)
        result = subprocess.run(self.command(self.root / "obs.pem", log), input="", text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(log.read_bytes().splitlines()), len(rows))

    def test_log_public_key_checks_signature_notary_position_and_every_request_field(self):
        observer, key, public, log = self.client()
        self.run_observed(observer)
        observer.close()
        original = log.read_bytes()
        changes = {"task_id": "other", "kind": "test.result", "ledger_id": "f" * 64, "ledger_seq": 99,
                   "ledger_hash": "e" * 64, "obs_seq": 99, "obs_prev": "d" * 64, "notary": "different"}
        for field, value in changes.items():
            with self.subTest(field=field):
                rows = [json.loads(line) for line in original.splitlines()]
                body = dict(rows[-1]["receipt"]["body"], **{field: value})
                rows[-1]["receipt"] = trust.sign(body, key)
                self.rewrite_log(log, rows)
                self.assertEqual(obs.check_log(log), [])
                self.assertTrue(obs.check_log(log, public))
        rows = [json.loads(line) for line in original.splitlines()]
        rows[-1]["receipt"]["signature_b64"] = "invalid"
        self.rewrite_log(log, rows)
        self.assertEqual(obs.check_log(log), [])
        self.assertTrue(obs.check_log(log, public))

    def test_log_structure_prev_seq_and_partial_lines_are_checked(self):
        observer, _, _, log = self.client()
        self.run_observed(observer)
        observer.close()
        original = log.read_bytes()
        for change in (lambda rows: rows[-1].update(seq=rows[0]["seq"]),
                       lambda rows: rows[-1].update(prev=GENESIS),
                       lambda rows: rows[-1].pop("request")):
            rows = [json.loads(line) for line in original.splitlines()]
            change(rows)
            log.write_bytes(b"".join(trust.canonical(row) + b"\n" for row in rows))
            self.assertTrue(obs.check_log(log))
        log.write_bytes(original.rstrip(b"\n"))
        self.assertTrue(obs.check_log(log))
        log.write_bytes(b"not-json\n")
        self.assertTrue(obs.check_log(log))

    def test_client_rejects_wrong_key_wrong_request_and_timeout(self):
        observer, key, public, _ = self.client()
        event = self.ledger.append("t1", "task.submitted", {})
        request = obs.request(event, event.hash)
        envelope = trust.sign(dict(schema=obs.SCHEMA, notary=observer.notary, strength="local",
                                   obs_seq=1, obs_prev=GENESIS, ts="now", **dict(request, task_id="other")), key)
        responder = [sys.executable, "-c", "import sys; sys.stdin.readline(); print(" + repr(json.dumps(envelope)) + ")"]
        for name, command, configured, timeout in (
                ("request", responder, public, 5),
                ("signature", self.command(self.root / "obs.pem", self.root / "wrong.jsonl"), trust.generate_keypair()[1], 5),
                ("timeout", [sys.executable, "-c", "import sys,time; sys.stdin.readline(); time.sleep(10)"], public, 0.1)):
            with self.subTest(name=name):
                client = obs.ObsClient(command, configured, timeout)
                self.addCleanup(client.close)
                with self.assertRaises(obs.ObsError): client.witness(event, event.hash)
                self.assertIsNotNone(client.process.poll())
        with self.assertRaises(obs.ObsError):
            obs.ObsClient([str(self.root / "no-such-program")], public)

    def test_client_has_minimal_environment_and_noninheritable_pipes(self):
        key, public, path, log = self.keys()
        real_popen = subprocess.Popen
        with patch.dict(os.environ, {"M2K_TEST_KEY": "do-not-pass", "OBS_KEY_PATH": str(path),
                                     "OBS_LOG_PATH": str(log), "UNRELATED_SETTING": "not-needed"}), \
                patch("m2k.obs.subprocess.Popen", wraps=real_popen) as started:
            observer = obs.ObsClient(self.command(path, log), public)
        self.addCleanup(observer.close)
        env = started.call_args.kwargs["env"]
        self.assertFalse(set(env) & {"M2K_TEST_KEY", "OBS_KEY_PATH", "OBS_LOG_PATH", "UNRELATED_SETTING"})
        self.assertTrue(started.call_args.kwargs["close_fds"])
        self.assertEqual(started.call_args.kwargs["cwd"], Path(obs.__file__).resolve().parent.parent)
        self.assertFalse(os.get_inheritable(observer.process.stdin.fileno()))
        self.assertFalse(os.get_inheritable(observer.process.stdout.fileno()))
        report = self.run_observed(observer)
        self.assertEqual(report.verdict.value, "PASS", report.reasons)

    def test_receipt_for_other_event_task_or_ledger_does_not_cover_the_last_fact(self):
        observer, key, public, _ = self.client()
        self.run_observed(observer)
        verdict = self.ledger.last("t1", "verdict")
        tail = self.ledger.append("t1", "verdict", verdict.payload)
        body = self.ledger.events("t1", "obs.receipt")[-1].payload["receipt"]["body"].copy()
        for fields in ({"ledger_seq": tail.seq}, {"ledger_hash": tail.hash},
                       {"ledger_seq": tail.seq, "ledger_hash": tail.hash, "task_id": "other"},
                       {"ledger_seq": tail.seq, "ledger_hash": tail.hash, "ledger_id": "0" * 64},
                       {"ledger_seq": tail.seq, "ledger_hash": tail.hash, "kind": "test.result"}):
            self.ledger.append("t1", "obs.receipt", {"receipt": trust.sign(dict(body, **fields), key)})
            self.assertEqual(adjudicate(self.ledger, "t1", obs_public_key=public).verdict, "FAIL")


class ObsCliTest(WitnessFiles, KernelCase):
    def run_case(self, name, deliver=False, die=False):
        _, public, key, log = self.keys(name)
        command, _ = self.config({"key": key.name, "log": log.name, "public_key_b64": public})
        config, task = self.root / "m2k.toml", self.root / "task.toml"
        config.write_text(f'anchor_file = "{name}.anchors.jsonl"\n' +
                          config.read_text(encoding="utf-8").replace("cli.sqlite", f"{name}.sqlite"), encoding="utf-8")
        if deliver:
            task.write_text(task.read_text(encoding="utf-8").replace('workspace = "ws/t1"\n',
                            f'workspace = "ws/t1"\ndeliver_to = "{name}-out"\n'), encoding="utf-8")
        real_client = obs.ObsClient

        def start(*args, **kwargs):
            client = real_client(*args, **kwargs)
            witness = client.witness

            def send(event, ledger_id):
                if die and event.kind == "verdict":
                    client.process.kill()
                    client.process.wait()
                return witness(event, ledger_id)

            client.witness = send
            return client

        out, err = io.StringIO(), io.StringIO()
        with patch("m2k.cli.obs.ObsClient", side_effect=start), \
                patch("m2k.cli.build_model", side_effect=[ScriptedModel(GOOD, "w"), ScriptedModel([review("GO")], "r")]), \
                redirect_stdout(out), redirect_stderr(err):
            code = main(command)
        return code, out.getvalue(), err.getvalue(), self.root / f"{name}.sqlite", public, log

    def test_verdict_witness_death_reports_unknown_with_and_without_delivery(self):
        for deliver in (False, True):
            name = f"dead-{deliver}"
            with self.subTest(deliver=deliver):
                code, out, err, path, _, _ = self.run_case(name, deliver=deliver, die=True)
                self.assertEqual(code, 2, out + err)
                self.assertIn("t1: UNKNOWN", out)
                self.assertIn("未交付", out)
                self.assertIn("内核判定 PASS", out)
                self.assertIn("裁决器 FAIL", out)
                with Ledger(path) as ledger:
                    verdicts = ledger.events("t1", "verdict")
                    self.assertEqual(len(verdicts), 1)
                    self.assertEqual(verdicts[0].payload["verdict"], "PASS")
                    self.assertEqual(ledger.last("t1", "obs.failed").payload["kind"], "verdict")
                    self.assertEqual(ledger.last("t1", "gate.adjudication").payload["verdict"], "FAIL")
                    self.assertEqual(ledger.events("t1", "delivery.intent"), [])
                    self.assertEqual(status(ledger, "t1")["adjudication"], "FAIL")
                note = json.loads((self.root / f"{name}.anchors.jsonl").read_text(encoding="utf-8"))
                self.assertEqual(note["note"], "t1 UNKNOWN")

    def test_existing_delivery_target_reports_unknown(self):
        target = self.root / "occupied-out" / "t1"
        target.mkdir(parents=True)
        code, out, err, path, _, _ = self.run_case("occupied", deliver=True)
        self.assertEqual(code, 2, out + err)
        self.assertIn("未交付", out)
        self.assertIn("delivery target already exists", out)
        self.assertEqual(list(target.iterdir()), [])
        with Ledger(path) as ledger:
            self.assertEqual(ledger.last("t1", "verdict").payload["verdict"], "PASS")
            self.assertEqual(ledger.last("t1", "gate.adjudication").payload["verdict"], "PASS")
            self.assertEqual(ledger.events("t1", "delivery.intent"), [])

    def test_happy_cli_records_gate_with_and_without_delivery(self):
        for deliver in (False, True):
            with self.subTest(deliver=deliver):
                code, out, err, path, public, log = self.run_case(f"ok-{deliver}", deliver=deliver)
                self.assertEqual(code, 0, out + err)
                self.assertIn("t1: PASS", out)
                self.assertNotIn("未交付", out)
                with Ledger(path) as ledger:
                    gate = ledger.last("t1", "gate.adjudication")
                    self.assertIsNotNone(gate)
                    self.assertEqual(gate.payload["verdict"], "PASS")
                    self.assertEqual(len(ledger.events("t1", "verdict")), 1)
                    if deliver:
                        self.assertTrue(ledger.last("t1", "delivery.result").payload["post_verified"])
                    else:
                        self.assertEqual(ledger.events("t1", "delivery.intent"), [])
                self.assertEqual(obs.check_log(log, public), [])

    def test_failed_post_delivery_verification_reports_unknown(self):
        from m2k.kernel import shutil
        copy = shutil.copy2

        def corrupt(source, destination, *args, **kwargs):
            result = copy(source, destination, *args, **kwargs)
            Path(destination).write_text("incomplete delivery\n", encoding="utf-8")
            return result

        with patch("m2k.kernel.shutil.copy2", side_effect=corrupt):
            code, out, err, path, _, _ = self.run_case("post-verify", deliver=True)
        self.assertEqual(code, 2, out + err)
        self.assertIn("未交付", out)
        self.assertIn("post-verification failed", out)
        with Ledger(path) as ledger:
            self.assertIsNone(ledger.last("t1", "delivery.result"))
            self.assertIn("post-verification failed", ledger.last("t1", "delivery.refused").payload["reason"])
            self.assertFalse((self.root / "post-verify-out" / "t1").exists())
            self.assertFalse((self.root / "post-verify-out" / ".m2k-staging-t1").exists())
            self.assertEqual(ledger.last("t1", "gate.adjudication").payload["verdict"], "PASS")

    def test_cli_adjudication_reconciles_the_configured_log(self):
        code, _, _, _, _, log = self.run_case("adjudicate")
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        rows.pop()
        self.rewrite_log(log, rows)
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(self.root / "m2k.toml"), "adjudicate", "t1"])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("missing or different in log", out.getvalue())

    def config(self, section=None):
        config, task = self.root / "m2k.toml", self.root / "task.toml"
        contract = self.contract()
        task.write_text('[task]\nid = "t1"\ngoal = "write calc"\nworkspace = "ws/t1"\n'
                        '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n', encoding="utf-8")
        text = 'ledger = "cli.sqlite"\n[sandbox]\nbackend = "none"\n[models.worker]\n[models.reviewer]\n'
        if isinstance(section, dict):
            text += "[obs]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in section.items())
        elif section is not None:
            text = f"obs = {json.dumps(section)}\n" + text
        config.write_text(text, encoding="utf-8")
        return ["--config", str(config), "run", "--untrusted", str(task)], contract

    def invoke(self, command):
        with patch("m2k.cli.build_model", side_effect=[ScriptedModel(GOOD, "w"), ScriptedModel([review("GO")], "r")]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(command)

    def test_run_starts_real_witness_uses_config_relative_paths_and_closes_it(self):
        _, public, key, log = self.keys()
        command, _ = self.config({"key": key.name, "log": log.name, "public_key_b64": public})
        real_client = obs.ObsClient
        clients = []

        def start(*args, **kwargs):
            client = real_client(*args, **kwargs)
            clients.append(client)
            return client

        with patch("m2k.cli.obs.ObsClient", side_effect=start):
            self.assertEqual(self.invoke(command), 0)
        self.assertIsNotNone(clients[0].process.poll())
        with Ledger(self.root / "cli.sqlite") as ledger:
            self.assertEqual(adjudicate(ledger, "t1", obs_public_key=public).verdict, "PASS")
            self.assertEqual(len(ledger.events("t1", "obs.receipt")), 6)
        self.assertEqual(obs.check_log(log, public), [])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--config", str(self.root / "m2k.toml"), "status", "t1"]), 0)
        self.assertEqual(json.loads(out.getvalue())["obs_receipts"], 6)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.root / "m2k.toml"), "adjudicate", "t1"]), 0)

    def test_run_refuses_inside_workspace_key_inside_workspace_log_and_wrong_key(self):
        _, public, key, log = self.keys()
        ws = self.root / "ws" / "t1"
        cases = ({"key": str(ws / "key.pem"), "log": str(log), "public_key_b64": public},
                 {"key": str(key), "log": str(ws / "obs.jsonl"), "public_key_b64": public},
                 {"key": str(key), "log": str(log), "public_key_b64": trust.generate_keypair()[1]})
        for section in cases:
            with self.subTest(section=section), patch("m2k.cli.Kernel") as kernel, \
                    patch("m2k.cli.obs.ObsClient") as client:
                command, _ = self.config(section)
                with self.assertRaises(SystemExit) as stopped: self.invoke(command)
                self.assertIn("OBS", str(stopped.exception))
                kernel.assert_not_called()
                client.assert_not_called()

    def test_without_obs_warns_and_records_null(self):
        command, _ = self.config()
        err = io.StringIO()
        with patch("m2k.cli.build_model", side_effect=[ScriptedModel(GOOD, "w"), ScriptedModel([review("GO")], "r")]), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(main(command), 0)
        self.assertIn("警告：未配置 OBS 独立旁证，交付只凭账本裁决。", err.getvalue())
        with Ledger(self.root / "cli.sqlite") as ledger:
            self.assertIsNone(ledger.last("t1", "task.submitted").payload["obs"])
            self.assertEqual(ledger.events("t1", "obs.receipt"), [])

    def test_run_rejects_malformed_obs_before_opening_ledger(self):
        _, public, key, log = self.keys()
        valid = {"key": key.name, "log": log.name, "public_key_b64": public}
        sections = [{k: v for k, v in valid.items() if k != missing} for missing in valid]
        sections += [dict(valid, **{field: value}) for field in valid for value in (1, [], {})]
        sections += [[], "wrong type", True]
        for section in sections:
            with self.subTest(section=section):
                command, _ = self.config(section)
                err = io.StringIO()
                with patch("m2k.cli._ledger") as ledger, \
                        patch("m2k.cli.build_model", side_effect=[ScriptedModel(GOOD, "w"),
                                                                 ScriptedModel([review("GO")], "r")]), \
                        redirect_stdout(io.StringIO()), redirect_stderr(err):
                    self.assertEqual(main(command), 2)
                self.assertIn("配置错误：[obs]", err.getvalue())
                ledger.assert_not_called()
                from tests.test_startup_refusals import assert_refusal
                assert_refusal(self, self.root / "cli.sqlite", "t1", "OBS_CONFIG")

    def test_run_surfaces_programming_errors_outside_obs_setup(self):
        _, public, key, log = self.keys()
        command, _ = self.config({"key": key.name, "log": log.name, "public_key_b64": public})
        for error in (KeyError("programming error"), TypeError("programming error")):
            with self.subTest(error=type(error).__name__), patch("m2k.cli.Kernel.run", side_effect=error):
                with self.assertRaises(type(error)) as raised:
                    self.invoke(command)
                self.assertIs(raised.exception, error)

    def test_keygen_prints_usable_block_refuses_nonempty_directory_and_restricts_permissions(self):
        folder = self.root / "new-obs"
        out = io.StringIO()
        with redirect_stdout(out): self.assertEqual(main(["obs", "keygen", "--out", str(folder)]), 0)
        section = tomllib.loads(out.getvalue())["obs"]
        self.assertEqual(trust.public_key_b64(trust.load_private_key(section["key"])), section["public_key_b64"])
        self.assertEqual(Path(section["key"]).name, "obs-key.pem")
        if os.name != "nt": self.assertEqual(Path(section["key"]).stat().st_mode & 0o777, 0o600)
        before = Path(section["key"]).read_bytes()
        with redirect_stderr(io.StringIO()): self.assertEqual(main(["obs", "keygen", "--out", str(folder)]), 1)
        self.assertEqual(Path(section["key"]).read_bytes(), before)


class ObsTreeTest(WitnessFiles, TreeCase):
    def test_parent_reconciles_the_logs_of_already_merged_children(self):
        observer, _, public, log = self.client()
        contract = self.parent(subs=(sub("a", ["a.py"], "check_a.py"),), check_all=CHECK_A)
        report = Kernel(self.ledger, ScriptedModel(good_a(), "w"), ScriptedModel([review("GO")] * 2, "r"),
                        obs=observer).run(contract)
        self.assertEqual(report.reported_verdict.value, "PASS")
        observer.close()
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        child_row = next(row for row in rows if row["request"]["task_id"] == "p1.a")
        child_row["request"]["kind"] = "changed"
        self.rewrite_log(log, rows)
        self.assertEqual(obs.check_log(log), [])
        result = adjudicate(self.ledger, "p1", obs_public_key=public, obs_log=log)
        self.assertEqual(result.verdict, "FAIL")
        self.assertIn("subtask a does not adjudicate PASS", " ".join(result.reasons))

    def test_witness_death_at_child_verdict_never_merges_or_reassigns(self):
        observer, _, _, _ = self.client()
        witness = observer.witness

        def die(event, ledger_id):
            if event.task_id == "p1.a" and event.kind == "verdict":
                observer.process.kill()
                observer.process.wait()
            return witness(event, ledger_id)

        first = replace(sub("a", ["a.py"], "check_a.py"), max_reassign=1)
        contract = self.parent(subs=(first, sub("b", ["b.py"], "check_b.py")))
        worker = ScriptedModel(good_a() * 2 + good_b(), "w")
        with patch.object(observer, "witness", side_effect=die):
            report = Kernel(self.ledger, worker, ScriptedModel([review("GO")] * 4, "r"), obs=observer).run(contract)
        self.assertEqual(self.ledger.last("p1.a", "verdict").payload["verdict"], "PASS")
        self.assertEqual(self.merges("p1"), [])
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(self.ledger.events("p1.a.e2"), [])
        self.assertEqual(self.ledger.events("p1.b"), [])
        self.assertEqual(report.verdict.value, "UNKNOWN", report.reasons)
        self.assertIn("adjudication FAIL:", report.worker.reason)
        self.assertIn("OBS_UNAVAILABLE:", " ".join(report.reasons))
        self.assertEqual(self.ledger.events("p1", "delivery.intent"), [])
        self.assertFalse((contract.workspace / "a.py").exists())

    def test_observed_task_and_adjudication_with_one_subtask_never_read_all_events(self):
        self.ledger.append("unrelated", "first", {})
        observer, _, public, log = self.client()
        setup, _ = trust.dev_setup(self.root / "trust", CAPS)
        contract = self.parent(subs=(sub("a", ["a.py"], "check_a.py", tokens=40000),),
                               check_all=CHECK_A, budget=BUDGET)
        events, whole_reads = self.ledger.events, []
        read_bytes, reads = Path.read_bytes, []

        def read(path):
            if path == log:
                reads.append(path)
            return read_bytes(path)

        def tracked(task_id=None, kind=None):
            if task_id is None:
                whole_reads.append(kind)
            return events(task_id, kind)

        with patch.object(self.ledger, "events", side_effect=tracked), patch.object(Path, "read_bytes", read):
            result = Kernel(self.ledger, ScriptedModel(good_a(), "w"), ScriptedModel([review("GO")] * 2, "r"),
                            trust=setup, obs=observer).run(contract)
            self.assertEqual(len(reads), 2)  # child merge Gate, then parent release Gate (including its child)
            decision = adjudicate(self.ledger, contract.task_id, obs_public_key=public, obs_log=log)
            self.assertEqual(len(reads), 3)
        self.assertEqual(result.verdict.value, "PASS", result.reasons)
        self.assertIsNotNone(result.delivered_to)
        self.assertEqual(decision.verdict, "PASS", decision.reasons)
        self.assertEqual(len(whole_reads), 0)

    def test_parent_and_every_subtask_have_receipts_and_use_the_same_configured_key(self):
        observer, _, public, log = self.client()
        setup, _ = trust.dev_setup(self.root / "trust", CAPS)
        contract = self.parent(subs=(sub("a", ["a.py"], "check_a.py", tokens=40000),
                                     sub("b", ["b.py"], "check_b.py", tokens=40000)), budget=BUDGET)
        result = Kernel(self.ledger, ScriptedModel(good_a() + good_b(), "w"),
                        ScriptedModel([review("GO")] * 3, "r"), trust=setup, obs=observer).run(contract)
        self.assertEqual(result.verdict.value, "PASS", result.reasons)
        self.assertIsNotNone(result.delivered_to)
        for tid in ("p1", "p1.a", "p1.b"):
            self.assertEqual(len(self.ledger.events(tid, "obs.receipt")), 8 if tid == "p1" else 6)
            self.assertEqual(len(self.ledger.events(tid, "anchor.checkpoint")), 1 if tid == "p1" else 0)
            self.assertEqual(adjudicate(self.ledger, tid, obs_public_key=public).verdict, "PASS")
        child = self.ledger.last("p1.a", "verdict")
        self.ledger.append("p1.a", "verdict", child.payload)
        self.assertEqual(adjudicate(self.ledger, "p1").verdict, "PASS")
        result = adjudicate(self.ledger, "p1", obs_public_key=public)
        self.assertEqual(result.verdict, "FAIL")
        self.assertIn("subtask a does not adjudicate PASS", " ".join(result.reasons))
        self.assertEqual(obs.check_log(log, public), [])

    def test_witness_failure_in_child_prevents_merge_and_parent_delivery(self):
        observer, _, _, _ = self.client()

        def stop_witness(messages):
            observer.close()
            return step("finish")

        result = Kernel(self.ledger, ScriptedModel([good_a()[0], stop_witness], "w"),
                        ScriptedModel([review("GO")] * 3, "r"), obs=observer).run(self.parent())
        self.assertEqual(result.verdict.value, "UNKNOWN", result.reasons)
        self.assertEqual(self.ledger.last("p1.a", "verdict").payload["verdict"], "UNKNOWN")
        self.assertEqual(self.merges("p1"), [])
        self.assertIsNone(result.delivered_to)
        self.assertIn("OBS_UNAVAILABLE:", " ".join(result.reasons))


class ReleaseWitnessTest(WitnessFiles, KernelCase):
    def release_request(self, observer, version=2, slot="a", manifest_id="a2", revocations_version=3,
                        root="test-root", domain="TEST_ONLY"):
        release = {"root": root, "domain": domain, "slot": slot, "version": version,
                   "manifest_id": manifest_id, "revocations_version": revocations_version}
        event = self.ledger.append("m2k.startup", "release.checked", {"release": release})
        return observer.witness(event, self.ledger.ledger_id)

    def assert_refused_without_logging(self, observer, log, reason, **changes):
        before = log.read_bytes()
        with self.assertRaises(obs.ObsRefused) as refused:
            self.release_request(observer, **changes)
        self.assertEqual(str(refused.exception), reason)
        self.assertEqual(log.read_bytes(), before)
        self.assertIsNone(observer.process.poll())

    def test_versions_slots_roots_domains_conflicts_and_null_on_one_live_witness(self):
        observer, _, public, log = self.client()
        first = self.release_request(observer)
        self.assertEqual(first["body"]["release"], {"root": "test-root", "domain": "TEST_ONLY", "slot": "a",
                                                  "version": 2, "manifest_id": "a2", "revocations_version": 3})
        self.release_request(observer)
        self.release_request(observer, version=1, slot="b", manifest_id="b1", revocations_version=None)
        self.assert_refused_without_logging(observer, log, "release rollback", version=1, manifest_id="a1")
        self.assert_refused_without_logging(observer, log, "two manifests share slot a version 2", manifest_id="different")
        self.assert_refused_without_logging(observer, log, "revocations rollback", slot="b", manifest_id="b2", revocations_version=2)
        self.release_request(observer, version=1, slot="b", manifest_id="b1")
        self.release_request(observer, revocations_version=None)
        self.assert_refused_without_logging(observer, log, "revocations rollback", revocations_version=2)
        other, _, other_public, other_log = self.client("other-root")
        self.release_request(other, version=1, manifest_id="other-root", root="another-root", revocations_version=0)
        self.assertEqual(obs.check_log(other_log, other_public), [])
        self.release_request(observer, version=1, manifest_id="other-domain", domain="PRODUCTION", revocations_version=0)
        self.assertEqual(obs.check_log(log, public), [])

    def test_first_root_is_per_domain_and_precedes_version_checks_without_logging(self):
        observer, _, public, log = self.client()
        first, second = [trust.fingerprint(trust.generate_keypair()[0].public_key()) for _ in range(2)]
        self.release_request(observer, root=first)
        reason = f"root changed: TEST_ONLY first seen {first}, now {second}"
        self.assert_refused_without_logging(observer, log, reason, root=second, version=1, revocations_version=0)
        self.assert_refused_without_logging(observer, log, reason, root=second, slot="new-slot")
        self.release_request(observer, root=second, domain="PRODUCTION")
        self.assert_refused_without_logging(observer, log,
                                           f"root changed: PRODUCTION first seen {second}, now {first}",
                                           root=first, domain="PRODUCTION")
        self.assertEqual(obs.root_memory(log, public), {"TEST_ONLY": first, "PRODUCTION": second})
        observer.close()
        restarted = obs.ObsClient(self.command(self.root / "obs.pem", log), public)
        self.addCleanup(restarted.close)
        self.assert_refused_without_logging(restarted, log, reason, root=second)
        self.assertEqual(obs.root_memory(log, public), {"TEST_ONLY": first, "PRODUCTION": second})

    def test_restart_keeps_first_root_and_ignores_other_roots_in_legacy_signed_rows(self):
        observer, key, public, log = self.client()
        first, second = [trust.fingerprint(trust.generate_keypair()[0].public_key()) for _ in range(2)]
        self.release_request(observer, root=first)
        observer.close()
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        old = rows[0]
        release = old["request"]["release"] | {"root": second, "version": 99, "revocations_version": 99}
        event = self.ledger.append("m2k.startup", "release.checked", {"release": release})
        request = obs.request(event, self.ledger.ledger_id)
        rows.append(old | {"seq": 2, "request": request,
                           "receipt": trust.sign(old["receipt"]["body"] | request | {"obs_seq": 2}, key)})
        self.resign_log(log, rows, key)   # a valid pre-T65 log, including a signed tail
        self.assertEqual(obs.check_log(log, public), [])
        self.assertEqual(obs.root_memory(log, public), {"TEST_ONLY": first})
        restarted = obs.ObsClient(self.command(self.root / "obs.pem", log), public)
        self.addCleanup(restarted.close)
        self.assert_refused_without_logging(restarted, log,
                                           f"root changed: TEST_ONLY first seen {first}, now {second}", root=second)
        self.release_request(restarted, root=first)
        self.assert_refused_without_logging(restarted, log, "release rollback", root=first, version=1)
        self.assertEqual(obs.root_memory(log, public), {"TEST_ONLY": first})

    def test_root_memory_verifies_every_signature_and_never_returns_partial_memory(self):
        observer, key, public, log = self.client()
        self.assertEqual(obs.root_memory(log, public), {})
        self.release_request(observer)
        self.release_request(observer)
        observer.close()
        with self.assertRaises(obs.ObsError):
            obs.root_memory(log, trust.generate_keypair()[1])
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        rows[0]["receipt"]["signature_b64"] = "invalid"
        self.rewrite_log(log, rows)
        rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        tail = rows[-1]
        tail["receipt"] = trust.sign(tail["receipt"]["body"] | {"obs_prev": tail["prev"]}, key)
        self.rewrite_log(log, rows)
        self.assertEqual(obs.check_log(log), [])
        with self.assertRaisesRegex(obs.ObsError, "OBS log line 1"):
            obs.root_memory(log, public)

    def test_restart_rebuilds_versions_and_revocations_across_all_slots(self):
        observer, _, public, log = self.client()
        self.release_request(observer)
        self.release_request(observer, slot="b", version=1, manifest_id="b1", revocations_version=None)
        observer.close()
        restarted = obs.ObsClient(self.command(self.root / "obs.pem", log), public)
        self.addCleanup(restarted.close)
        self.release_request(restarted)
        self.assert_refused_without_logging(restarted, log, "release rollback", version=1, manifest_id="a1")
        self.assert_refused_without_logging(restarted, log, "two manifests share slot a version 2", manifest_id="other")
        self.assert_refused_without_logging(restarted, log, "revocations rollback", slot="c", version=1,
                                           manifest_id="c1", revocations_version=2)
        self.assertEqual(obs.check_log(log, public), [])

    def test_release_shape_is_checked_and_the_signed_receipt_binds_the_whole_release(self):
        observer, key, public, log = self.client()
        receipt = self.release_request(observer)
        good = receipt["body"]["release"]
        for changes in ({"version": True}, {"version": 0}, {"slot": "INVALID"}, {"revocations_version": False},
                        {"revocations_version": -1}, {"domain": "other"}, {"manifest_id": ""}, {"extra": 1}):
            event = self.ledger.append("m2k.startup", "release.checked", {"release": good | changes})
            before = log.read_bytes()
            with self.subTest(changes=changes), self.assertRaisesRegex(obs.ObsRefused, "malformed release.checked"):
                observer.witness(event, self.ledger.ledger_id)
            self.assertEqual(log.read_bytes(), before)
        row = json.loads(log.read_bytes())
        request = row["request"] | {"release": good | {"manifest_id": "different"}}
        with self.assertRaisesRegex(obs.ObsError, "notary or request"):
            obs.receipt_body(receipt, public, request)
        observer.close()
        row["receipt"] = trust.sign(receipt["body"] | {"release": good | {"version": 3}}, key)
        log.write_bytes(trust.canonical(row) + b"\n")
        self.assertIn("notary or request", " ".join(obs.check_log(log, public)))
