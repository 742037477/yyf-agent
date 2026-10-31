"""T36: a busy provider (429/5xx, or an error body without choices) is asked again after a wait; nothing else is."""
import io
import json
import unittest
import urllib.error
from dataclasses import replace
from unittest.mock import MagicMock, patch

from m2k.kernel import Kernel, status
from m2k.models import ModelError, OpenAICompatModel, ProviderBusy, ScriptedModel, build_model
from tests.test_model_plans import PARTS
from tests.test_task_tree import TreeCase

OK = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
URL = "https://example.invalid/v1"


def http(code, body=b"busy"):
    return urllib.error.HTTPError(URL + "/chat/completions", code, "x", {}, io.BytesIO(body))


def answer(data):
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(data).encode()
    return resp


class AdapterTest(unittest.TestCase):
    def model(self, delays=(60, 240)):
        return OpenAICompatModel(URL, "m", api_key_env="T36_KEY", retry_delays=delays)

    def call(self, model, outcomes):
        with patch.dict("os.environ", {"T36_KEY": "k"}), patch("urllib.request.urlopen", side_effect=outcomes) as req, \
                patch("m2k.models.time.sleep") as sleep:
            try:
                return model.complete([{"role": "user", "content": "x"}]), req.call_count, [c.args[0] for c in sleep.call_args_list]
            except ModelError as e:
                return e, req.call_count, [c.args[0] for c in sleep.call_args_list]

    def test_busy_then_answer(self):
        for busy in (http(503), http(429), http(500), http(502), http(504),
                     answer({"error": {"message": "We were unable to start processing your request"}})):
            with self.subTest(busy=busy):
                reply, calls, waits = self.call(self.model(), [busy, answer(OK)])
                self.assertEqual((reply.text, calls, waits), ("hi", 2, [60.0]))

    def test_gives_up_after_the_last_delay(self):
        error, calls, waits = self.call(self.model((1, 2)), [http(503, b"too busy") for _ in range(3)])
        self.assertIsInstance(error, ProviderBusy)
        self.assertEqual(str(error), "HTTP 503: too busy (gave up after 3 attempts)")
        self.assertEqual((calls, waits), (3, [1.0, 2.0]))

    def test_other_failures_are_never_resent(self):
        cases = {"bad request": http(400), "unauthorised": http(401),
                 "cut off": answer({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})}
        for name, outcome in cases.items():
            with self.subTest(name):
                error, calls, waits = self.call(self.model(), [outcome, answer(OK)])
                self.assertIsInstance(error, ModelError)
                self.assertNotIsInstance(error, ProviderBusy)
                self.assertEqual((calls, waits), (1, []))

    def test_default_is_one_attempt_with_the_old_message(self):
        error, calls, waits = self.call(self.model(()), [http(503, b"too busy"), answer(OK)])
        self.assertEqual((str(error), calls, waits), ("HTTP 503: too busy", 1, []))
        self.assertIsInstance(error.__cause__, urllib.error.HTTPError)

    def test_config_list_from_toml(self):
        m = build_model({"base_url": URL, "model": "m", "retry_delays": [60, 240]})
        self.assertEqual(m.retry_delays, (60.0, 240.0))
        self.assertEqual(build_model({"base_url": URL, "model": "m"}).retry_delays, ())


class PlannerErrorTest(TreeCase):
    def test_a_failed_planner_call_is_a_model_error_not_a_rejected_plan(self):
        def busy(messages):
            raise ProviderBusy("HTTP 503: too busy (gave up after 3 attempts)")

        c = self.parent()
        c = replace(c, subtasks=(), plan="model", acceptance=replace(c.acceptance, parts=PARTS))
        r = Kernel(self.ledger, ScriptedModel([busy], "w"), ScriptedModel([], "r")).run(c)
        self.assertEqual(r.verdict.value, "FAIL")
        worker = status(self.ledger, "p1")["worker"]
        self.assertEqual(worker["status"], "MODEL_ERROR")
        self.assertEqual(worker["reason"], "planner: HTTP 503: too busy (gave up after 3 attempts)")
        self.assertEqual(self.ledger.events("p1", "plan.rejected"), [])
        self.assertEqual(self.ledger.events("p1", "subtask.started"), [])


if __name__ == "__main__":
    unittest.main()
