"""T30: fail closed on the ten reviewed edge cases, plus T24 command facts."""
import io
import socket
import subprocess
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from m2k import sandbox, trust
from m2k.cli import main
from m2k.contract import Budget, ContractError, Subtask, check_subtasks
from m2k.kernel import Kernel, KernelError, recall, status
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.types import Verdict
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, review, step
from tools import sign_preflight as preflight


class SmallFixesTest(KernelCase):
    def cli_files(self, sandbox_config='[sandbox]\nbackend = "none"\n', worker='"worker"', reviewer='"reviewer"'):
        config, task = self.root / "config.toml", self.root / "task.toml"
        config.write_text('ledger = "cli.sqlite"\n' + sandbox_config
                          + f'[models.worker]\nmodel = {worker}\n[models.reviewer]\nmodel = {reviewer}\n',
                          encoding="utf-8")
        ws = self.root / "cli-ws"
        ws.mkdir(exist_ok=True)
        (ws / "check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        task.write_text('[task]\nid = "cli"\ngoal = "g"\nworkspace = "cli-ws"\n'
                        '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n', encoding="utf-8")
        return ["--config", str(config), "run", "--untrusted", str(task)]

    def refused_run(self, args, message):
        err = io.StringIO()
        with patch("m2k.cli.build_model") as build, patch("m2k.cli._ledger") as ledger, redirect_stderr(err):
            self.assertEqual(main(args), 2)
        self.assertIn(message, err.getvalue())
        build.assert_not_called()
        ledger.assert_not_called()
        if message == "no backend" or "Python is installed at /" in message:
            from tests.test_startup_refusals import assert_refusal
            assert_refusal(self, self.root / "cli.sqlite", "cli", "SANDBOX_UNAVAILABLE")
        else:
            self.assertFalse((self.root / "cli.sqlite").exists())
        return err.getvalue()

    def test_python_prefix_at_root_makes_bwrap_unavailable(self):
        reason = "Python is installed at /; binding it would expose the whole file system"
        args = self.cli_files('[sandbox]\nbackend = "bwrap"\n')
        for field in ("prefix", "base_prefix"):
            for value in ("/", str(Path("/") / "usr" / "..")):
                with self.subTest(field=field, value=value), \
                        patch.object(sandbox.sys, "platform", "linux"), \
                        patch.object(sandbox.sys, "prefix", str(self.root)), \
                        patch.object(sandbox.sys, "base_prefix", str(self.root)), \
                        patch.object(sandbox.sys, field, value), \
                        patch.object(sandbox.shutil, "which", return_value="bwrap"), \
                        patch.object(sandbox.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as probe:
                    self.assertEqual(sandbox.Bwrap().available(), (False, reason))
                    self.refused_run(args, reason)
                    probe.assert_not_called()

    def test_sandbox_config_must_be_a_table(self):
        for value in ('"bwrap"', "1", "[]", "true"):
            with self.subTest(value=value):
                self.refused_run(self.cli_files(f"sandbox = {value}\n"),
                                 '配置错误：[sandbox] 必须是一个表，例如 [sandbox] backend = "bwrap"')

    def test_model_names_must_be_strings(self):
        for role in ("worker", "reviewer"):
            for value in ("1", "true", "[]", "{}"):
                with self.subTest(role=role, value=value):
                    self.refused_run(self.cli_files(**{role: value}),
                                     "配置错误：[models.worker] 和 [models.reviewer] 的 model 必须是字符串")

    def test_unknown_recall_is_distinct_from_an_existing_parentless_task(self):
        self.assertEqual(recall(self.ledger, "missing", "stop"), "unknown task 'missing'")
        self.assertEqual(self.ledger.events(), [])
        self.ledger.append("parent", "task.submitted", {"goal": "g"})
        self.ledger.append("orphan", "worker.stopped", {"status": "STALLED"})
        before = self.ledger.events()
        for tid in ("parent", "orphan"):
            self.assertEqual(recall(self.ledger, tid, "stop"), f"'{tid}' is not a subtask")
        self.assertEqual(self.ledger.events(), before)

    def test_status_and_report_without_submission_refuse_unknown_task(self):
        self.ledger.append("orphan", "worker.stopped", {"status": "STALLED"})
        for tid in ("missing", "orphan"):
            with self.subTest(tid=tid):
                message = f"unknown task '{tid}'"
                with self.assertRaises(KernelError) as error:
                    status(self.ledger, tid)
                self.assertEqual(str(error.exception), message)
                with self.assertRaises(SystemExit) as error:
                    main(["--ledger", str(self.ledger.path), "status", tid])
                self.assertEqual(str(error.exception), message)
                err, out = io.StringIO(), self.root / f"{tid}.html"
                with redirect_stderr(err):
                    self.assertEqual(main(["--ledger", str(self.ledger.path), "report", tid, "--out", str(out)]), 1)
                self.assertIn(message, err.getvalue())
                self.assertFalse(out.exists())

    def test_child_credential_writes_must_be_a_list_of_strings(self):
        parent = self.contract(budget=Budget(max_tokens=1000))
        sub = Subtask("calc", "write calc.py", ("calc.py",), parent.acceptance,
                      Budget(max_rounds=4, max_model_calls=5, max_tokens=1000, max_commands=1), parent.tools)
        parent = replace(parent, subtasks=(sub,))
        child = replace(parent, task_id="t1.calc", subtasks=(), writes=sub.writes, budget=sub.budget)
        caps = {**vars(parent.budget), "tools": list(parent.tools), "network": False}
        setup, _ = trust.dev_setup(self.root / "keys", caps)
        issuer, now = setup.issuer_key(), int(time.time())
        parent_env = trust.issue_credential(setup.charter_env["body"], issuer, parent, now, setup.ttl_s)
        child_env = trust.issue_child_credential(issuer, parent_env["body"], parent, child, sub.id, now)

        def verify(env):
            return trust.verify_child_chain(setup.pin, setup.domain, setup.charter_env, parent_env, env,
                                            setup.revocations(), parent, child, now, Path(trust.__file__).parent)

        self.assertEqual(verify(child_env)["writes"], ["calc.py"])
        for writes in ("calc.py", {"calc.py": True}, 1, None, [1], ["calc.py", None]):
            with self.subTest(writes=writes):
                env = trust.sign(child_env["body"] | {"writes": writes}, issuer)
                with self.assertRaises(trust.TrustError) as error:
                    verify(env)
                self.assertEqual(error.exception.code, "FORMAT")

    def test_budget_sum_error_names_the_subtasks(self):
        parent = self.contract(budget=Budget(max_rounds=5))
        subs = tuple(Subtask(sid, "write calc.py", ("calc.py",), parent.acceptance,
                             Budget(max_rounds=3, max_model_calls=4, max_commands=1), parent.tools)
                     for sid in ("alpha", "beta"))
        with self.assertRaises(ContractError) as error:
            check_subtasks(replace(parent, subtasks=subs))
        for text in ("add up", "max_rounds", "alpha", "beta"):
            self.assertIn(text, str(error.exception))

    def test_untrusted_warning_only_after_sandbox_passes(self):
        args = self.cli_files()
        warning = "警告：未授权运行（没有信任链），已记入账本。"
        with patch("m2k.cli.sandboxes.choose", return_value=sandbox.Unavailable("no backend")):
            self.assertNotIn(warning, self.refused_run(args, "no backend"))
        models = [ScriptedModel([step("finish")], "worker"), ScriptedModel([review("GO")], "reviewer")]
        err = io.StringIO()
        with patch("m2k.cli.sandboxes.choose", return_value=sandbox.NoSandbox()), \
                patch("m2k.cli.build_model", side_effect=models), redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        self.assertIn(warning, err.getvalue())
        with Ledger(self.root / "cli.sqlite") as ledger:
            self.assertIsNone(ledger.last("cli", "task.submitted").payload["trust"])
            self.assertEqual(ledger.last("cli", "verdict").payload["verdict"], "PASS")
            self.assertTrue(ledger.verify_chain()[0])

    def test_repair_requires_the_last_audit_for_the_failed_candidate(self):
        for case in ("missing", "mismatch", "matching"):
            with self.subTest(case=case):
                contract = replace(self.contract(task_id=case), max_repairs=1, deliver_to=self.root / case)
                worker = ScriptedModel([step("write_file", path="calc.py", content=BAD_CODE), step("finish"),
                                        step("write_file", path="calc.py", content=GOOD_CODE), step("finish")], "worker")
                reviewer = ScriptedModel([review("GO")] * 2, "reviewer")
                original_last = self.ledger.last

                def last(tid, kind):
                    event = original_last(tid, kind)
                    if kind == "audit.result":
                        if case == "missing":
                            return None
                        if case == "mismatch":
                            return replace(event, payload=event.payload | {"candidate": "another-candidate"})
                    return event

                with patch.object(self.ledger, "last", side_effect=last):
                    report = Kernel(self.ledger, worker, reviewer).run(contract)
                allowed = case == "matching"
                self.assertEqual(report.verdict, Verdict.PASS if allowed else Verdict.FAIL)
                self.assertEqual(len(self.ledger.events(case, "repair.started")), int(allowed))
                self.assertEqual(len(self.ledger.events(case, "candidate.frozen")), 1 + int(allowed))
                self.assertEqual(len(worker.calls), 4 if allowed else 2)
                self.assertEqual(len(reviewer.calls), 2 if allowed else 1)
                if not allowed:
                    self.assertIsNone(report.delivered_to)
                    self.assertEqual(self.ledger.events(case, "delivery.intent"), [])

    def test_dns_unexpected_exception_and_empty_queue_are_look(self):
        for error in (RuntimeError("resolver crashed"), ValueError("invalid lookup")):
            with self.subTest(error=error), patch.object(preflight.socket, "gethostbyname", side_effect=error), \
                    patch.object(preflight.threading, "excepthook"):
                state, reason = preflight.dns_check()
                self.assertEqual(state, "LOOK")
                self.assertIn(str(error), reason)
        with patch.object(preflight.threading, "Thread") as thread, \
                patch.object(preflight.socket, "gethostbyname") as lookup:
            thread.return_value.is_alive.return_value = False
            state, reason = preflight.dns_check()
            self.assertEqual(state, "LOOK")
            self.assertIn("no result", reason)
            lookup.assert_not_called()
        for result, error, expected in (("127.0.0.1", None, "FAIL"), (None, socket.gaierror("offline"), "PASS")):
            with self.subTest(expected=expected), \
                    patch.object(preflight.socket, "gethostbyname", return_value=result, side_effect=error):
                self.assertEqual(preflight.dns_check()[0], expected)

    def test_successor_receives_failed_command_exit_code_in_facts(self):
        parent = self.contract()
        sub = Subtask("calc", "write calc.py", ("calc.py",), parent.acceptance,
                      Budget(max_rounds=8, max_model_calls=10, max_commands=2), parent.tools, max_reassign=1)
        command = '{python} -c "raise SystemExit(1)"'
        worker = ScriptedModel([step("write_file", path="calc.py", content=BAD_CODE),
                                step("run_command", command=command), step("finish"),
                                step("write_file", path="calc.py", content=GOOD_CODE), step("finish")], "worker")
        reviewer = ScriptedModel([review("GO")] * 3, "reviewer")
        report = Kernel(self.ledger, worker, reviewer).run(replace(parent, subtasks=(sub,)))
        self.assertEqual(report.verdict, Verdict.PASS, report.reasons)
        failed = next(e for e in self.ledger.events("t1.calc", "effect.result") if e.payload["tool"] == "run_command")
        self.assertEqual(failed.payload["detail"]["exit_code"], 1)
        prompt = worker.calls[3][1]["content"]
        self.assertIn("Facts from the ledger about earlier attempts", prompt)
        self.assertIn(f"run_command {command!r}: FAILED (exit 1)", prompt)


if __name__ == "__main__":
    unittest.main()
