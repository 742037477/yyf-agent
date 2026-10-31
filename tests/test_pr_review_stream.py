import contextlib
import http.client
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import Mock, patch

from m2k.models import ScriptedModel
from tools import pr_review


def event(chunk):
    return b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"


def content(text, **extra):
    return event({"choices": [{"delta": {"content": text, **extra}}]})


DONE = b"data: [DONE]\n\n"


class FakeResponse:
    def __init__(self, lines):
        self.lines = iter(lines)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self.lines)
        if isinstance(line, Exception):
            raise line
        return line


class PRReviewStreamTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "GLM_API_KEY": "dummy-stream-key", "GLM_BASE_URL": "https://example.invalid/v1/",
            "GLM_MODEL": "stream-reviewer", "PR_TITLE": "T13: test", "BASE_REF": "main",
        }))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        card = Mock()
        card.read_text.return_value = "Test task card"
        self.enterContext(patch.object(pr_review, "find_card", return_value=card))
        self.enterContext(patch.object(pr_review.subprocess, "run", return_value=Mock(stdout="test diff")))
        self.post = self.enterContext(patch.object(pr_review, "comment"))
        self.request = self.enterContext(patch("urllib.request.urlopen"))
        self.model = ScriptedModel([json.dumps({"decision": "GO", "summary": "范围正确"}, ensure_ascii=False)])
        self.reply = self.model.complete([{"role": "user", "content": "Review the test diff."}]).text

    def run_stream(self, lines):
        response = FakeResponse(lines)
        self.request.return_value = response
        status = pr_review.main()
        self.assertTrue(response.closed)
        self.post.assert_called_once()
        return status, self.post.call_args.args[0]

    def assert_unknown(self, lines):
        status, body = self.run_stream(lines)
        self.assertEqual(status, 1)
        self.assertTrue(body.startswith("### GLM review: UNKNOWN"), body)
        self.assertIn("A human/Claude review is required.", body)
        return body

    def test_content_only_joined_and_last_usage_chunk_used(self):
        text = "```json\n" + self.reply + "\n```"
        lines = [b": keepalive\n", b"event: message\n", b"\n",
                 event({"choices": [{"delta": {"role": "assistant", "reasoning_content": "Considering scope."}}]}),
                 content(text[:12], reasoning_content="Checking the card."),
                 event({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
                 content(text[12:25]), content(None, reasoning_content="Reviewing tests."),
                 content(text[25:]), event({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": None}),
                 event({"choices": [], "usage": {"prompt_tokens": 42, "completion_tokens": 7}}), DONE,
                 ConnectionResetError("must not read past DONE")]
        with patch.object(pr_review, "extract_json", wraps=pr_review.extract_json) as extract:
            status, body = self.run_stream(lines)
        extract.assert_called_once_with(text)
        self.assertEqual(status, 0)
        self.assertIn("### GLM review (stream-reviewer): GO", body)
        self.assertIn("范围正确", body)
        self.assertIn("_tokens in/out: 42/7. Final acceptance: Claude review._", body)

    def test_request_stream_usage_config_and_per_read_timeout(self):
        self.assertEqual(self.run_stream([content(self.reply), DONE])[0], 0)
        self.request.assert_called_once()
        req = self.request.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://example.invalid/v1/chat/completions")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer dummy-stream-key")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertIs(body["stream"], True)
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual((body["model"], body["max_tokens"]), ("stream-reviewer", 32768))
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertIn("Test task card", body["messages"][0]["content"])
        self.assertIn("test diff", body["messages"][0]["content"])
        self.assertEqual(self.request.call_args.kwargs, {"timeout": 120})

    def test_default_url_and_model_unchanged(self):
        with patch.dict(os.environ, {"GLM_BASE_URL": "", "GLM_MODEL": ""}):
            status, body = self.run_stream([content(self.reply), DONE])
        self.assertEqual(status, 0)
        req = self.request.call_args.args[0]
        self.assertEqual(req.full_url, "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
        self.assertEqual(json.loads(req.data)["model"], "glm-5.3")
        self.assertIn("### GLM review (glm-5.3): GO", body)

    def test_missing_usage_is_none(self):
        status, body = self.run_stream([content(self.reply), DONE])
        self.assertEqual(status, 0)
        self.assertIn("_tokens in/out: None/None.", body)

    def test_partial_usage_preserves_unknown_and_zero(self):
        status, body = self.run_stream([content(self.reply), event({"choices": [], "usage": {"prompt_tokens": 0}}), DONE])
        self.assertEqual(status, 0)
        self.assertIn("_tokens in/out: 0/None.", body)

    def test_missing_done_is_unknown_even_after_valid_reply(self):
        body = self.assert_unknown([content(self.reply)])
        self.assertIn("[DONE]", body)

    def test_midstream_connection_reset_timeout_and_http_read_error(self):
        for error in (ConnectionResetError("read reset"), TimeoutError("stream stalled"),
                      http.client.IncompleteRead(b"partial", 8)):
            with self.subTest(error=type(error).__name__):
                self.post.reset_mock()
                body = self.assert_unknown([content(self.reply), error])
                self.assertIn(f"{type(error).__name__}: {error}", body)

    def test_request_failures_post_unknown(self):
        errors = (urllib.error.HTTPError("https://example.invalid", 429, "rate", {}, io.BytesIO(b"rate limited")),
                  urllib.error.URLError("unreachable"), ConnectionResetError("connection reset"),
                  TimeoutError("connect timeout"), http.client.RemoteDisconnected("peer closed"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.post.reset_mock()
                self.request.reset_mock()
                self.request.side_effect = error
                self.assertEqual(pr_review.main(), 1)
                self.request.assert_called_once()
                self.post.assert_called_once()
                self.assertTrue(self.post.call_args.args[0].startswith("### GLM review: UNKNOWN"))

    def test_invalid_utf8_json_and_chunk_shape_are_unknown(self):
        for raw in (b"data: \xff\n", b"data: {\n", b"data: []\n", content(123)):
            with self.subTest(raw=raw):
                self.post.reset_mock()
                self.assert_unknown([raw, DONE])

    def test_empty_or_reasoning_only_reply_is_unknown(self):
        for lines in ([DONE], [content(" \t"), DONE], [content(None, reasoning_content="Considering scope."), DONE]):
            with self.subTest(lines=lines):
                self.post.reset_mock()
                self.assert_unknown(lines)

    def test_non_json_reply_is_unknown(self):
        self.assert_unknown([content("No structured review."), DONE])

    def test_no_go_comment_and_exit_code_unchanged(self):
        text = json.dumps({"decision": "NO_GO", "summary": "Needs a fix", "blocking": ["pr_review.py:1 - fix"],
                           "notes": ["style note"]})
        status, body = self.run_stream([content(text), DONE])
        self.assertEqual(status, 1)
        self.assertIn("### GLM review (stream-reviewer): NO_GO", body)
        self.assertIn("**Blocking (must fix inside this card):**\n- pr_review.py:1 - fix", body)
        self.assertIn("<details><summary>Non-blocking notes (do NOT act on these in this PR)</summary>", body)
        self.assertIn("- style note", body)


if __name__ == "__main__":
    unittest.main()
