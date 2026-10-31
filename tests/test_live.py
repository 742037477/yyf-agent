"""The observer reads real scripted runs over HTTP, without changing their ledger."""
import http.client
import io
import json
import re
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from m2k import live, trust
from m2k.cli import adjudication_inputs, main
from m2k.contract import Budget
from m2k.kernel import Kernel
from m2k.ledger import Event, Ledger
from m2k.models import ScriptedModel
from tests.helpers import GOOD_CODE, KernelCase, review, step
from tests.test_obs import CAPS, WitnessFiles


def event(seq, kind, payload=None, task="root"):
    return Event(seq, task, kind, None, payload or {}, "2026-10-19T00:00:00Z", "", "")


class MappingTest(unittest.TestCase):
    def test_workspace_and_outside_observations_map_to_the_worker(self):
        payload = {"added": ["a", "b"], "changed": ["c"], "removed": [], "files": 3}
        mapped = live.animation_steps([event(1, "workspace.changed", payload)])[0]
        self.assertEqual((mapped["station"], mapped["actor"]["do"]), ("worker", "edit"))
        self.assertEqual(mapped["caption"], "框架观测到工作区变化：新增 2、修改 1、删除 0")
        payload = {"added": [], "changed": [], "removed": ["x"] * 50, "more": {"removed": 3}}
        mapped = live.animation_steps([event(1, "workspace.changed", payload)])[0]
        self.assertEqual(mapped["actor"]["do"], "files")
        self.assertIn("删除 53", mapped["caption"])
        mapped = live.animation_steps([event(1, "outside.changed", payload)])[0]
        self.assertEqual((mapped["station"], mapped["state"], mapped["caption"]),
                         ("worker", "bad", "工作区外的监视路径被改动：53 处"))
        mapped = live.animation_steps([event(1, "outside.changed", {"unverified": ["x"]})])[0]
        self.assertEqual((mapped["station"], mapped["state"], mapped["caption"]),
                         ("worker", "unknown", "工作区外的监视路径无法核验"))

    def test_every_refusal_lamp_and_prefix(self):
        codes = {"CONFIG": 1, "NO_TRUST": 2, "TRUST_BROKEN": 2, "RELEASE_MISSING": 3,
                 "RELEASE_CONFIG": 3, "OBS_REQUIRED": 4, "OBS_CONFIG": 4, "OBS_UNAVAILABLE": 4,
                 "ANCHOR_CONFIG": 5, "ANCHOR_CHECK": 5, "ANCHOR_REQUIRED": 5, "FRAMEWORK_CHANGED": 6,
                 "CHARTER_MISSING": 7, "EXCEEDS_CHARTER": 7, "ROOT_CHANGED": 8, "RELEASE_ROLLBACK": 8,
                 "OBS_REFUSED": 8, "SANDBOX_UNAVAILABLE": 9, "SAME_MODEL": 10, "WORKER_PROBE": 11}
        for code, lamp in codes.items():
            with self.subTest(code=code):
                mapped = live.animation_steps([event(1, "startup.refused", {"task": "root", "code": code}, "m2k.startup")])[0]
                self.assertEqual(mapped["lamps"], {str(i): "ok" for i in range(1, lamp)} | {str(lamp): "bad"})
                self.assertEqual((mapped["state"], mapped["task"]), ("bad", "root"))

    def test_root_memory_failure_after_release_and_folded_receipt(self):
        events = [event(1, "release.checked", {"task": "root"}, "m2k.startup"),
                  event(2, "startup.refused", {"task": "root", "code": "OBS_UNAVAILABLE"}, "m2k.startup")]
        self.assertEqual(live.animation_steps(events)[0]["lamps"], {"8": "unknown"})
        events.append(event(3, "obs.receipt", {"receipt": {"body": {"ledger_seq": 1}}}, "m2k.startup"))
        mapped = live.animation_steps(events)
        self.assertEqual(len(mapped), 2)
        self.assertTrue(mapped[0]["obs"])
        self.assertEqual(mapped[0]["lamps"], {"8": "ok"})
        self.assertEqual(mapped[1]["lamps"]["8"], "bad")

    def test_startup_lamps_skip_warn_and_preserve_warning(self):
        events = [event(1, "anchor.warning"), event(2, "task.submitted", {"trust": None, "obs": None,
                  "sandbox": "none", "review": "same-model"})]
        lamps = {}
        for mapped in live.animation_steps(events):
            lamps.update(mapped.get("lamps", {}))
        self.assertEqual(lamps, {str(i): "skip" if i in (2, 3, 4, 7, 8) else "warn" if i in (5, 9, 10) else "ok"
                                 for i in range(1, 12)})

    def test_animals_ignore_case(self):
        for name, expected in (("DeepSeek", "whale"), ("DSH", "whale"), ("Claude", "crab"),
                               ("CODEX", "dragon"), ("GPT-6", "dragon"), ("OpenAI", "dragon"),
                               ("GLM-5", "fox"), ("local", "robot"), ("openai_compat:glm-5.3", "fox"),
                               ("openai_compat:deepseek-flash", "whale"), ("cli:claude-code", "crab")):
            with self.subTest(name=name):
                self.assertEqual(live.animal(name), expected)

    def test_stations_unknown_and_obs_failure(self):
        expected = {"task.submitted": "contract", "credential.issued": "cred", "candidate.frozen": "frozen",
                    "test.result": "test", "audit.result": "review", "verdict": "verdict", "gate.adjudication": "adjud",
                    "anchor.checkpoint": "anchor", "anchor.published": "anchor", "anchor.failed": "anchor",
                    "delivery.result": "gate", "delivery.refused": "gate", "gate.denied": "worker", "effect.intent": "worker",
                    "plan.accepted": "worker", "budget.reserved": "worker", "subtask.finished": "worker",
                    "repair.started": "worker", "worker.stopped": "worker", "model.error": "worker", "unknown.new": None}
        for kind, station in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(live.animation_steps([event(1, kind)])[0]["station"], station)
        failed = live.animation_steps([event(1, "obs.failed", {"kind": "test.result"})])[0]
        self.assertEqual((failed["station"], failed["state"]), ("test", "bad"))
        unknown = live.animation_steps([event(1, "future.event", {"prompt": "private"})])[0]
        self.assertEqual((unknown["station"], unknown["caption"]), (None, "future.event"))

    def test_actors_and_safe_tool_captions(self):
        submitted = event(1, "task.submitted", {"worker_model": "deepseek", "reviewer_model": "glm"})
        for tool, action in (("list_files", "files"), ("read_file", "files"), ("write_file", "edit"),
                             ("edit_file", "edit"), ("run_command", "cmd"), ("external_agent", "busy")):
            with self.subTest(tool=tool):
                mapped = live.animation_steps([submitted, event(2, "effect.intent", {"tool": tool,
                    "args": {"path": "calc.py", "content": "private", "prompt": "private"}})])[-1]
                self.assertEqual(mapped["actor"], {"role": "worker", "animal": "whale", "do": action})
                self.assertNotIn("private", mapped["caption"])
                if tool == "external_agent":
                    self.assertIn("框架看不到其内部过程", mapped["caption"])
        for kind, payload, role, action in (("credential.issued", {}, "worker", "enter"),
                ("effect.result", {"status": "COMPLETED", "output": "private"}, "worker", "home"),
                ("gate.denied", {"reason": "scope"}, "worker", "denied"),
                ("model.called", {"role": "planner"}, "planner", "think"),
                ("model.called", {"role": "reviewer"}, "reviewer", "enter"),
                ("audit.result", {"decision": "NO_GO", "verdict": "FAIL"}, "reviewer", "verdict")):
            with self.subTest(kind=kind, role=role):
                mapped = live.animation_steps([submitted, event(2, kind, payload)])[-1]
                self.assertEqual((mapped["actor"]["role"], mapped["actor"]["do"]), (role, action))
                self.assertNotIn("private", mapped["caption"])
        self.assertIn("NO_GO", mapped["caption"])

    def test_tokens_are_cumulative_and_unknown_is_sticky(self):
        events = [event(i, "model.called", {"role": "worker", "model": "dsh", "round": i,
                  "input_tokens": incoming, "output_tokens": outgoing})
                  for i, (incoming, outgoing) in enumerate(((0, 0), (10, 20), (None, 3), (1, 2)), 1)]
        mapped = live.animation_steps(events)
        self.assertEqual([s["detail"]["tokens"] for s in mapped], [0, 30, None, None])
        self.assertEqual(mapped[1]["detail"]["model"], "dsh")
        self.assertEqual(mapped[1]["detail"]["round"], 2)

    def test_subtask_tree_and_child_review_stay_at_worker(self):
        events = [event(1, "task.submitted", {"subtasks": [{"id": "a"}]}),
                  event(2, "subtask.started", {"id": "a", "task_id": "root.a"}),
                  event(3, "task.submitted", {"parent": "root", "worker_model": "claude"}, "root.a"),
                  event(4, "plan.accepted", {"subtasks": [{"id": "b"}]}, "root.a"),
                  event(5, "test.result", {"verdict": "PASS"}, "root.a"),
                  event(6, "model.called", {"role": "reviewer"}, "root.a"),
                  event(7, "audit.result", {"verdict": "PASS", "decision": "GO"}, "root.a"),
                  event(8, "verdict", {"verdict": "PASS"}, "root.a"),
                  event(9, "subtask.finished", {"id": "a", "task_id": "root.a", "verdict": "PASS"})]
        mapped = live.animation_steps(events)
        self.assertEqual(mapped[1]["detail"]["tree"], [["root.a", "run"]])
        self.assertEqual(mapped[-1]["detail"]["tree"], [["root.a", "PASS"], ["root.a.b", "pending"]])
        self.assertEqual([s["station"] for s in mapped[4:8]], ["worker"] * 4)


class LiveServerTest(WitnessFiles, KernelCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()

    def start_server(self, path=None, **kwargs):
        server = live.make_server(path or self.ledger.path, port=0, **kwargs)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            thread.join(5)
            server.server_close()
            self.assertFalse(thread.is_alive())
        self.addCleanup(stop)
        return server

    def request(self, server, path, method="GET"):
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            client.request(method, path)
            response = client.getresponse()
            raw = response.read()
            data = json.loads(raw) if raw and response.getheader("Content-Type").startswith("application/json") else raw
            return response.status, data
        finally:
            client.close()

    def scripted_run(self, trusted=False, observer=None):
        setup = trust.dev_setup(self.root / "trust", CAPS)[0] if trusted else None
        result = Kernel(self.ledger, ScriptedModel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")], "deepseek"),
                        ScriptedModel([review("GO")], "glm"), trust=setup, obs=observer).run(self.contract(budget=Budget(max_tokens=1000)))
        self.assertEqual(result.reported_verdict.value, "PASS", result.reasons)

    def test_full_scripted_pass_and_pagination(self):
        self.scripted_run(trusted=True)
        server = self.start_server(task="t1")
        code, data = self.request(server, "/api/steps")
        self.assertEqual((code, data["ledger"], data["done"]), (200, "ok", True))
        steps = data["steps"]
        stations = [s["station"] for i, s in enumerate(steps) if s["station"] is not None and
                    (i == 0 or steps[i - 1]["station"] != s["station"])]
        self.assertEqual(stations, ["contract", "cred", "worker", "worker", "worker", "frozen", "test", "review", "verdict", "adjud", "gate"])
        self.assertFalse(any(s["station"] == "verify" for s in steps))
        self.assertEqual(next(s["actor"]["animal"] for s in steps if s["kind"] == "model.called"), "whale")
        self.assertEqual(next(s["actor"]["animal"] for s in steps if s["kind"] == "audit.result"), "fox")
        received, after = [], 0
        while True:
            page = self.request(server, f"/api/steps?task=t1&after={after}&limit=3")[1]
            if not page["steps"]:
                break
            received.extend(page["steps"])
            after = page["last_seq"]
        self.assertEqual(received, steps)
        tasks = self.request(server, "/api/tasks")[1]["tasks"]
        self.assertEqual((tasks[0]["task"], tasks[0]["verdict"], tasks[0]["last_kind"]), ("t1", "PASS", "delivery.result"))

    def test_refused_only_task_is_listed_and_newest_first(self):
        self.ledger.append("old", "task.submitted", {})
        self.ledger.append("m2k.startup", "startup.refused", {"task": "blocked", "code": "NO_TRUST", "reason": "no trust"})
        server = self.start_server()
        tasks = self.request(server, "/api/tasks")[1]["tasks"]
        self.assertEqual([t["task"] for t in tasks], ["blocked", "old"])
        self.assertEqual(tasks[0]["refused"], "NO_TRUST")
        self.assertEqual(tasks[0]["first_seq"], tasks[0]["last_seq"])
        self.assertTrue(self.request(server, "/api/steps?task=blocked")[1]["done"])

    def test_descendants_startup_receipts_and_child_pagination_context(self):
        self.ledger.append("m2k.startup", "release.checked", {"task": "root"})
        self.ledger.append("m2k.startup", "obs.receipt", {"receipt": {"body": {"ledger_seq": 1}}})
        self.ledger.append("root", "task.submitted", {"subtasks": [{"id": "child"}]})
        self.ledger.append("root.child", "task.submitted", {"parent": "root"})
        self.ledger.append("root.child.grand", "task.submitted", {"parent": "root.child"})
        self.ledger.append("other", "task.submitted", {})
        self.ledger.append("root.child.grand", "model.called", {"role": "worker", "input_tokens": 2, "output_tokens": 4})
        server = self.start_server()
        data = self.request(server, "/api/steps?task=root")[1]
        self.assertEqual([s["seq"] for s in data["steps"]], [1, 3, 4, 5, 7])
        self.assertTrue(data["steps"][0]["obs"])
        tail = self.request(server, "/api/steps?task=root&after=5")[1]["steps"]
        self.assertEqual(tail, data["steps"][-1:])
        self.assertEqual(tail[0]["detail"]["tokens"], 6)
        self.assertEqual({t["task"] for t in self.request(server, "/api/tasks")[1]["tasks"]}, {"root", "other"})

    def test_done_means_final_stage_and_child_adjudication_does_not_finish_parent(self):
        self.ledger.append("root", "task.submitted", {})
        self.ledger.append("root.child", "task.submitted", {"parent": "root"})
        server = self.start_server()
        for kind in ("verdict", "gate.adjudication"):
            self.ledger.append("root.child", kind, {"verdict": "PASS"})
            self.assertFalse(self.request(server, "/api/steps?task=root")[1]["done"])
            self.assertFalse(self.request(server, "/api/steps?task=root.child")[1]["done"])
        self.ledger.append("root", "verdict", {"verdict": "PASS"})
        self.assertFalse(self.request(server, "/api/steps?task=root")[1]["done"])
        self.ledger.append("root", "gate.adjudication", {"verdict": "PASS"})
        data = self.request(server, "/api/steps?task=root")[1]
        self.assertTrue(data["done"])
        self.ledger.append("root", "anchor.checkpoint", {})
        tail = self.request(server, f"/api/steps?task=root&after={data['last_seq']}")[1]
        self.assertTrue(tail["done"])
        self.assertEqual(tail["steps"][0]["kind"], "anchor.checkpoint")

    def test_delivery_result_and_refused_are_terminal_without_adjudication(self):
        server = self.start_server()
        for tid, kind in (("delivered", "delivery.result"), ("denied", "delivery.refused")):
            self.ledger.append(tid, kind, {})
            self.assertTrue(self.request(server, "/api/steps?task=" + tid)[1]["done"])

    def test_trimmed_raw_events_do_not_include_large_or_signed_fields(self):
        payload = {"text": "x" * 301, "output": "secret-output", "prompt": "secret-prompt", "envelope": {"secret": 1},
                   "receipt": {"body": {}}, "signature_b64": "secret-signature", "a": {"b": {"c": {"d": 42}}}}
        self.ledger.append("root", "future.event", payload)
        server = self.start_server()
        code, data = self.request(server, "/api/events?task=root&limit=1")
        self.assertEqual(code, 200)
        p = data["events"][0]["payload"]
        self.assertEqual(p["text"], "x" * 300 + "…")
        self.assertEqual((p["output"], p["prompt"]), ("<13 chars>", "<13 chars>"))
        self.assertEqual(p["envelope"], "<signed>")
        self.assertEqual(p["receipt"], "<signed>")
        self.assertEqual(p["signature_b64"], "<signed>")
        self.assertEqual(p["a"]["b"]["c"]["d"], "…")
        self.assertEqual(self.request(server, "/api/events?task=root&after=1")[1]["events"], [])

    def test_invalid_parameters_and_all_non_get_methods_return_json(self):
        server = self.start_server()
        paths = ("/api/steps", "/api/steps?task=", "/api/steps?task=x&limit=501", "/api/events?task=x&limit=0",
                 "/api/steps?task=x&limit=abc", "/api/steps?task=x&after=-1", "/api/events?task=x&after=nan",
                 "/api/steps?task=x&task=y", "/api/tasks?task=x", "/api/adjudicate", "/api/adjudicate?task=x&after=0")
        for path in paths:
            with self.subTest(path=path):
                code, data = self.request(server, path)
                self.assertEqual(code, 400)
                self.assertIn("error", data)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "BREW"):
            with self.subTest(method=method):
                code, data = self.request(server, "/api/tasks", method)
                self.assertEqual((code, data), (405, {"error": "GET only"}))
        self.assertEqual(self.request(server, "/", "HEAD")[0], 405)
        self.assertEqual(self.request(server, "/unknown")[0], 404)

    def test_missing_ledger_does_not_create_directories_and_can_appear_later(self):
        path = self.root / "missing dir" / "账本 # %.sqlite"
        server = self.start_server(path=path)
        for endpoint, key in (("tasks", "tasks"), ("steps?task=root", "steps"), ("events?task=root", "events")):
            code, data = self.request(server, "/api/" + endpoint)
            self.assertEqual((code, data["ledger"], data[key]), (200, "missing", []))
        self.assertEqual(self.request(server, "/api/adjudicate?task=root")[1]["verdict"], "UNKNOWN")
        self.assertFalse(path.parent.exists())
        with Ledger(path) as ledger:
            ledger.append("root", "task.submitted", {})
        self.assertEqual(self.request(server, "/api/steps?task=root")[1]["ledger"], "ok")

    def test_requests_and_adjudication_preserve_bytes_mtimes_and_delete_private_copies(self):
        self.scripted_run()
        snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        server = self.start_server()
        copies, real_temp = [], tempfile.TemporaryDirectory
        def temporary(*args, **kwargs):
            directory = real_temp(*args, **kwargs)
            copies.append(Path(directory.name))
            return directory
        with patch("m2k.live.tempfile.TemporaryDirectory", side_effect=temporary), \
                patch("m2k.live.Ledger", wraps=Ledger) as constructor, patch("m2k.cli.build_model") as model, \
                patch("m2k.cli.obs.ObsClient") as observer, patch("m2k.cli.adjudication_inputs") as inputs:
            for _ in range(3):
                for path in ("/api/tasks", "/api/steps?task=t1", "/api/events?task=t1", "/api/adjudicate?task=t1"):
                    self.assertEqual(self.request(server, path)[0], 200)
            inputs.assert_not_called()
            model.assert_not_called()
            observer.assert_not_called()
            self.assertTrue(all(Path(call.args[0]) != self.ledger.path for call in constructor.call_args_list))
        self.assertTrue(copies)
        self.assertTrue(all(not directory.exists() for directory in copies))
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}, snapshot)

    def test_real_obs_forged_tail_requires_fresh_adjudication(self):
        observer, _, public, log = self.client()
        self.scripted_run(observer=observer)
        observer.close()
        original = self.ledger.last("t1", "verdict")
        forged = self.ledger.append("t1", "verdict", original.payload)
        self.assertTrue(self.ledger.verify_chain()[0])
        self.assertEqual(self.ledger.last("t1", "gate.adjudication").payload["verdict"], "PASS")
        cfg = {"obs": {"log": str(log), "public_key_b64": public}}
        server = self.start_server(cfg=cfg, base=self.root)
        before = (self.ledger.path.read_bytes(), self.ledger.path.stat().st_mtime_ns, log.read_bytes(), log.stat().st_mtime_ns)
        with patch("m2k.cli.adjudication_inputs", wraps=adjudication_inputs) as inputs:
            code, data = self.request(server, "/api/adjudicate?task=t1")
        self.assertEqual(code, 200)
        inputs.assert_called_once_with(cfg, self.root)
        self.assertNotEqual(data["verdict"], "PASS")
        self.assertIn(forged.seq, data["flagged_seqs"])
        steps = self.request(server, "/api/steps?task=t1")[1]["steps"]
        self.assertTrue(next(s["obs"] for s in steps if s["seq"] == original.seq))
        self.assertFalse(next(s["obs"] for s in steps if s["seq"] == forged.seq))
        self.assertEqual((self.ledger.path.read_bytes(), self.ledger.path.stat().st_mtime_ns, log.read_bytes(), log.stat().st_mtime_ns), before)

    def test_failed_adjudication_also_cleans_up_copy(self):
        server = self.start_server()
        copies, real_temp = [], tempfile.TemporaryDirectory
        def temporary(*args, **kwargs):
            directory = real_temp(*args, **kwargs)
            copies.append(Path(directory.name))
            return directory
        with patch("m2k.live.tempfile.TemporaryDirectory", side_effect=temporary), patch("m2k.live.adjudicate", side_effect=ValueError("bad input")):
            self.assertEqual(self.request(server, "/api/adjudicate?task=root"), (400, {"error": "bad input"}))
        self.assertTrue(all(not directory.exists() for directory in copies))

    def test_page_is_packaged_and_self_contained(self):
        server = self.start_server()
        code, page = self.request(server, "/")
        self.assertEqual(code, 200)
        text = page.decode("utf-8")
        for needle in ("/api/tasks", "/api/steps", "/api/events", "/api/adjudicate", 'id="stage"'):
            self.assertIn(needle, text)
        self.assertIsNone(re.search(r'(?:src\s*=|href\s*=|@import|url\()[\s\x22\x27]*https?://', text, re.I))

    def test_cli_resolves_config_relative_ledger_without_constructing_it(self):
        config = self.root / "config.toml"
        config.write_text('ledger = "state/ledger.sqlite"\n', encoding="utf-8")
        with patch("m2k.live.serve", return_value=0) as serve, patch("m2k.cli.Ledger") as constructor:
            self.assertEqual(main(["--config", str(config), "live", "--task", "t1", "--port", "9000", "--no-browser"]), 0)
        serve.assert_called_once_with(self.root / "state/ledger.sqlite", 9000, True, "t1", {"ledger": "state/ledger.sqlite"}, self.root)
        constructor.assert_not_called()

    def test_port_collision_is_one_line_and_ctrl_c_closes_server(self):
        server = self.start_server()
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(live.serve(self.ledger.path, port=server.server_port, no_browser=True), 1)
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        for no_browser in (False, True):
            with patch("m2k.live.ThreadingHTTPServer.serve_forever", side_effect=KeyboardInterrupt), \
                    patch("m2k.live.webbrowser.open") as browser, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(live.serve(self.ledger.path, port=0, no_browser=no_browser, task="t1"), 0)
                self.assertTrue(output.getvalue().startswith("http://127.0.0.1:"))
                self.assertEqual(browser.call_count, 0 if no_browser else 1)
