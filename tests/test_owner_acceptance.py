"""Owner acceptance files and frozen-candidate shadow-module checks, using scripted models."""
import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from m2k.contract import Acceptance, ContractError, TaskContract, parse_contract
from m2k.kernel import Kernel, KernelError
from m2k.models import ScriptedModel
from m2k.types import Verdict
from tests.helpers import BAD_CODE, CHECK, GOOD_CODE, KernelCase, review, step

FINISH = step("finish", summary="done")
WRITE_GOOD = step("write_file", path="calc.py", content=GOOD_CODE)


class OwnerAcceptanceParserTest(unittest.TestCase):
    def test_missing_empty_or_invalid_files_rejected(self):
        for files in (None, [], "check.py", [""], [1]):
            with self.subTest(files=files):
                acc = {"command": "{python} check.py"}
                if files is not None:
                    acc["files"] = files
                data = {"task": {"id": "t", "goal": "g", "workspace": "ws"}, "acceptance": acc}
                with self.assertRaises(ContractError) as error:
                    parse_contract(data, Path.cwd())
                self.assertEqual(str(error.exception),
                                 "[acceptance].files must list the task owner's acceptance files")

    def test_files_become_a_tuple(self):
        data = {"task": {"id": "t", "goal": "g", "workspace": "ws"},
                "acceptance": {"command": "{python} check.py", "files": ["check.py", "data.txt"]}}
        self.assertEqual(parse_contract(data, Path.cwd()).acceptance.files, ("check.py", "data.txt"))


class OwnerAcceptanceTest(KernelCase):
    def run_contract(self, contract, steps):
        self.worker = ScriptedModel(steps, "worker")
        self.reviewer = ScriptedModel([review("GO", "tests pass")], "reviewer")
        return Kernel(self.ledger, self.worker, self.reviewer).run(contract)

    def assert_refused(self, contract, message):
        with self.assertRaises(KernelError) as error:
            self.run_contract(contract, [WRITE_GOOD, FINISH])
        self.assertEqual(str(error.exception), message)
        self.assertEqual(self.ledger.events(), [])
        self.assertEqual(self.worker.calls, [])
        self.assertEqual(self.reviewer.calls, [])

    def assert_failed_without_delivery(self, contract, result, reason):
        self.assertEqual(result.verdict, Verdict.FAIL)
        self.assertEqual(result.test.reason, reason)
        self.assertIsNone(result.delivered_to)
        self.assertEqual(self.ledger.last(contract.task_id, "test.result").payload["reason"], reason)
        self.assertNotIn("delivery.intent", self.kinds(contract.task_id))
        self.assertTrue(self.ledger.verify_chain()[0])

    def test_empty_files_refused_without_events(self):
        c = replace(self.contract(), acceptance=Acceptance("{python} check.py"))
        self.assert_refused(c, "acceptance files must be provided by the task owner")

    def test_worker_cannot_create_its_own_acceptance(self):
        c = replace(self.contract(), acceptance=Acceptance("{python} worker_check.py", files=("worker_check.py",)))
        with self.assertRaisesRegex(KernelError, "acceptance file missing at submit: worker_check.py"):
            self.run_contract(c, [step("write_file", path="worker_check.py", content="print('OK')"), FINISH])
        self.assertFalse((c.workspace / "worker_check.py").exists())
        self.assertEqual(self.ledger.events(), [])
        self.assertEqual(self.worker.calls, [])
        self.assertEqual(self.reviewer.calls, [])

    def test_every_listed_file_must_exist_after_seeding(self):
        seed = self.root / "seed"
        seed.mkdir()
        (seed / "check.py").write_text(CHECK, encoding="utf-8")
        c = TaskContract("seed", "g", self.root / "seed-ws",
                         Acceptance("{python} check.py", files=("check.py", "missing.py")), seed=seed)
        self.assert_refused(c, "acceptance file missing at submit: missing.py")
        self.assertEqual((c.workspace / "check.py").read_text(encoding="utf-8"), CHECK)

    def test_directory_does_not_count_as_acceptance_file(self):
        c = self.contract()
        (c.workspace / "folder").mkdir()
        c = replace(c, acceptance=Acceptance("{python} folder", files=("folder",)))
        self.assert_refused(c, "acceptance file missing at submit: folder")

    def test_command_must_name_an_acceptance_file(self):
        c = self.contract(command="{python} calc.py")
        self.assert_refused(c, "acceptance command does not use any acceptance file")

    def test_auto_protection_denies_write_and_edit_without_explicit_protected(self):
        c, r = self.run_task([step("write_file", path="check.py", content="print('OK')"),
                              step("edit_file", path="./check.py", old="== 5", new="== -1"),
                              WRITE_GOOD, FINISH], [review("GO")])
        self.assertEqual(c.protected, ())
        self.assertEqual([e.payload["gate"] for e in self.ledger.events(c.task_id, "gate.denied")],
                         ["protected_path", "protected_path"])
        self.assertEqual((c.workspace / "check.py").read_text(encoding="utf-8"), CHECK)
        self.assertEqual(r.verdict, Verdict.PASS)

    def test_submitted_hashes_and_effective_protection_include_all_owner_files(self):
        c = self.contract(protected=("owner.txt",))
        (c.workspace / "owner.txt").write_text("owner", encoding="utf-8")
        (c.workspace / "checks").mkdir()
        owner = c.workspace / "checks" / "extra.py"
        owner.write_text("pass\n", encoding="utf-8")
        c = replace(c, acceptance=Acceptance("{python} check.py", files=("check.py", "checks/extra.py")))
        r = self.run_contract(c, [step("write_file", path="owner.txt", content="changed"),
                                  step("write_file", path="checks/extra.py", content="changed"), WRITE_GOOD, FINISH])
        submitted = self.ledger.last(c.task_id, "task.submitted").payload
        hashes = {rel: hashlib.sha256((c.workspace / rel).read_bytes()).hexdigest() for rel in c.acceptance.files}
        self.assertEqual(submitted["acceptance_files"], hashes)
        self.assertEqual(set(submitted["protected"]), {"owner.txt", "check.py", "checks/extra.py"})
        self.assertEqual(submitted["contract_sha256"], c.digest())
        self.assertEqual([e.payload["gate"] for e in self.ledger.events(c.task_id, "gate.denied")],
                         ["protected_path", "protected_path"])
        self.assertEqual(r.verdict, Verdict.PASS)

    def test_acceptance_command_matches_posix_path_or_basename(self):
        for i, command in enumerate(("{python} checks/check.py", "{python} checks/./check.py")):
            with self.subTest(command=command):
                c = self.contract(task_id=f"path-{i}")
                (c.workspace / "checks").mkdir()
                (c.workspace / "checks" / "check.py").write_text("print('OK')\n", encoding="utf-8")
                c = replace(c, acceptance=Acceptance(command, files=("checks/../checks/check.py",)))
                r = self.run_contract(c, [WRITE_GOOD, FINISH])
                self.assertEqual(r.verdict, Verdict.PASS)
                self.assertEqual(set(self.ledger.last(c.task_id, "task.submitted").payload["acceptance_files"]),
                                 {"checks/check.py"})

    def test_command_route_rewrite_or_removal_fails_before_test_runs(self):
        commands = ("open('check.py','w').write('print(1)')", "__import__('os').remove('check.py')")
        for i, code in enumerate(commands):
            with self.subTest(code=code):
                c = self.contract(task_id=f"tamper-{i}")
                with patch("m2k.kernel.run_test", side_effect=AssertionError("tampered acceptance must not run")):
                    r = self.run_contract(c, [step("write_file", path="calc.py", content=BAD_CODE),
                                              step("run_command", command=f'{{python}} -c "{code}"'), FINISH])
                self.assert_failed_without_delivery(c, r, "PROTECTED_FILE_CHANGED")
                self.assertIn("check.py", "\n".join(r.reasons))

    def test_new_root_shadow_modules_and_packages_fail_before_test_runs(self):
        for i, rel in enumerate(("unittest.py", "json/__init__.py", "sitecustomize.py", "usercustomize.py", "conftest.py")):
            with self.subTest(rel=rel):
                c = self.contract(task_id=f"shadow-{i}")
                with patch("m2k.kernel.run_test", side_effect=AssertionError("shadowed acceptance must not run")):
                    r = self.run_contract(c, [WRITE_GOOD, step("write_file", path=rel, content="pass\n"), FINISH])
                self.assert_failed_without_delivery(c, r, "SHADOWED_MODULE")
                self.assertIn(rel, "\n".join(r.reasons))

    def test_shadows_next_to_nested_acceptance_file_are_detected(self):
        for i, rel in enumerate(("checks/unittest.py", "checks/json/__init__.py")):
            with self.subTest(rel=rel):
                c = self.contract(task_id=f"nested-{i}")
                (c.workspace / "checks").mkdir()
                (c.workspace / "checks" / "check.py").write_text("pass\n", encoding="utf-8")
                c = replace(c, acceptance=Acceptance("{python} checks/check.py", files=("checks/check.py",)))
                r = self.run_contract(c, [WRITE_GOOD, step("write_file", path=rel, content="pass\n"), FINISH])
                self.assert_failed_without_delivery(c, r, "SHADOWED_MODULE")
                self.assertIn(rel, "\n".join(r.reasons))

    def test_changed_seed_shadow_is_detected_but_unchanged_is_allowed(self):
        for i, changed in enumerate((False, True)):
            with self.subTest(changed=changed):
                seed = self.root / f"shadow-seed-{i}"
                seed.mkdir()
                (seed / "check.py").write_text(CHECK, encoding="utf-8")
                (seed / "json").mkdir()
                (seed / "json" / "__init__.py").write_text("pass\n", encoding="utf-8")
                c = TaskContract(f"seed-{i}", "g", self.root / f"seed-ws-{i}",
                                 Acceptance("{python} check.py", files=("check.py",)), seed=seed,
                                 deliver_to=self.root / "out")
                steps = [WRITE_GOOD]
                if changed:
                    steps.append(step("write_file", path="json/__init__.py", content="# changed\n"))
                r = self.run_contract(c, steps + [FINISH])
                if changed:
                    self.assert_failed_without_delivery(c, r, "SHADOWED_MODULE")
                    self.assertIn("json/__init__.py", "\n".join(r.reasons))
                else:
                    self.assertEqual(r.verdict, Verdict.PASS)
                    self.assertIsNotNone(r.delivered_to)

    def test_ordinary_code_and_unrelated_subfolder_shadows_are_allowed(self):
        c, r = self.run_task([WRITE_GOOD, step("write_file", path="other/unittest.py", content="pass\n"),
                              step("write_file", path="other/json/__init__.py", content="pass\n"), FINISH],
                             [review("GO")])
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assertIsNotNone(r.delivered_to)
        self.assertEqual(r.test.reason, "")

    def test_wrong_code_and_fake_unittest_cannot_pass_a_go_review(self):
        check = ("import unittest\nfrom calc import add\nclass Check(unittest.TestCase):\n"
                 "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\nunittest.main()\n")
        c = self.contract(check=check)
        r = self.run_contract(c, [step("write_file", path="calc.py", content=BAD_CODE),
                                  step("write_file", path="unittest.py", content="class TestCase: pass\ndef main(): print('OK')\n"),
                                  FINISH])
        self.assert_failed_without_delivery(c, r, "SHADOWED_MODULE")
        self.assertEqual(r.audit.decision, "GO")
        self.assertIn("unittest.py", "\n".join(r.reasons))


if __name__ == "__main__":
    unittest.main()
