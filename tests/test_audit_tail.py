"""T86: exhausted candidates, bounded review retries, and unknown connection usage."""
import http.client
import json
import ssl
import unittest
import urllib.error
from dataclasses import replace
from unittest.mock import MagicMock, patch

from m2k.adjudicate import adjudicate
from m2k.contract import Budget
from m2k.kernel import Kernel
from m2k.ledger import Ledger
from m2k.models import ModelError, OpenAICompatModel, ScriptedModel
from m2k.types import Verdict, WorkerStatus
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, review, step
from tests.test_busy_retry import OK, URL, answer, http as http_error
from tests.test_external_agent import AgentCase
from tests.test_model_plans import GOOD_PLAN, PARTS
from tests.test_task_tree import TreeCase, good_a, good_b, sub


WRITE = step("write_file", path="calc.py", content=GOOD_CODE)
FINISH = step("finish")
JSON_REASON = "reviewer reply was not exactly one JSON object"
AUDIT_KEYS = {"verdict", "decision", "reasons", "candidate", "reviewer", "nonce"}


def fail_review(messages):
    raise ModelError("connection failed")


class BudgetTailTest(AgentCase, KernelCase):
    def test_round_budget_delivers_checked_candidate_without_finish(self):
        c = replace(self.contract(budget=Budget(max_rounds=1)), max_repairs=2)
        worker, reviewer = ScriptedModel([WRITE], "w"), ScriptedModel([review("GO")], "r")
        report = Kernel(self.ledger, worker, reviewer).run(c)
        self.assertEqual(report.worker.status, WorkerStatus.BUDGET_EXHAUSTED)
        self.assertEqual((report.verdict, report.reported_verdict), (Verdict.PASS, Verdict.PASS))
        self.assertEqual((report.delivered_to / "calc.py").read_text(encoding="utf-8"), GOOD_CODE)
        self.assertFalse(self.ledger.events(c.task_id, "owner.request"))
        self.assertIsNone(report.awaiting)
        self.assertEqual(adjudicate(self.ledger, c.task_id).verdict, "PASS")
        self.assertEqual(report.reasons[0], "worker stopped: BUDGET_EXHAUSTED "
                         "(round budget exhausted (1)); candidate checked as usual")
        self.assertFalse(self.ledger.events(c.task_id, "repair.started"))
        self.assertEqual(len(reviewer.calls), 1)

    def test_failing_or_unreviewable_budget_candidate_still_awaits_owner(self):
        cases = [("test-fails", BAD_CODE, Budget(max_rounds=1), [review("GO")], 0),
                 ("no-review-budget", GOOD_CODE, Budget(max_model_calls=1), [review("GO")], 0),
                 ("review-no-go", GOOD_CODE, Budget(max_rounds=1), [review("NO_GO")], 1)]
        for tid, code, budget, replies, count in cases:
            with self.subTest(tid=tid):
                c, report = self.run_task([step("write_file", path="calc.py", content=code)], replies,
                                          task_id=tid, budget=budget)
                self.assertEqual(report.worker.status, WorkerStatus.BUDGET_EXHAUSTED)
                self.assertEqual(report.verdict, Verdict.UNKNOWN)
                self.assertIsNone(report.delivered_to)
                self.assertEqual(len(self.reviewer.calls), count)
                requests = self.ledger.events(tid, "owner.request")
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].payload, {"kind": "budget", "text": report.worker.reason,
                                                      "request": None, "by": "framework"})
                self.assertGreater(requests[0].seq, self.ledger.last(tid, "test.result").seq)
                if count:
                    self.assertGreater(requests[0].seq, self.ledger.last(tid, "audit.result").seq)
                self.assertEqual(report.awaiting, dict(requests[0].payload, seq=requests[0].seq))
                self.assertEqual(report.reasons[0], f"awaiting owner: budget: {report.worker.reason}")
                self.assertNotEqual(adjudicate(self.ledger, tid).verdict, "PASS")
                if not count:
                    self.assertIn("the worker did not finish (BUDGET_EXHAUSTED)",
                                  self.ledger.last(tid, "gate.adjudication").payload["reasons"])

    def test_broken_trust_keeps_budget_request_and_skips_review(self):
        for problem in ("CREDENTIAL_REVOKED", "FRAMEWORK_CHANGED: test change"):
            with self.subTest(problem=problem), patch.object(Kernel, "_problem", return_value=problem):
                _, report = self.run_task([WRITE], [review("GO")], task_id=problem.split(":")[0].lower(),
                                          budget=Budget(max_rounds=1))
                self.assertEqual(report.verdict, Verdict.UNKNOWN)
                self.assertIsNone(report.audit)
                self.assertIsNone(report.delivered_to)
                self.assertFalse(self.reviewer.calls)
                self.assertEqual(report.awaiting["text"], "round budget exhausted (1)")

    def test_non_budget_stop_with_correct_code_never_runs_review(self):
        for tid, tail, expected in (("stalled", ["invalid"] * 3, WorkerStatus.STALLED),
                                    ("model-error", [fail_review], WorkerStatus.MODEL_ERROR),
                                    ("owner", [step("ask_owner", kind="question", text="confirm")],
                                     WorkerStatus.AWAITING_OWNER)):
            with self.subTest(tid=tid):
                _, report = self.run_task([WRITE, *tail], [review("GO")], task_id=tid)
                self.assertEqual(report.worker.status, expected)
                self.assertEqual(report.test.verdict, Verdict.PASS)
                self.assertIsNone(report.audit)
                self.assertIsNone(report.delivered_to)
                self.assertFalse(self.reviewer.calls)

    def test_token_exhaustion_leaves_no_review_budget(self):
        _, report = self.run_task([WRITE], [review("GO")], budget=Budget(max_tokens=20))
        self.assertEqual(report.worker.status, WorkerStatus.BUDGET_EXHAUSTED)
        self.assertEqual(report.test.verdict, Verdict.PASS)
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        self.assertFalse(self.reviewer.calls)

    def test_changed_acceptance_candidate_cannot_use_budget_tail(self):
        check = "from pathlib import Path\nPath('changed.txt').write_text('changed')\n"
        _, report = self.run_task([WRITE], [review("GO")], budget=Budget(max_rounds=1), check=check)
        self.assertEqual(report.test.reason, "CANDIDATE_CHANGED")
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        self.assertFalse(self.reviewer.calls)

    def test_external_budget_candidate_uses_same_review_rule(self):
        # The existing CLI fixture writes correct code but reports more than 50k tokens.
        reviewer = ScriptedModel([review("GO")], "r")
        report = Kernel(self.ledger, self.agent("budget"), reviewer).run(
            self.contract(budget=Budget(max_tokens=50000)))
        self.assertEqual(report.worker.status, WorkerStatus.BUDGET_EXHAUSTED)
        self.assertEqual(report.test.verdict, Verdict.PASS)
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        self.assertFalse(reviewer.calls)
        self.assertEqual(report.awaiting["by"], "framework")

    def test_adjudicator_rejects_stalled_even_with_other_pass_facts(self):
        c, report = self.run_task([WRITE, FINISH], [review("GO")])
        self.assertEqual(report.verdict, Verdict.PASS)
        with Ledger(self.root / "stalled.sqlite") as copied:
            for event in self.ledger.events(c.task_id):
                payload = dict(event.payload)
                if event.kind == "worker.stopped":
                    payload["status"] = "STALLED"
                copied.append(event.task_id, event.kind, payload, op_id=event.op_id)
            result = adjudicate(copied, c.task_id)
        self.assertNotEqual(result.verdict, "PASS")
        self.assertIn("the worker did not finish (STALLED)", result.reasons)


class BudgetChildTest(TreeCase):
    def test_child_budget_candidate_passes_and_merges(self):
        c = self.parent(subs=(sub("a", ["a.py"], "check_a.py", rounds=1, calls=3),
                              sub("b", ["b.py"], "check_b.py")))
        _, report = self.run_tree(c, good_a()[:1] + good_b(), [review("GO")] * 3)
        self.assertEqual(report.verdict, Verdict.PASS, report.reasons)
        self.assertEqual((report.delivered_to / "a.py").read_text(encoding="utf-8"), "A = 1\n")
        self.assertEqual(self.ledger.last("p1.a", "worker.stopped").payload["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(self.ledger.last("p1.a", "verdict").payload["verdict"], "PASS")
        self.assertEqual(adjudicate(self.ledger, "p1.a").verdict, "PASS")
        self.assertFalse(self.ledger.events("p1.a", "owner.request"))
        self.assertEqual(len(self.merges("p1")), 4)

    def test_child_budget_failure_still_does_not_audit_or_merge(self):
        for tid, code, calls in (("bad-child", "A = 9\n", 3), ("no-review-child", "A = 1\n", 1)):
            with self.subTest(tid=tid):
                c = self.parent(tid=tid, subs=(sub("a", ["a.py"], "check_a.py", rounds=1, calls=calls),))
                kernel, report = self.run_tree(c, [step("write_file", path="a.py", content=code)], [review("GO")])
                self.assertEqual(report.verdict, Verdict.FAIL)
                self.assertEqual(self.ledger.last(f"{tid}.a", "verdict").payload["verdict"], "FAIL")
                self.assertFalse(kernel.reviewer_model.calls)
                self.assertFalse(self.merges(tid))
                self.assertFalse(self.ledger.events(f"{tid}.a", "owner.request"))


class AuditRetryTest(KernelCase):
    def checked(self, replies, **kw):
        return self.run_task([WRITE, FINISH], replies, **kw)

    def review_calls(self, tid="t1"):
        return [e for e in self.ledger.events(tid, "model.called") if e.payload["role"] == "reviewer"]

    def test_non_json_then_go_records_original_reply_and_new_private_nonce(self):
        raw = "first bad reply " * 200

        def approve(messages):
            obj = json.loads(review("GO")(messages))
            self.assertNotIn(obj["nonce"], json.dumps([e.payload for e in self.ledger.events("t1")]))
            self.assertNotIn(obj["nonce"], json.dumps(self.worker.calls))
            return json.dumps(obj)

        _, report = self.checked([raw, approve])
        self.assertEqual(report.verdict, Verdict.PASS)
        self.assertIsNotNone(report.delivered_to)
        self.assertEqual(len(self.reviewer.calls), 2)
        self.assertEqual(len(self.review_calls()), 2)
        retried, = self.ledger.events("t1", "audit.retried")
        self.assertEqual(retried.payload, {"candidate": report.candidate.digest, "decision": "NONE",
                                          "reasons": [JSON_REASON], "nonce": retried.payload["nonce"],
                                          "reply": raw[:2000]})
        self.assertNotEqual(retried.payload["nonce"], report.audit.nonce)
        self.assertEqual(set(self.ledger.last("t1", "audit.result").payload), AUDIT_KEYS)

    def test_model_error_then_go_records_only_successful_reply_usage(self):
        _, report = self.checked([fail_review, review("GO")], budget=Budget(max_model_calls=3))
        self.assertEqual(report.verdict, Verdict.PASS)
        self.assertEqual(len(self.reviewer.calls), 2)
        called, = self.review_calls()
        self.assertEqual((called.payload["input_tokens"], called.payload["output_tokens"]), (10, 10))
        retried = self.ledger.last("t1", "audit.retried").payload
        self.assertIsNone(retried["reply"])
        self.assertEqual(retried["reasons"], ["reviewer error: connection failed"])
        self.assertNotEqual(retried["nonce"], report.audit.nonce)
        self.assertEqual(report.usage["tokens_unknown_calls"], 0)
        self.assertEqual(report.usage["model_calls"], 3)

    def test_two_model_errors_stop_after_two_calls_without_recording_reply_usage(self):
        _, report = self.checked([fail_review, fail_review, review("GO")], budget=Budget(max_model_calls=3))
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        self.assertEqual(report.audit.reasons, ["reviewer error: connection failed"])
        self.assertEqual(len(self.reviewer.calls), 2)
        self.assertFalse(self.review_calls())
        self.assertEqual(len(self.ledger.events("t1", "audit.retried")), 1)
        self.assertIsNone(self.ledger.last("t1", "audit.retried").payload["reply"])
        self.assertEqual(report.usage["model_calls"], 2)
        self.assertEqual(report.usage["tokens_unknown_calls"], 0)

    def test_two_bad_replies_keep_second_reason_and_reply(self):
        raw = "second bad reply " * 200
        _, report = self.checked(["not json", raw, review("GO")])
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        self.assertEqual(report.audit.reasons, [JSON_REASON])
        self.assertEqual(len(self.reviewer.calls), 2)
        self.assertEqual(len(self.review_calls()), 2)
        self.assertEqual(len(self.ledger.events("t1", "audit.retried")), 1)
        payload = self.ledger.last("t1", "audit.result").payload
        self.assertEqual(set(payload), AUDIT_KEYS | {"reply"})
        self.assertEqual(payload["reply"], raw[:2000])
        self.assertIsNone(report.delivered_to)

    def test_second_call_error_is_final_and_has_no_raw_reply(self):
        _, report = self.checked(["not json", fail_review, review("GO")])
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        self.assertEqual(report.audit.reasons, ["reviewer error: connection failed"])
        self.assertEqual(set(self.ledger.last("t1", "audit.result").payload), AUDIT_KEYS)
        self.assertEqual(len(self.reviewer.calls), 2)
        self.assertEqual(len(self.review_calls()), 1)

    def test_nonce_mismatch_and_invalid_decision_never_retry(self):
        cases = [("nonce", '{"nonce":"wrong","decision":"GO"}', "reviewer reply nonce mismatch"),
                 ("decision", review("MAYBE"), "invalid decision 'MAYBE'")]
        for tid, reply, reason in cases:
            with self.subTest(tid=tid):
                _, report = self.checked([reply, review("GO")], task_id=tid)
                self.assertEqual(report.audit.reasons, [reason])
                self.assertEqual(report.verdict, Verdict.UNKNOWN)
                self.assertEqual(len(self.reviewer.calls), 1)
                self.assertFalse(self.ledger.events(tid, "audit.retried"))
                self.assertIn("reply", self.ledger.last(tid, "audit.result").payload)

    def test_budget_denial_keeps_first_bad_reply_without_second_call(self):
        _, report = self.checked(["not json", review("GO")], budget=Budget(max_model_calls=3))
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        self.assertEqual(report.audit.reasons, [JSON_REASON])
        self.assertEqual(len(self.reviewer.calls), 1)
        self.assertEqual(len(self.review_calls()), 1)
        self.assertEqual(len(self.ledger.events("t1", "audit.retried")), 1)

    def test_review_not_run_does_not_retry_or_generate_nonce(self):
        with patch("m2k.verify.secrets.token_hex", side_effect=AssertionError("no nonce before a call")):
            _, report = self.checked([review("GO")], budget=Budget(max_model_calls=2))
        self.assertIsNone(report.audit.nonce)
        self.assertFalse(self.reviewer.calls)
        self.assertFalse(self.review_calls())
        self.assertFalse(self.ledger.events("t1", "audit.retried"))

    def test_go_and_no_go_payloads_keep_original_keys(self):
        for decision in ("GO", "NO_GO"):
            with self.subTest(decision=decision):
                _, report = self.checked([review(decision)], task_id=decision.lower())
                payload = self.ledger.last(report.task_id, "audit.result").payload
                self.assertEqual(set(payload), AUDIT_KEYS)
                self.assertEqual(payload["decision"], decision)
                self.assertEqual(len(self.reviewer.calls), 1)


class ConnectionRetryTest(unittest.TestCase):
    def call(self, outcomes, delays=(2, 5)):
        model = OpenAICompatModel(URL, "m", api_key_env="T86_KEY", retry_delays=delays)
        with patch.dict("os.environ", {"T86_KEY": "k"}), \
                patch("urllib.request.urlopen", side_effect=outcomes) as request, \
                patch("m2k.models.time.sleep") as sleep:
            try:
                result = model.complete([{"role": "user", "content": "hello"}])
            except ModelError as error:
                result = error
        return result, request.call_count, [c.args[0] for c in sleep.call_args_list]

    def test_connection_errors_retry_with_unknown_attempts(self):
        failures = [OSError("reset"), urllib.error.URLError("reset"), ssl.SSLEOFError(8, "EOF"),
                    http.client.RemoteDisconnected("closed"), http.client.IncompleteRead(b"part", 10),
                    TimeoutError("timeout"), ConnectionResetError("reset"), ssl.SSLError("SSL")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                result, calls, waits = self.call([failure, answer(OK)])
                self.assertNotIsInstance(result, ModelError)
                self.assertEqual((result.text, result.lost_attempts, calls, waits), ("hi", 1, 2, [2.0]))

    def test_connection_failure_while_reading_response_retries(self):
        response = MagicMock()
        response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"part", 10)
        result, calls, waits = self.call([response, answer(OK)])
        self.assertEqual((result.text, result.lost_attempts, calls, waits), ("hi", 1, 2, [2.0]))

    def test_busy_and_connections_share_attempt_limit_and_last_error_type(self):
        for last, expected in ((OSError("reset"), "ConnectionLost"), (http_error(503), "ProviderBusy")):
            with self.subTest(last=expected):
                result, calls, waits = self.call([http_error(429), TimeoutError("timeout"), last, answer(OK)])
                self.assertEqual(type(result).__name__, expected)
                self.assertTrue(str(result).endswith(" (gave up after 3 attempts)"), str(result))
                self.assertEqual((calls, waits), (3, [2.0, 5.0]))

    def test_busy_does_not_add_lost_attempts(self):
        result, calls, waits = self.call([OSError("reset"), http_error(503), answer(OK)])
        self.assertEqual((result.lost_attempts, calls, waits), (1, 3, [2.0, 5.0]))
        result, _, _ = self.call([http_error(503), answer(OK)])
        self.assertEqual(result.lost_attempts, 0)

    def test_no_delays_preserves_original_message_and_cause(self):
        failure = urllib.error.URLError("reset")
        result, calls, waits = self.call([failure, answer(OK)], delays=())
        self.assertEqual(type(result).__name__, "ConnectionLost")
        self.assertEqual(str(result), f"URLError: {failure}")
        self.assertIs(result.__cause__, failure)
        self.assertEqual((calls, waits), (1, []))

    def test_certificates_json_unicode_and_nonbusy_http_never_retry(self):
        cert = ssl.SSLCertVerificationError(1, "certificate verify failed")
        bad_json, bad_unicode = MagicMock(), MagicMock()
        bad_json.__enter__.return_value.read.return_value = b"not JSON"
        bad_unicode.__enter__.return_value.read.return_value = b"\xff"
        for failure in (cert, urllib.error.URLError(cert), bad_json, bad_unicode, http_error(403)):
            with self.subTest(failure=type(failure).__name__):
                result, calls, waits = self.call([failure, answer(OK)])
                self.assertIs(type(result), ModelError)
                self.assertEqual((calls, waits), (1, []))


class LostUsageTest(TreeCase):
    def complete_with_loss(self, model):
        complete = model.complete

        def with_loss(messages):
            return replace(complete(messages), lost_attempts=2)

        return patch.object(model, "complete", side_effect=with_loss)

    def test_worker_unknown_attempts_do_not_consume_call_round_or_token_budget(self):
        c = self.contract(budget=Budget(max_rounds=2, max_model_calls=3, max_tokens=60))
        worker, reviewer = ScriptedModel([WRITE, FINISH], "w"), ScriptedModel([review("GO")], "r")
        with self.complete_with_loss(worker):
            report = Kernel(self.ledger, worker, reviewer).run(c)
        self.assertEqual(report.verdict, Verdict.PASS)
        calls = self.ledger.events(c.task_id, "model.called")
        self.assertEqual([e.payload.get("lost_attempts", 0) for e in calls], [2, 2, 0])
        self.assertNotIn("lost_attempts", calls[-1].payload)
        self.assertEqual(report.usage, {"model_calls": 3, "rounds": 2, "tokens": 60,
                                       "tokens_unknown_calls": 4, "commands": 0})

    def test_reviewer_records_lost_attempts(self):
        worker, reviewer = ScriptedModel([WRITE, FINISH], "w"), ScriptedModel([review("GO")], "r")
        with self.complete_with_loss(reviewer):
            report = Kernel(self.ledger, worker, reviewer).run(self.contract())
        self.assertEqual(report.verdict, Verdict.PASS)
        calls = self.ledger.events(report.task_id, "model.called")
        self.assertEqual([e.payload.get("lost_attempts", 0) for e in calls], [0, 0, 2])
        self.assertEqual(report.usage["tokens_unknown_calls"], 2)

    def test_planner_records_lost_attempts_without_changing_plan_reservations(self):
        c = self.parent()
        c = replace(c, subtasks=(), plan="model", acceptance=replace(c.acceptance, parts=PARTS))
        worker = ScriptedModel([GOOD_PLAN, *good_a(), *good_b()], "w")
        reviewer = ScriptedModel([review("GO")] * 3, "r")
        with self.complete_with_loss(worker):
            report = Kernel(self.ledger, worker, reviewer).run(c)
        self.assertEqual(report.verdict, Verdict.PASS, report.reasons)
        planner = self.ledger.events(c.task_id, "model.called")[0].payload
        self.assertEqual(planner["role"], "planner")
        self.assertEqual(planner["lost_attempts"], 2)
        self.assertEqual(report.usage["tokens_unknown_calls"], 10)
        self.assertEqual((report.usage["model_calls"], report.usage["rounds"], report.usage["tokens"]), (8, 4, 160))

    def test_unknown_success_usage_and_lost_attempts_both_count(self):
        self.ledger.append("usage", "model.called", {"role": "worker", "input_tokens": None,
                                                    "output_tokens": None, "lost_attempts": 2})
        self.assertEqual(self.ledger.usage("usage"), {"model_calls": 1, "rounds": 1, "tokens": 0,
                                                    "tokens_unknown_calls": 3, "commands": 0})


if __name__ == "__main__":
    unittest.main()
