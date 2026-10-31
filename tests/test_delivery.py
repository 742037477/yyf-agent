"""T47: real witnesses, portable receipts, staged release and the CI release gate."""
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from m2k import anchor, obs, trust
from m2k.adjudicate import adjudicate
from m2k.cli import main
from m2k.delivery import SUFFIX, check_delivery
from m2k.kernel import Kernel, KernelError, status
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from tests import test_release as release_tests
from tests.helpers import KernelCase, review, step
from tests.test_obs import GOOD, WitnessFiles


class DeliveryTest(WitnessFiles, KernelCase):
    def setUp(self):
        super().setUp()
        self.observer, self.obs_key, self.public, self.log = self.client()
        self.out = self.root / "out"

    def run_delivery(self, tid="t1", **kwargs):
        contract = self.contract(task_id=tid)
        report = Kernel(self.ledger, ScriptedModel(GOOD, "w"), ScriptedModel([review("GO")], "r"),
                        obs=self.observer, **kwargs).run(contract)
        return contract, report

    def check(self, full):
        return check_delivery(self.out, self.public, **({"ledger": self.ledger, "obs_log": self.log} if full else {}))

    def cli_check(self, full):
        config = self.root / "check.toml"
        config.write_text('ledger = "ledger.sqlite"\n[obs]\nlog = "obs.jsonl"\npublic_key_b64 = '
                          + json.dumps(self.public) + '\n', encoding="utf-8")
        args = (["--config", str(config)] if full else []) + ["check-delivery", str(self.out)]
        if not full:
            args += ["--standalone", "--public-key", self.public]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    def assert_problem(self, entry, *reasons):
        for full in (False, True):
            with self.subTest(full=full, entry=entry):
                result = self.check(full)
                problems = "; ".join(result[entry]["problems"])
                self.assertTrue(problems, result)
                for reason in reasons:
                    self.assertIn(reason, problems)
                code, out, err = self.cli_check(full)
                self.assertEqual(code, 1, out + err)
                self.assertIn(f"未经核验 {entry}:", out)
                for reason in reasons:
                    self.assertIn(reason, out)

    def test_happy_portable_receipt_and_exact_event(self):
        _, report = self.run_delivery()
        self.assertEqual(report.reported_verdict.value, "PASS", report.reasons)
        event = self.ledger.last("t1", "delivery.result")
        doc = json.loads((self.out / ("t1" + SUFFIX)).read_bytes())
        self.assertEqual(doc["event"], asdict(event))
        self.assertEqual(doc["ledger_id"], self.ledger.ledger_id)
        self.assertEqual(event.payload, {"target": str(self.out / "t1"), "candidate": report.candidate.digest,
                                        "files": report.candidate.files, "landed": report.candidate.digest,
                                        "post_verified": True})
        receipt = next(e for e in self.ledger.events("t1", "obs.receipt")
                       if e.payload["receipt"]["body"]["ledger_seq"] == event.seq)
        self.assertEqual(doc["receipt"], receipt.payload["receipt"])
        self.assertGreater(self.ledger.last("t1", "anchor.checkpoint").seq, receipt.seq)
        self.assertEqual(status(self.ledger, "t1")["delivery_receipt"], doc["receipt"]["body"]["obs_seq"])
        self.assertEqual((self.out / ("t1" + SUFFIX)).read_bytes(), trust.canonical(doc))
        for full in (False, True):
            results = self.check(full)
            self.assertEqual(set(results), {"t1", "t1" + SUFFIX})
            self.assertTrue(all(r == {"files": len(report.candidate.files), "problems": []} for r in results.values()), results)
            code, out, err = self.cli_check(full)
            self.assertEqual(code, 0, out + err)
            self.assertIn(f"OK t1 ({len(report.candidate.files)} files)", out)
        self.assertEqual(adjudicate(self.ledger, "t1", obs_public_key=self.public, obs_log=self.log).verdict, "PASS")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.root / "check.toml"), "adjudicate", "t1"]), 0)
        moved = self.root / "portable"
        shutil.copytree(self.out, moved)
        self.assertTrue(all(not r["problems"] for r in check_delivery(moved, self.public).values()))

    def test_missing_extra_and_changed_files_are_named_in_both_modes(self):
        self.run_delivery()
        path = self.out / "t1" / "calc.py"
        original = path.read_bytes()
        path.write_bytes(b"changed\n")
        self.assert_problem("t1", "changed file: calc.py")
        path.unlink()
        self.assert_problem("t1", "missing file: calc.py")
        path.write_bytes(original)
        extra = self.out / "t1" / ".m2k" / "extra.txt"
        extra.parent.mkdir()
        extra.write_text("extra", encoding="utf-8")
        self.assert_problem("t1", "extra file: .m2k/extra.txt")

    def test_planted_root_entries_and_staging_are_reported(self):
        self.run_delivery()
        (self.out / "fake").mkdir()
        (self.out / "stray.txt").write_text("stray", encoding="utf-8")
        (self.out / ".m2k-staging-leftover").mkdir()
        (self.out / ("orphan" + SUFFIX)).write_text("{}", encoding="utf-8")
        for entry, reason in (("fake", "missing receipt"), ("stray.txt", "not a delivery folder"),
                              (".m2k-staging-leftover", "staging leftover"), ("orphan" + SUFFIX, "missing delivery folder")):
            self.assert_problem(entry, reason)

    def test_copied_real_receipt_does_not_verify_a_fake_folder(self):
        self.run_delivery()
        (self.out / "fake").mkdir()
        (self.out / "fake" / "fake.txt").write_text("fake", encoding="utf-8")
        shutil.copyfile(self.out / ("t1" + SUFFIX), self.out / ("fake" + SUFFIX))
        self.assert_problem("fake", "task name mismatch", "target name mismatch", "missing file: calc.py", "extra file: fake.txt")

    def test_edited_event_wrong_signer_and_malformed_receipts_fail(self):
        self.run_delivery()
        path = self.out / ("t1" + SUFFIX)
        original = path.read_bytes()
        doc = json.loads(original)
        doc["event"]["payload"]["target"] = "different/t1"
        path.write_bytes(trust.canonical(doc))
        self.assert_problem("t1", "event hash mismatch")
        doc = json.loads(original)
        doc["receipt"] = trust.sign(doc["receipt"]["body"], trust.generate_keypair()[0])
        path.write_bytes(trust.canonical(doc))
        self.assert_problem("t1", "signature does not verify")
        for raw in (b"[]", b"{}", b"not-json", b'{"schema":1,"schema":2}'):
            path.write_bytes(raw)
            self.assert_problem("t1", "invalid delivery receipt")

    def test_symlink_inside_delivery_and_at_root_is_never_followed(self):
        self.run_delivery()
        external = self.root / "external.txt"
        external.write_text("outside", encoding="utf-8")
        link = self.out / "t1" / "link.txt"
        try:
            link.symlink_to(external)
        except OSError:
            self.skipTest("symlinks unavailable")
        original = Path.read_bytes

        def read(path):
            self.assertNotIn(path, (link, external))
            return original(path)

        with patch.object(Path, "read_bytes", read):
            self.assert_problem("t1", "symlink or special file: link.txt")
        link.unlink()
        (self.out / "linked").symlink_to(self.out / "t1", target_is_directory=True)
        self.assert_problem("linked", "symlink")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX special files")
    def test_special_file_is_not_opened(self):
        self.run_delivery()
        os.mkfifo(self.out / "t1" / "pipe")
        self.assert_problem("t1", "special file: pipe")
        os.mkfifo(self.out / "root-pipe")
        self.assert_problem("root-pipe", "special file")

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_directory_junction_and_linked_root_are_never_followed(self):
        self.run_delivery()
        external = self.root / "external"
        external.mkdir()
        (external / "secret.txt").write_text("outside", encoding="utf-8")
        junction = self.out / "t1" / "junction"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(external)], capture_output=True)
        if result.returncode:
            self.skipTest("junctions unavailable")
        original = Path.read_bytes

        def read(path):
            self.assertFalse(path.is_relative_to(junction))
            return original(path)

        with patch.object(Path, "read_bytes", read):
            self.assert_problem("t1", "symlink or special file: junction")
            with self.assertRaisesRegex(ValueError, "symlink delivery root"):
                check_delivery(junction, self.public)

    def test_delivery_name_may_end_with_receipt_suffix(self):
        tid = "named" + SUFFIX
        _, report = self.run_delivery(tid)
        self.assertEqual(report.reported_verdict.value, "PASS", report.reasons)
        for full in (False, True):
            self.assertTrue(all(not r["problems"] for r in self.check(full).values()))

    def test_cli_pair_is_printed_once(self):
        _, report = self.run_delivery()
        for full in (False, True):
            code, out, err = self.cli_check(full)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(out.splitlines(), [f"OK t1 ({len(report.candidate.files)} files)",
                                                "合计 1 份：通过 1 份，未经核验 0 份"])

    def test_cli_mixed_entries_count_pairs_orphans_and_staging(self):
        self.run_delivery()
        self.run_delivery("changed")
        (self.out / "changed" / "calc.py").write_text("changed\n", encoding="utf-8")
        (self.out / "hand-made").mkdir()
        (self.out / ".m2k-staging-leftover").mkdir()
        (self.out / ("orphan" + SUFFIX)).write_text("{}", encoding="utf-8")
        expected = ["未经核验 .m2k-staging-leftover: staging leftover",
                    "未经核验 changed: changed file: calc.py",
                    "未经核验 hand-made: missing receipt file",
                    f"未经核验 orphan{SUFFIX}: missing delivery folder",
                    "OK t1 (2 files)", "合计 5 份：通过 1 份，未经核验 4 份"]
        for full in (False, True):
            code, out, err = self.cli_check(full)
            self.assertEqual(code, 1, out + err)
            self.assertEqual(out.splitlines(), expected)

    def test_cli_empty_root_and_task_name_with_receipt_suffix(self):
        self.out.mkdir()
        for full in (False, True):
            code, out, err = self.cli_check(full)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(out, "OK empty delivery root\n")
        tid = "named" + SUFFIX
        self.run_delivery(tid)
        for full in (False, True):
            code, out, err = self.cli_check(full)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(out.splitlines(), [f"OK {tid} (2 files)",
                                                "合计 1 份：通过 1 份，未经核验 0 份"])

    def test_ledger_event_and_obs_line_mismatches_are_reported(self):
        self.run_delivery()
        path = self.out / ("t1" + SUFFIX)
        original = path.read_bytes()
        doc = json.loads(original)
        doc["event"]["seq"] = self.ledger.last("t1", "verdict").seq
        doc["receipt"] = trust.sign(doc["receipt"]["body"] | {"ledger_seq": doc["event"]["seq"]}, self.obs_key)
        path.write_bytes(trust.canonical(doc))
        self.assertFalse(self.check(False)["t1"]["problems"])
        self.assertIn("event mismatch", "; ".join(self.check(True)["t1"]["problems"]))
        path.write_bytes(original)
        doc = json.loads(original)
        doc["receipt"] = trust.sign(doc["receipt"]["body"] | {"obs_seq": 999}, self.obs_key)
        path.write_bytes(trust.canonical(doc))
        self.assertFalse(self.check(False)["t1"]["problems"])
        self.assertIn("different in OBS log", "; ".join(self.check(True)["t1"]["problems"]))

    def test_two_deliveries_share_one_chain_check_and_one_obs_log_read(self):
        self.run_delivery()
        self.run_delivery("second")
        calls, reads = [], []
        events, read_bytes = self.ledger.events, Path.read_bytes

        def scoped(task_id=None, kind=None):
            calls.append(task_id)
            return events(task_id, kind)

        def read(path):
            if path == self.log:
                reads.append(path)
            return read_bytes(path)

        with patch.object(self.ledger, "events", side_effect=scoped), \
                patch.object(self.ledger, "verify_chain", wraps=self.ledger.verify_chain) as chain, \
                patch.object(Path, "read_bytes", read):
            results = self.check(True)
        self.assertTrue(all(not r["problems"] for r in results.values()), results)
        self.assertEqual(len(results), 4)
        self.assertNotIn(None, calls)
        self.assertEqual(reads, [self.log])
        chain.assert_called_once_with()

    def test_overlap_in_both_directions_refused_before_recording(self):
        contract = self.contract()
        for target in (contract.workspace, contract.workspace / "out", contract.workspace.parent):
            with self.subTest(target=target), self.assertRaisesRegex(KernelError, "overlaps the workspace"):
                Kernel(self.ledger, ScriptedModel([], "w"), ScriptedModel([], "r")).run(replace(contract, deliver_to=target))
            self.assertEqual(self.ledger.events("t1"), [])

    def test_preexisting_target_receipt_or_stage_is_preserved(self):
        for index, name in enumerate(("t0", "t1" + SUFFIX, ".m2k-staging-t2")):
            self.out.mkdir(exist_ok=True)
            path = self.out / name
            path.write_text("keep", encoding="utf-8")
            _, report = self.run_delivery(f"t{index}")
            self.assertEqual(report.reported_verdict.value, "UNKNOWN")
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")
            self.assertIsNone(self.ledger.last(f"t{index}", "delivery.intent"))
            self.assertIsNone(self.ledger.last(f"t{index}", "delivery.result"))

    def test_receipt_or_folder_rename_failure_refuses_and_removes_created_files(self):
        rename = Path.rename
        for tid, fail_folder in (("receipt", False), ("folder", True)):
            with self.subTest(fail_folder=fail_folder):
                def fail(path, target):
                    if (path.name.startswith(".m2k-staging-") if fail_folder else path.suffix == ".tmp"):
                        raise OSError("rename failed")
                    return rename(path, target)

                with patch.object(Path, "rename", fail):
                    _, report = self.run_delivery(tid)
                self.assertEqual(report.reported_verdict.value, "UNKNOWN")
                self.assertFalse((self.out / tid).exists())
                self.assertFalse((self.out / (".m2k-staging-" + tid)).exists())
                self.assertFalse((self.out / (tid + SUFFIX)).exists())
                self.assertFalse((self.out / (tid + SUFFIX)).with_suffix(".tmp").exists())
                self.assertIn("rename failed", self.ledger.last(tid, "delivery.refused").payload["reason"])
                self.assertNotIn("delivered_to", status(self.ledger, tid))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_receipt_write_failure_cleans_partial_file_before_recording_refusal(self):
        path_open, append = Path.open, self.ledger.append

        def fail_write(path, *args, **kwargs):
            stream = path_open(path, *args, **kwargs)
            if path.parent == self.out and path.suffix == ".tmp":
                writer = Mock(wraps=stream)
                writer.write.side_effect = OSError("receipt write failed")
                return closing(writer)
            return stream

        def record(tid, kind, *args, **kwargs):
            if kind == "delivery.refused":
                self.assertEqual(list(self.out.iterdir()), [])
            return append(tid, kind, *args, **kwargs)

        with patch.object(Path, "open", fail_write), patch.object(self.ledger, "append", side_effect=record):
            _, report = self.run_delivery("write-fails")
        self.assertEqual(report.reported_verdict.value, "UNKNOWN")
        self.assertIn("receipt write failed", self.ledger.last("write-fails", "delivery.refused").payload["reason"])
        self.assertEqual(list(self.out.iterdir()), [])

    def test_existing_temporary_receipt_is_preserved_on_refusal(self):
        self.out.mkdir()
        temporary = (self.out / ("existing" + SUFFIX)).with_suffix(".tmp")
        temporary.write_bytes(b"belongs to an earlier run")
        _, report = self.run_delivery("existing")
        self.assertEqual(report.reported_verdict.value, "UNKNOWN")
        self.assertIsNotNone(self.ledger.last("existing", "delivery.refused"))
        self.assertEqual(temporary.read_bytes(), b"belongs to an earlier run")
        self.assertEqual(list(self.out.iterdir()), [temporary])

    def test_temporary_path_reused_after_receipt_rename_is_preserved(self):
        rename = Path.rename
        receipt = self.out / ("reused" + SUFFIX)
        temporary = receipt.with_suffix(".tmp")

        def fail_folder(path, target):
            if path.name == ".m2k-staging-reused":
                temporary.write_bytes(b"created by another run")
                raise OSError("folder rename failed")
            return rename(path, target)

        with patch.object(Path, "rename", fail_folder):
            _, report = self.run_delivery("reused")
        self.assertEqual(report.reported_verdict.value, "UNKNOWN")
        self.assertFalse(receipt.exists())
        self.assertEqual(temporary.read_bytes(), b"created by another run")
        self.assertEqual(list(self.out.iterdir()), [temporary])

    def test_empty_root_and_missing_public_key(self):
        self.out.mkdir()
        self.assertEqual(self.check(False), {})
        code, out, err = self.cli_check(False)
        self.assertEqual(code, 0, out + err)
        self.assertIn("empty delivery root", out)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["check-delivery", str(self.out), "--standalone"]), 1)
        self.assertIn("requires --public-key", err.getvalue())


class DeliveryCliTest(unittest.TestCase):
    def setUp(self):
        # Reuse the existing temporary Root/config fixture without inheriting its test cases.
        self.f = release_tests.ReleaseTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        f.task.write_bytes(f.task.read_bytes().replace(b'workspace = "ws"\n', b'workspace = "ws"\ndeliver_to = "out"\n'))

    def assert_nothing_released(self, tid="t43"):
        root = self.f.root / "out"
        self.assertFalse((root / tid).exists())
        self.assertFalse((root / (tid + SUFFIX)).exists())
        self.assertFalse((root / (".m2k-staging-" + tid)).exists())

    def anchors(self, production=False):
        f = self.f
        f.task.write_bytes(f.task.read_bytes().replace(b'deliver_to = "out"\n', b''))
        f.anchors(production=production)

    def test_anchor_publish_covers_delivery_receipt_before_folder_rename(self):
        f = self.f
        self.anchors()
        publish = anchor.publish

        def observed(*args, **kwargs):
            self.assertFalse((f.root / "out/t43").exists())
            self.assertFalse((f.root / "out" / ("t43" + SUFFIX)).exists())
            self.assertTrue((f.root / "out/.m2k-staging-t43/check.py").is_file())
            with Ledger(f.ledger_path) as ledger:
                event = ledger.last("t43", "delivery.result")
                checkpoint = ledger.last("t43", "anchor.checkpoint")
                self.assertGreater(checkpoint.seq, event.seq)
                receipt = next(e for e in ledger.events("t43", "obs.receipt")
                               if e.payload["receipt"]["body"]["ledger_seq"] == event.seq)
                self.assertLess(receipt.seq, checkpoint.seq)
            return publish(*args, **kwargs)

        with patch("m2k.anchor.publish", side_effect=observed) as publish_mock:
            code, out, err = f.anchored_run()
        self.assertEqual(code, 0, out + err)
        publish_mock.assert_called_once()
        for args in (("check-delivery", f.root / "out"),
                     ("check-delivery", f.root / "out", "--standalone", "--public-key", f.obs_public),
                     ("adjudicate", "t43")):
            code, out, err = f.cli(*args)
            self.assertEqual(code, 0, out + err)
        code, out, err = f.cli("status", "t43")
        self.assertEqual(code, 0, err)
        self.assertIsInstance(json.loads(out)["delivery_receipt"], int)

    def test_g7_failure_removes_stage_and_records_no_delivery_result(self):
        copy_file = shutil.copy2

        def corrupt(source, destination, *args, **kwargs):
            result = copy_file(source, destination, *args, **kwargs)
            Path(destination).write_text("incomplete delivery", encoding="utf-8")
            return result

        with patch("m2k.kernel.shutil.copy2", side_effect=corrupt):
            code, out, err = self.f.anchored_run()
        self.assertEqual(code, 2, out + err)
        self.assertIn("UNKNOWN", out)
        self.assert_nothing_released()
        with Ledger(self.f.ledger_path) as ledger:
            self.assertIsNone(ledger.last("t43", "delivery.result"))
            self.assertIn("post-verification failed", ledger.last("t43", "delivery.refused").payload["reason"])

    def test_obs_dies_at_delivery_result_and_nothing_is_released(self):
        witness = obs.ObsClient.witness

        def die(client, event, ledger_id):
            if event.kind == "delivery.result":
                client.process.kill()
                client.process.wait()
            return witness(client, event, ledger_id)

        with patch.object(obs.ObsClient, "witness", die):
            code, out, err = self.f.anchored_run()
        self.assertEqual(code, 2, out + err)
        self.assertIn("UNKNOWN", out)
        self.assert_nothing_released()
        with Ledger(self.f.ledger_path) as ledger:
            self.assertEqual(ledger.last("t43", "obs.failed").payload["kind"], "delivery.result")
            self.assertEqual(ledger.last("t43", "verdict").payload["verdict"], "PASS")
            self.assertIsNotNone(ledger.last("t43", "delivery.refused"))
            self.assertEqual(adjudicate(ledger, "t43", obs_public_key=self.f.obs_public).verdict, "FAIL")

    def test_production_publish_failure_removes_stage(self):
        f = self.f
        self.anchors(production=True)
        with patch("m2k.anchor.publish", side_effect=OSError("unreachable remote")):
            code, out, err = f.anchored_run()
        self.assertEqual(code, 2, out + err)
        self.assert_nothing_released()
        with Ledger(f.ledger_path) as ledger:
            self.assertIn("ANCHOR_UNAVAILABLE", ledger.last("t43", "delivery.refused").payload["reason"])
            self.assertIsNotNone(ledger.last("t43", "delivery.result"))

    def test_test_only_publish_failure_warns_and_delivers_with_receipt(self):
        f = self.f
        self.anchors()
        with patch("m2k.anchor.publish", side_effect=OSError("unreachable remote")):
            code, out, err = f.anchored_run()
        self.assertEqual(code, 0, out + err)
        self.assertIn("警告：外部锚点未发布", err)
        self.assertTrue((f.root / "out/t43").is_dir())
        self.assertTrue((f.root / "out" / ("t43" + SUFFIX)).is_file())
        self.assertEqual(f.cli("check-delivery", f.root / "out")[0], 0)

    def test_revoking_manifest_after_delivery_only_fails_full_check(self):
        f = self.f
        self.assertEqual(f.anchored_run()[0], 0)
        revocations = f.setup.revocations()["body"] | {"version": 2, "revoked_releases": ["test-release"]}
        (f.keys / "revocations.json").write_bytes(trust.canonical(trust.sign(revocations, f.root_key)))
        code, out, err = f.cli("check-delivery", f.root / "out")
        self.assertEqual(code, 1, out + err)
        self.assertIn("adjudication FAIL", out)
        self.assertIn("RELEASE_REVOKED", out)
        code, out, err = f.cli("check-delivery", f.root / "out", "--standalone", "--public-key", f.obs_public)
        self.assertEqual(code, 0, out + err)

    def test_planted_entry_warns_but_does_not_block_delivery(self):
        f = self.f
        (f.root / "out/fake").mkdir(parents=True)
        with patch("m2k.delivery.freeze_one", side_effect=AssertionError("structural scan must not hash")):
            code, out, err = f.anchored_run()
        self.assertEqual(code, 0, out + err)
        self.assertIn("警告：交付目录里有 1 项未经核验的产出", err)
        self.assertTrue((f.root / "out/t43").is_dir())

    def test_structural_warning_also_runs_when_existing_target_refuses_delivery(self):
        f = self.f
        (f.root / "out/t43").mkdir(parents=True)
        code, out, err = f.anchored_run()
        self.assertEqual(code, 2, out + err)
        self.assertIn("警告：交付目录里有 1 项未经核验的产出", err)
        with Ledger(f.ledger_path) as ledger:
            self.assertIsNone(ledger.last("t43", "delivery.intent"))

    def test_untrusted_delivery_warns_and_checker_marks_it_unverified(self):
        f = self.f
        f.config.write_bytes(f.config.read_bytes().split(b"[trust]", 1)[0])
        with patch("m2k.cli.build_model", side_effect=[ScriptedModel([step("finish")], "w"), ScriptedModel([review("GO")], "r")]):
            code, out, err = f.cli("run", "--untrusted", f.task)
        self.assertEqual(code, 0, out + err)
        self.assertIn("警告：本次交付没有回执（未配置 OBS），m2k check-delivery 会把它列为未经核验。", err)
        self.assertIn("1 项未经核验", err)
        self.assertTrue((f.root / "out/t43").is_dir())
        code, out, err = f.cli("check-delivery", f.root / "out", "--standalone", "--public-key", f.obs_public)
        self.assertEqual(code, 1, out + err)
        self.assertIn("未经核验 t43: missing receipt", out)

    def test_cli_overlap_has_no_task_submitted(self):
        f = self.f
        for target in ("ws/out", "."):
            f.task.write_bytes(f.task.read_bytes().replace(b'deliver_to = "out"', f'deliver_to = "{target}"'.encode()))
            with self.assertRaisesRegex(SystemExit, "overlaps the workspace"):
                f.anchored_run()
            with Ledger(f.ledger_path) as ledger:
                self.assertEqual(ledger.events("t43"), [])
            f.task.write_bytes(f.task.read_bytes().replace(f'deliver_to = "{target}"'.encode(), b'deliver_to = "out"'))

    def test_missing_obs_log_is_clear_for_shared_helper_and_clis(self):
        f = self.f
        f.config.write_bytes(f.config.read_bytes().replace(b'log = "obs.jsonl"\n', b''))
        for args in (("adjudicate", "t43"), ("check-delivery", f.root / "out")):
            code, out, err = f.cli(*args)
            self.assertEqual(code, 1, out + err)
            self.assertIn("[obs] requires log", err)

    def task_result(self, *, changes=None):
        f = self.f
        pristine = f.root / "pristine"
        pristine.mkdir(exist_ok=True)
        (pristine / "check.py").write_bytes((f.root / "ws/check.py").read_bytes())
        card = pristine / "card.md"
        card.write_text('# T00: delivery test\n- **Files you may change:** `result.txt`\n'
                        '- **Branch:** `task/T00-test`\n', encoding="utf-8")
        # The card lives outside the pristine tree compared by the exporter.
        external_card = f.root / "card.md"
        card.replace(external_card)
        (f.root / "ws/result.txt").write_text("old", encoding="utf-8")
        replies = [step("write_file", path="result.txt", content="approved"), step("finish")]
        self.assertEqual(f.anchored_run(replies)[0], 0)
        if changes:
            changes()
        from tools import task_result
        argv = ["task_result.py", "--config", str(f.config), "--ledger", str(f.ledger_path), "--task-id", "t43",
                "--workspace", str(f.root / "ws"), "--pristine", str(pristine), "--card", str(external_card), "--apply"]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            code = task_result.main()
        return code, output.getvalue(), pristine

    def test_ci_normal_witnessed_pass_is_exported(self):
        code, output, pristine = self.task_result()
        self.assertEqual(code, 0, output)
        self.assertEqual((pristine / "result.txt").read_text(encoding="utf-8"), "approved")

    def test_ci_forged_unwitnessed_tail_is_refused(self):
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

    def test_ci_last_gate_must_be_pass_for_the_same_candidate(self):
        for verdict, candidate in (("UNKNOWN", None), ("PASS", "different")):
            if verdict == "PASS":
                self.doCleanups()
                self.setUp()

            def append_gate():
                with Ledger(self.f.ledger_path) as ledger:
                    digest = ledger.last("t43", "candidate.frozen").payload["digest"]
                    ledger.append("t43", "gate.adjudication", {"verdict": verdict, "candidate": candidate or digest})

            code, output, pristine = self.task_result(changes=append_gate)
            self.assertEqual(code, 1, output)
            self.assertIn("::error::no Gate PASS", output)
            self.assertFalse((pristine / "result.txt").exists())

    def test_ci_requires_obs_and_trust_and_a_log(self):
        for missing in ("obs", "trust", "log", "pin"):
            with self.subTest(missing=missing):
                if missing != "obs":
                    self.doCleanups()
                    self.setUp()

                def remove_config():
                    f = self.f
                    raw = f.config.read_bytes()
                    if missing == "obs":
                        raw = raw.replace(f.obs_block, b"")
                    elif missing == "trust":
                        raw = raw.split(b"[trust]", 1)[0] + f.obs_block
                    elif missing == "pin":
                        raw = raw.split(b"[trust]", 1)[0] + b"[trust]\n" + f.obs_block
                    else:
                        raw = raw.replace(b'log = "obs.jsonl"\n', b'')
                    f.config.write_bytes(raw)

                code, output, pristine = self.task_result(changes=remove_config)
                self.assertEqual(code, 1, output)
                self.assertIn("::error::", output)
                self.assertIn("[trust]" if missing == "pin" else "[obs]", output)
                self.assertFalse((pristine / "result.txt").exists())


if __name__ == "__main__":
    unittest.main()
