import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from m2k.models import ModelError, OpenAICompatModel, ScriptedModel, extract_json, read_secret
from m2k.worker import parse_proposal
from tools import pr_review


class ExtractTest(unittest.TestCase):
    def test_bare_fenced_and_surrounded_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(extract_json('ok:\n```json\n{"a": 2}\n```\nbye'), {"a": 2})
        self.assertEqual(extract_json('I will do {"a": 3} now'), {"a": 3})
        with self.assertRaises(ValueError):
            extract_json("no json here")

    def test_parse_proposal_requires_tool_and_args(self):
        p = parse_proposal('{"thought": "t", "tool": "read_file", "args": {"path": "a"}}')
        self.assertEqual((p.tool, p.args), ("read_file", {"path": "a"}))
        with self.assertRaises(ValueError):
            parse_proposal('{"tool": 3}')


class FakeAPI(BaseHTTPRequestHandler):
    reply: dict = {}
    status = 200
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.seen.append((self.path, self.headers.get("Authorization"), body))
        data = json.dumps(FakeAPI.reply).encode()
        self.send_response(FakeAPI.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class OpenAICompatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        os.environ["M2K_TEST_KEY"] = "sk-test"
        FakeAPI.status, FakeAPI.seen = 200, []
        self.m = OpenAICompatModel(self.base, "m1", api_key_env="M2K_TEST_KEY", max_tokens=99, timeout_s=5)

    def test_request_and_usage(self):
        FakeAPI.reply = {"model": "m1-2026", "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        r = self.m.complete([{"role": "user", "content": "x"}])
        self.assertEqual((r.text, r.input_tokens, r.output_tokens, r.model), ("hi", 12, 3, "m1-2026"))
        path, auth, body = FakeAPI.seen[0]
        self.assertEqual((path, auth, body["model"], body["max_tokens"]), ("/v1/chat/completions", "Bearer sk-test", "m1", 99))

    def test_missing_usage_is_none_not_zero(self):
        FakeAPI.reply = {"choices": [{"message": {"content": "hi"}}]}
        r = self.m.complete([{"role": "user", "content": "x"}])
        self.assertIsNone(r.input_tokens)

    def test_http_error_and_empty_length_cutoff_raise(self):
        FakeAPI.status, FakeAPI.reply = 429, {"error": "rate"}
        with self.assertRaises(ModelError):
            self.m.complete([{"role": "user", "content": "x"}])
        FakeAPI.status = 200
        FakeAPI.reply = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        with self.assertRaises(ModelError):
            self.m.complete([{"role": "user", "content": "x"}])

    def test_missing_key_raises_before_any_request(self):
        del os.environ["M2K_TEST_KEY"]
        with self.assertRaises(ModelError):
            self.m.complete([{"role": "user", "content": "x"}])
        self.assertEqual(FakeAPI.seen, [])

    def test_key_from_toml_file_field(self):
        FakeAPI.reply = {"choices": [{"message": {"content": "hi"}}]}
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "config.toml"
            f.write_text('[model_providers.ds]\nexperimental_bearer_token = "sk-file"\n', encoding="utf-8")
            m = OpenAICompatModel(self.base, "m1", api_key_file=str(f),
                                  api_key_field="model_providers.ds.experimental_bearer_token", timeout_s=5)
            m.complete([{"role": "user", "content": "x"}])
        self.assertEqual(FakeAPI.seen[-1][1], "Bearer sk-file")


class ConnectionErrorsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"T11_DUMMY_KEY": "dummy"}))
        self.model = OpenAICompatModel("https://example.invalid/v1", "test", api_key_env="T11_DUMMY_KEY")

    def assert_wrapped(self, error):
        with self.assertRaises(ModelError) as caught:
            self.model.complete([{"role": "user", "content": "x"}])
        self.assertEqual(str(caught.exception), f"{type(error).__name__}: {error}")
        self.assertIs(caught.exception.__cause__, error)

    def test_request_network_failures_are_model_errors(self):
        errors = (http.client.RemoteDisconnected("peer closed"), ConnectionResetError("connection reset"),
                  OSError("network down"), TimeoutError("timed out"), urllib.error.URLError("unreachable"),
                  http.client.HTTPException("bad HTTP response"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch("urllib.request.urlopen", side_effect=error) as request:
                    self.assert_wrapped(error)
                request.assert_called_once()

    def test_response_read_failures_are_model_errors(self):
        for error in (ConnectionResetError("read reset"), http.client.IncompleteRead(b"partial", 8)):
            with self.subTest(error=type(error).__name__):
                response = Mock()
                response.read.side_effect = error
                with patch("urllib.request.urlopen") as request:
                    request.return_value.__enter__.return_value = response
                    self.assert_wrapped(error)
                response.read.assert_called_once_with()

    def test_invalid_utf8_and_json_are_model_errors(self):
        for raw, kind in ((b"\xff\xfe", UnicodeDecodeError), (b"{", json.JSONDecodeError)):
            with self.subTest(body=raw):
                with patch("urllib.request.urlopen") as request:
                    request.return_value.__enter__.return_value.read.return_value = raw
                    with self.assertRaises(ModelError) as caught:
                        self.model.complete([])
                self.assertIsInstance(caught.exception.__cause__, kind)
                self.assertEqual(str(caught.exception), f"{kind.__name__}: {caught.exception.__cause__}")

    def test_http_error_keeps_status_and_body_detail(self):
        for raw, detail in ((b"rate limited", "rate limited"), (b"\xff", "\ufffd")):
            with self.subTest(body=raw):
                error = urllib.error.HTTPError(self.model.url, 429, "rate", {}, io.BytesIO(raw))
                with patch("urllib.request.urlopen", side_effect=error):
                    with self.assertRaises(ModelError) as caught:
                        self.model.complete([])
                self.assertEqual(str(caught.exception), f"HTTP 429: {detail}")
                self.assertIs(caught.exception.__cause__, error)

    def test_interrupted_http_error_body_is_also_a_model_error(self):
        for read_error in (ConnectionResetError("error body reset"), http.client.IncompleteRead(b"partial", 8)):
            with self.subTest(error=type(read_error).__name__):
                body = Mock()
                body.read.side_effect = read_error
                error = urllib.error.HTTPError(self.model.url, 502, "bad gateway", {}, body)
                with patch("urllib.request.urlopen", side_effect=error):
                    self.assert_wrapped(read_error)


class PRReviewErrorsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"GLM_API_KEY": "dummy", "PR_TITLE": "T11: test",
                                                  "BASE_REF": "main"}))
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.card = Mock()
        self.card.read_text.return_value = "Test task card"
        self.find = self.enterContext(patch.object(pr_review, "find_card", return_value=self.card))
        self.git = self.enterContext(patch.object(pr_review.subprocess, "run", return_value=Mock(stdout="test diff")))
        self.post = self.enterContext(patch.object(pr_review, "comment"))
        self.model = ScriptedModel([json.dumps({"decision": "GO", "summary": "correct"})])
        self.model.model = "test-reviewer"
        self.build = self.enterContext(patch.object(pr_review, "OpenAICompatModel", return_value=self.model))

    def assert_unknown(self, error):
        self.assertEqual(pr_review.main(), 1)
        self.post.assert_called_once()
        body = self.post.call_args.args[0]
        self.assertTrue(body.startswith("### GLM review: UNKNOWN"), body)
        self.assertIn(f"{type(error).__name__}: {error}", body)

    def test_go_and_no_go_still_post_one_review(self):
        for decision, status in (("GO", 0), ("NO_GO", 1)):
            with self.subTest(decision=decision):
                self.post.reset_mock()
                self.model.steps = [json.dumps({"decision": decision, "summary": "review summary",
                                               "blocking": ["models.py:1 - test reason"], "notes": ["style note"]})]
                self.assertEqual(pr_review.main(), status)
                self.post.assert_called_once()
                body = self.post.call_args.args[0]
                self.assertIn(f"### GLM review (test-reviewer): {decision}", body)
                self.assertIn("review summary", body)
                self.assertIn("models.py:1 - test reason", body)
                self.assertIn("Non-blocking notes", body)
                self.assertIn("test diff", self.model.calls[-1][0]["content"])

    def test_model_failure_posts_existing_unknown_comment(self):
        self.model.steps = []
        self.assertEqual(pr_review.main(), 1)
        self.post.assert_called_once_with(
            "### GLM review: UNKNOWN\nThe review could not be completed: `scripted model has no more replies`. "
            "A human/Claude review is required.")

    def test_dropped_connection_posts_unknown(self):
        self.build.return_value = OpenAICompatModel("https://example.invalid/v1", "test", api_key_env="GLM_API_KEY")
        error = http.client.RemoteDisconnected("peer closed")
        with patch("urllib.request.urlopen", side_effect=error):
            self.assert_unknown(error)

    def test_finding_card_failure_including_system_exit_posts_unknown(self):
        for error in (RuntimeError("card lookup failed"), SystemExit("card not found")):
            with self.subTest(error=type(error).__name__):
                self.post.reset_mock()
                self.find.side_effect = error
                self.assert_unknown(error)
                self.assertEqual(self.model.calls, [])

    def test_git_failure_posts_unknown(self):
        error = pr_review.subprocess.CalledProcessError(128, ["git", "diff"])
        self.git.side_effect = error
        self.assert_unknown(error)
        self.assertEqual(self.model.calls, [])

    def test_reading_card_and_building_prompt_failures_post_unknown(self):
        error = OSError("cannot read task card")
        self.card.read_text.side_effect = error
        self.assert_unknown(error)
        self.post.reset_mock()
        self.card.read_text.side_effect = None
        error = ValueError("invalid prompt format")
        with patch.object(pr_review, "PROMPT", Mock(format=Mock(side_effect=error))):
            self.assert_unknown(error)
        self.assertEqual(self.model.calls, [])

    def test_comment_failure_prints_error_and_never_retries(self):
        for failing_model in (False, True):
            with self.subTest(failing_model=failing_model):
                self.post.reset_mock()
                self.model.steps = [] if failing_model else [json.dumps({"decision": "GO"})]
                self.post.side_effect = ConnectionResetError("comment connection reset")
                self.assertEqual(pr_review.main(), 1)
                self.post.assert_called_once()
                self.assertIn("ConnectionResetError: comment connection reset", self.output.getvalue())


class SecretTest(unittest.TestCase):
    def test_plain_json_and_errors_never_show_the_value(self):
        with tempfile.TemporaryDirectory() as d:
            plain, js = Path(d) / "k.txt", Path(d) / "k.json"
            plain.write_text("  sk-plain\n", encoding="utf-8")
            js.write_text('{"a": {"b": "sk-json"}}', encoding="utf-8")
            self.assertEqual(read_secret(str(plain), None), "sk-plain")
            self.assertEqual(read_secret(str(js), "a.b"), "sk-json")
            with self.assertRaises(ModelError) as cm:
                read_secret(str(js), "a.c")
            self.assertNotIn("sk-json", str(cm.exception))
            with self.assertRaises(ModelError):
                read_secret(str(Path(d) / "missing.txt"), None)


if __name__ == "__main__":
    unittest.main()
