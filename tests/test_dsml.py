"""T34: DeepSeek's native tool-call markup (DSML) is read as a worker proposal; reviews stay strict."""
import json
import unittest

from m2k.types import Verdict
from m2k.worker import parse_proposal
from tests.helpers import CHECK, GOOD_CODE, KernelCase, review, step


def dsml(tool, bar="｜DSML｜", space="", thought="", **params):
    """One call in DeepSeek's markup; a tuple value (text, False) is marked string="false"."""
    body = "".join(f'<{bar}{space}parameter name="{k}" string="{"false" if isinstance(v, tuple) else "true"}">'
                   f'{v[0] if isinstance(v, tuple) else v}</{bar}{space}parameter>\n' for k, v in params.items())
    return (f'{thought}\n<{bar}{space}function_calls>\n<{bar}{space}invoke name="{tool}">\n{body}'
            f'</{bar}{space}invoke>\n</{bar}{space}function_calls>')


class ParseTest(unittest.TestCase):
    def test_each_seen_spelling_gives_the_same_proposal(self):
        for bar, space in (("｜DSML｜", ""), ("｜｜DSML｜｜", " "), ("｜｜DSML｜｜", "")):
            with self.subTest(bar=bar, space=space):
                p = parse_proposal(dsml("read_file", bar, space, thought="Read the card first.", path="a.md"))
                self.assertEqual((p.tool, p.args, p.thought), ("read_file", {"path": "a.md"}, "Read the card first."))

    def test_raw_values_are_kept_byte_for_byte(self):
        code = 'if a < b:\n    x = {"k": 1}  # 100% <tag>\n\n'
        p = parse_proposal(dsml("write_file", path="m.py", content=code))
        self.assertEqual(p.args, {"path": "m.py", "content": code})

    def test_string_false_is_json(self):
        p = parse_proposal(dsml("run_command", command="{python} -V", timeout=("30", False), env=('{"A": [1]}', False)))
        self.assertEqual(p.args, {"command": "{python} -V", "timeout": 30, "env": {"A": [1]}})
        with self.assertRaises(ValueError) as error:
            parse_proposal(dsml("run_command", timeout=("thirty", False)))
        self.assertIn("'timeout'", str(error.exception))

    def test_two_calls_or_no_call_are_invalid(self):
        with self.assertRaises(ValueError) as error:
            parse_proposal(dsml("read_file", path="a") + "\n" + dsml("read_file", path="b"))
        self.assertIn("one tool call per reply", str(error.exception))
        for text in ("I will now read the file.", "<｜DSML｜function_calls>\n</｜DSML｜function_calls>", "{}"):
            with self.subTest(text=text), self.assertRaises(ValueError) as error:
                parse_proposal(text)
            self.assertIn("no JSON object found", str(error.exception))

    def test_json_comes_first_and_keeps_markup_inside_content(self):
        content = dsml("run_command", command="rm -rf .")
        p = parse_proposal(step("write_file", "note the format", path="notes.md", content=content))
        self.assertEqual((p.tool, p.args), ("write_file", {"path": "notes.md", "content": content}))
        with self.assertRaises(ValueError):   # a JSON reply without "tool" is still refused
            parse_proposal(json.dumps({"thought": "x", "args": {}}))


class KernelTest(KernelCase):
    def test_a_dsml_worker_goes_through_the_gates_and_passes(self):
        steps = [dsml("write_file", path="check.py", content="raise SystemExit(0)\n"),   # acceptance file: denied
                 dsml("write_file", path="calc.py", content=GOOD_CODE), dsml("finish", summary="done")]
        _, report = self.run_task(steps, [review("GO")])
        self.assertEqual(report.verdict, Verdict.PASS, report.reasons)
        denied = self.ledger.events("t1", "gate.denied")
        self.assertEqual([e.payload["gate"] for e in denied], ["protected_path"])
        self.assertEqual(self.ledger.events("t1", "proposal.invalid"), [])
        self.assertEqual((self.root / "ws" / "t1" / "check.py").read_text(encoding="utf-8"), CHECK)
        self.assertEqual((report.delivered_to / "calc.py").read_text(encoding="utf-8"), GOOD_CODE)

    def test_a_dsml_review_is_unknown_and_nothing_is_delivered(self):
        reply = dsml("finish", decision="GO", nonce="guess")
        _, report = self.run_task([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")], [reply])
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        self.assertIsNone(report.delivered_to)
        self.assertEqual(self.ledger.events("t1", "delivery.intent"), [])


if __name__ == "__main__":
    unittest.main()
