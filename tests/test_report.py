import io
import json
import subprocess
import sys
from contextlib import chdir, redirect_stdout
from html import escape
from html.parser import HTMLParser
from unittest.mock import patch

from m2k.cli import main
from m2k.kernel import Kernel
from m2k.models import ScriptedModel
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, review, step


class Timeline(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.rows, self.details, self.tags = [], 0, []
        self.row, self.in_details = None, False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "tr":
            self.row = {"class": dict(attrs).get("class", ""), "text": ""}
        elif tag == "details":
            self.details += 1
            self.in_details = True

    def handle_endtag(self, tag):
        if tag == "tr":
            self.rows.append(self.row)
            self.row = None
        elif tag == "details":
            self.in_details = False

    def handle_data(self, text):
        if self.row is not None and not self.in_details:
            self.row["text"] += text


class ReportTest(KernelCase):
    def report(self, task_id="t1", out=None):
        path = out or self.root / "reports" / "timeline.html"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["--ledger", str(self.ledger.path), "report", task_id, "--out", str(path)])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertEqual(stdout.getvalue().strip(), str(path))
        return path.read_text(encoding="utf-8")

    def test_pass_report_header_timeline_and_ledger_unchanged(self):
        c, result = self.run_task([
            step("write_file", path="../escape.py", content="x"),
            step("write_file", thought="实现加法", path="calc.py", content=GOOD_CODE),
            step("run_command", command="{python} check.py"), step("finish", summary="完成")
        ], [review("GO", "正确")])
        before = self.ledger.events()
        with patch.object(self.ledger, "append", side_effect=AssertionError("report must be read-only")):
            from m2k.report import write_report
            path = write_report(self.ledger, "t1", self.root / "pass.html")
        text = path.read_text(encoding="utf-8")
        parsed = Timeline(text)
        self.assertEqual(len(parsed.rows) - 1, len(before))
        self.assertEqual(parsed.details, len(before))
        for row, event in zip(parsed.rows[1:], before):
            self.assertIn(event.ts, row["text"])
            self.assertIn(event.kind, row["text"])
        self.assertIn(c.goal, text)
        self.assertIn('class="pass"', text)
        self.assertIn("PASS", text)
        self.assertIn("FINISHED", text)
        self.assertIn("哈希链", text)
        self.assertIn("通过", text)
        for key, label in (("rounds", "轮次"), ("model_calls", "模型调用"),
                           ("tokens", "已知令牌"), ("tokens_unknown_calls", "用量未知调用"),
                           ("commands", "命令")):
            self.assertIn(f"{label}：{result.usage[key]}", text)
        denied = next(row for row in parsed.rows if "gate.denied" in row["text"])
        self.assertEqual(denied["class"], "fail")
        self.assertIn("path_scope", denied["text"])
        self.assertIn("第 2 轮：write_file calc.py — 实现加法", text)
        self.assertEqual(self.ledger.events(), before)
        self.report()
        self.assertEqual(self.ledger.events(), before)
        self.assertTrue(self.ledger.verify_chain()[0])
        for tag, attrs in parsed.tags:
            self.assertNotIn(tag, ("script", "link", "iframe", "img"))
            self.assertNotIn("src", attrs)

    def test_status_and_report_show_same_and_cross_model_review(self):
        for mode, name, label in (("same-model", "worker", "同源审查"),
                                  ("cross-model", "reviewer", "跨模型审查")):
            with self.subTest(mode=mode):
                contract = self.contract(task_id=mode, deliver=False)
                worker = ScriptedModel([step("write_file", path="calc.py", content=GOOD_CODE),
                                        step("finish")], "worker")
                reviewer = ScriptedModel([review("GO")], name)
                result = Kernel(self.ledger, worker, reviewer).run(contract)
                self.assertEqual(result.verdict.value, "PASS", result.reasons)
                self.assertEqual(self.ledger.last(mode, "task.submitted").payload["review"], mode)
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(["--ledger", str(self.ledger.path), "status", mode]), 0)
                self.assertEqual(json.loads(output.getvalue())["review"], label)
                self.assertIn(f"<dt>审查模式</dt><dd>{label}</dd>", self.report(mode))

    def test_legacy_task_review_mode_stays_unrecorded(self):
        self.ledger.append("legacy", "task.submitted", {"goal": "旧任务"})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--ledger", str(self.ledger.path), "status", "legacy"]), 0)
        self.assertIsNone(json.loads(output.getvalue())["review"])
        self.assertIn("<dt>审查模式</dt><dd>未记录</dd>", self.report("legacy"))

    def test_fail_report_and_effect_output_preview(self):
        self.run_task([step("write_file", path="calc.py", content=BAD_CODE), step("finish")],
                      [review("GO")])
        output = "长" * 300 + "PREVIEW_END"
        for state in ("FAILED", "UNKNOWN"):
            self.ledger.append("t1", "effect.result", {"status": state, "output": output})
        before = self.ledger.events()
        text = self.report()
        parsed = Timeline(text)
        verdict = next(row for row in parsed.rows if "verdict" in row["text"] and "FAIL" in row["text"])
        self.assertEqual(verdict["class"], "fail")
        self.assertIn("test: FAIL", verdict["text"])
        effects = [row for row in parsed.rows if "长" in row["text"]]
        self.assertEqual([row["class"] for row in effects], ["fail", "unknown"])
        for row in effects:
            self.assertIn("长" * 300, row["text"])
            self.assertNotIn("PREVIEW_END", row["text"])
        self.assertIn("PREVIEW_END", text)  # raw payload keeps the full output
        self.assertEqual(self.ledger.events(), before)

    def test_unknown_and_absent_verdict_and_unknown_usage(self):
        self.run_task([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")],
                      ["unparseable review"])
        self.ledger.append("t1", "model.called", {"role": "worker", "model": "scripted"})
        text = self.report()
        self.assertIn('class="unknown"', text)
        self.assertIn("未知（UNKNOWN）", text)
        self.assertIn("用量未知调用：1", text)
        self.ledger.append("pending", "task.submitted", {"goal": "等待执行"})
        text = self.report("pending")
        self.assertIn('class="none"', text)
        self.assertIn("无判定", text)
        self.assertIn("未记录", text)

    def test_all_ledger_text_is_escaped(self):
        payload = '<script>alert("x")</script> & <img src="https://example.invalid/x">'
        task_id = '任务<&"'
        self.ledger.append(task_id, "task.submitted", {"goal": payload})
        self.ledger.append(task_id, "proposal", {
            "round": 3, "tool": payload, "args": {"path": payload}, "thought": payload})
        self.ledger.append(task_id, payload, {"nested": [payload]})
        self.ledger.append(task_id, "worker.stopped", {"status": payload})
        text = self.report(task_id)
        self.assertIn(escape(payload), text)
        self.assertIn(escape(task_id), text)
        self.assertIn(escape(json.dumps({"nested": [payload]}, ensure_ascii=False, indent=2)), text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("<img", text)

    def test_default_output_path(self):
        self.run_task([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")],
                      [review("GO")])
        stdout = io.StringIO()
        with chdir(self.root), redirect_stdout(stdout):
            self.assertEqual(main(["--ledger", str(self.ledger.path), "report", "t1"]), 0)
        expected = self.root / ".m2k" / "reports" / "t1.html"
        self.assertTrue(expected.is_file())
        self.assertEqual(stdout.getvalue().strip(), str(expected.relative_to(self.root)))

    def test_broken_hash_chain_is_reported(self):
        self.ledger.append("t1", "task.submitted", {"goal": "检查账本"})
        self.ledger._db.execute("UPDATE events SET hash = ? WHERE seq = 1", ("broken",))
        text = self.report()
        self.assertIn("断裂", text)
        self.assertIn("事件序号 1", text)
        self.assertFalse(self.ledger.verify_chain()[0])

    def test_unknown_task_exits_one_without_report_or_events(self):
        path = self.root / "missing.html"
        before = self.ledger.events()
        result = subprocess.run([sys.executable, "-m", "m2k", "--ledger", str(self.ledger.path),
                                 "report", "missing", "--out", str(path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown task", result.stderr)
        self.assertIn("missing", result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(self.ledger.events(), before)

    def test_output_cannot_overwrite_ledger(self):
        from m2k.report import write_report
        self.ledger.append("t1", "task.submitted", {"goal": "保留账本"})
        before = self.ledger.events()
        with self.assertRaisesRegex(ValueError, "账本"):
            write_report(self.ledger, "t1", self.ledger.path)
        self.assertEqual(self.ledger.events(), before)
        self.assertTrue(self.ledger.verify_chain()[0])
