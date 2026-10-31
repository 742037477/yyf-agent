"""T37: a vendor agent CLI as the worker. M2 governs its conclusion: chain first, no M2 secrets in its environment,
then freeze, owner checks, owner acceptance, independent review, one delivery. Its story is never adopted."""
import io
import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from m2k.cli import main
from m2k.contract import DEFAULT_TOOLS
from m2k.external import PROBE_PROMPT, CliAgent
from m2k.gates import TrustChainGate
from m2k.kernel import Kernel, KernelError, status
from m2k.models import ModelError, ModelReply, ScriptedModel, build_model
from m2k.trust import TrustError
from tests.helpers import GOOD_CODE, KernelCase, review
from tests.test_task_tree import TreeCase, sub

FAKE = str(Path(__file__).resolve().parent / "fixtures" / "fake_agent.py")
ENV = ["PATH", "SYSTEMROOT"]


class AgentCase:
    def agent(self, mode, timeout_s=60):
        return CliAgent([sys.executable, FAKE, mode, str(self.root / "seen")], name="fake-codex", env=ENV,
                        timeout_s=timeout_s, usage="codex-jsonl", isolation="vendor sandbox (declared)")

    def seen(self, mode, what):
        return (self.root / "seen" / f"{mode}.{what}").read_text(encoding="utf-8")

    def run_agent(self, mode, contract=None, reviews=("GO",), timeout_s=60):
        self.reviewer = ScriptedModel([review(d) for d in reviews], "glm")
        return Kernel(self.ledger, self.agent(mode, timeout_s), self.reviewer).run(contract or self.contract())


class ExternalAgentTest(AgentCase, KernelCase):
    def test_good_work_passes_through_the_whole_chain(self):
        r = self.run_agent("good")
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        self.assertEqual((r.delivered_to / "calc.py").read_text(encoding="utf-8"), GOOD_CODE)
        intent = self.ledger.events("t1", "effect.intent")[0].payload
        self.assertEqual((intent["tool"], intent["args"]["agent"]), ("external_agent", "fake-codex"))
        self.assertEqual(intent["args"]["isolation"], "vendor sandbox (declared)")
        called = [e.payload for e in self.ledger.events("t1", "model.called") if e.payload["role"] == "worker"]
        self.assertEqual([(c["model"], c["input_tokens"], c["output_tokens"]) for c in called], [("fake-codex", 1200, 300)])
        prompt = self.seen("good", "prompt.txt")
        self.assertIn("Write calc.py with add(a, b).", prompt)
        self.assertIn("Never change these files (any change fails the task): check.py", prompt)
        self.assertIn("{python} check.py", prompt)
        self.assertEqual(status(self.ledger, "t1")["worker"]["status"], "FINISHED")

    def test_a_rewritten_acceptance_check_fails_and_nothing_is_delivered(self):
        r = self.run_agent("cheat")
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertIsNone(r.delivered_to)
        self.assertEqual(self.ledger.events("t1", "delivery.intent"), [])
        self.assertIn("PROTECTED", r.test.reason)

    def test_a_confident_story_over_wrong_code_is_not_believed(self):
        r = self.run_agent("story")
        self.assertEqual(r.verdict.value, "FAIL")
        self.assertIsNone(r.delivered_to)
        self.assertEqual(self.ledger.last("t1", "worker.stopped").payload["summary"], "")
        for call in self.reviewer.calls:   # the agent's own output never reaches the reviewer
            self.assertNotIn("All tests pass", call[-1]["content"])

    def test_failure_timeout_and_budget_are_stops_not_passes(self):
        for mode, worker_status, effect in (("fail", "MODEL_ERROR", "FAILED"), ("sleep", "STALLED", "UNKNOWN"),
                                           ("budget", "BUDGET_EXHAUSTED", "COMPLETED")):
            with self.subTest(mode=mode):
                c = self.contract(task_id=mode, budget=replace(self.contract(task_id=mode).budget, max_tokens=50000))
                r = self.run_agent(mode, c, timeout_s=3)
                self.assertNotEqual(r.verdict.value, "PASS")
                self.assertIsNone(r.delivered_to)
                self.assertEqual(status(self.ledger, mode)["worker"]["status"], worker_status)
                self.assertEqual(self.ledger.events(mode, "effect.result")[0].payload["status"], effect)

    def test_the_agent_gets_no_secrets_only_allowed_variables(self):
        planted = {"DEEPSEEK_API_KEY": "sk-deepseek-secret-123", "GLM_API_KEY": "glm-secret-456", "M2K_HOME": "/x"}
        with patch.dict(os.environ, planted):
            self.assertEqual(self.run_agent("good").verdict.value, "PASS")
        names = set(json.loads(self.seen("good", "env.json")))
        self.assertFalse(names & set(planted))
        self.assertLessEqual(names - {"PYTHONUTF8"}, set(ENV) | {"LC_CTYPE", "COMSPEC", "PATHEXT"})

    def test_a_broken_chain_stops_the_agent_before_it_starts(self):
        def broken():
            raise TrustError("CREDENTIAL_REVOKED", "revoked")
        c = self.contract()
        out = self.agent("good").run(c, self.ledger, c.workspace, (TrustChainGate(broken),), {"check.py"})
        self.assertEqual(out.status.value, "TRUST_BROKEN")
        self.assertFalse((self.root / "seen" / "good.prompt.txt").exists())
        self.assertEqual(self.ledger.events("t1", "effect.intent"), [])

    def test_an_external_agent_must_be_granted_every_tool_and_cannot_plan(self):
        with self.assertRaises(KernelError):
            Kernel(self.ledger, self.agent("good"), ScriptedModel([], "glm")).run(replace(self.contract(), tools=("read_file",)))
        with self.assertRaises(ModelError):
            self.agent("good").complete([])
        self.assertEqual(self.ledger.events(), [])

    def cli_config(self, mode):
        config = self.root / f"{mode}.toml"
        command = json.dumps([sys.executable, FAKE, mode, str(self.root / "seen")])
        config.write_text(f'ledger = "cli-ledger.sqlite"\n[sandbox]\nbackend = "none"\n'
                          f'[models.worker]\nkind = "cli"\nname = "fake-codex"\ncommand = {command}\nenv = ["PATH"]\n'
                          f'[models.reviewer]\nmodel = "glm"\nbase_url = "https://example.invalid/v1"\n', encoding="utf-8")
        return str(config)

    def test_ping_starts_an_external_agent_once_in_an_empty_folder(self):
        for mode, code, line in (("good", 0, "OK   worker: fake-codex (external agent) answered"),
                                 ("fail", 1, "FAIL worker: fake-codex (external agent) FAILED (exit 3)")):
            with self.subTest(mode=mode), patch("m2k.models.OpenAICompatModel.complete", return_value=ModelReply('{"ok": true}', 1, 1, "glm")):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(main(["--config", self.cli_config(mode), "ping"]), code)
                self.assertIn(line, out.getvalue())
                self.assertEqual(self.seen(mode, "prompt.txt"), PROBE_PROMPT)
                self.assertFalse((self.root / "calc.py").exists())

    def test_run_refuses_before_recording_anything_when_the_probe_fails(self):
        task = self.root / "task.toml"
        self.contract()   # writes ws/t1/check.py
        task.write_text('[task]\nid = "t1"\ngoal = "g"\nworkspace = "ws/t1"\n'
                        '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n', encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", self.cli_config("fail"), "run", "--untrusted", str(task)]), 2)
        self.assertIn("拒绝启动：Worker 命令试启动失败（fake-codex）：FAILED (exit 3)", err.getvalue())
        self.assertNotIn("已记入账本", err.getvalue())
        from tests.test_startup_refusals import assert_refusal
        assert_refusal(self, self.root / "cli-ledger.sqlite", "t1", "WORKER_PROBE")

    def test_argument_prompt_reaches_probe_and_task_with_empty_stdin_and_unknown_usage(self):
        agent = build_model({"kind": "cli", "command": [sys.executable, FAKE, "good", str(self.root / "seen")],
                             "env": ENV, "prompt": "arg", "usage": ""})
        command = agent.command.copy()
        self.assertIsNone(agent.probe())
        self.assertEqual(self.seen("good", "prompt.txt"), PROBE_PROMPT)
        self.assertEqual(self.seen("good", "stdin.txt"), "")
        contract = replace(self.contract(), goal='Write calc.py with add(a, b).\nKeep "quotes" and 中文.')
        result = Kernel(self.ledger, agent, ScriptedModel([review("GO")], "glm")).run(contract)
        self.assertEqual(result.verdict.value, "PASS", result.reasons)
        self.assertEqual(self.seen("good", "prompt.txt"), agent.prompt(contract, {"check.py"}, ""))
        self.assertEqual(self.seen("good", "stdin.txt"), "")
        self.assertEqual(agent.command, command)
        call = next(e.payload for e in self.ledger.events("t1", "model.called") if e.payload["role"] == "worker")
        self.assertEqual((call["input_tokens"], call["output_tokens"]), (None, None))

    def test_probe_explains_launcher_scripts_without_starting_them(self):
        for suffix in (".PS1", ".cmd", ".bat"):
            with self.subTest(suffix=suffix), patch.object(CliAgent, "_exec") as execute:
                reason = CliAgent(["agent" + suffix]).probe()
                self.assertIn("launcher script", reason)
                self.assertIn(".exe", reason)
                self.assertIn("node <entry>.js", reason)
                execute.assert_not_called()

    def test_probe_puts_json_error_result_first_even_when_exit_is_zero(self):
        reason = self.agent("login").probe()
        self.assertTrue(reason.startswith("Not logged in · Please run /login"), reason)
        output = 'log line\n' + json.dumps({"is_error": True, "result": "Login required"})
        with patch.object(CliAgent, "_exec", return_value=("COMPLETED", 0, output)):
            self.assertTrue(self.agent("good").probe().startswith("Login required"))

    def test_claude_code_usage_counts_cache_tokens_as_input(self):
        agent = CliAgent([sys.executable, FAKE, "claude", str(self.root / "seen")], name="fake-claude", env=ENV,
                         usage="claude-json")
        self.reviewer = ScriptedModel([review("GO")], "glm")
        r = Kernel(self.ledger, agent, self.reviewer).run(self.contract())
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        called = [e.payload for e in self.ledger.events("t1", "model.called") if e.payload["role"] == "worker"]
        self.assertEqual([(c["input_tokens"], c["output_tokens"]) for c in called], [(1200, 300)])
        self.assertEqual(agent._usage('{"type": "result", "usage": {"input_tokens": 5}}'), (None, None))
        codex = self.agent("good")   # the same rule for Codex: no output_tokens means unknown, never zero
        self.assertEqual(codex._usage('{"type": "turn.completed", "usage": {"input_tokens": 5}}'), (None, None))

    def test_config(self):
        a = build_model({"kind": "cli", "command": ["codex", "exec", "-"], "env": ["PATH"], "timeout_s": 900})
        self.assertEqual((a.name, a.command, a.timeout_s, a.external), ("cli:codex", ["codex", "exec", "-"], 900, True))
        for bad in ([], [""], "codex"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CliAgent(bad if bad != "codex" else [1])
        with self.assertRaises(ValueError):
            CliAgent(["dsh"], prompt="file")


class ExternalSubtasksTest(AgentCase, TreeCase):
    def test_each_subtask_runs_the_agent_in_its_own_copy_and_is_merged_only_on_pass(self):
        c = self.parent(subs=(replace(sub("a", ["a.py"], "check_a.py"), tools=DEFAULT_TOOLS),
                              replace(sub("b", ["b.py"], "check_b.py"), tools=DEFAULT_TOOLS)))
        r = self.run_agent("tree", c, reviews=("GO",) * 3)
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        self.assertEqual([op for kind, op in self.merges("p1") if kind == "effect.intent"], ["merge-a", "merge-b"])
        self.assertEqual((r.delivered_to / "a.py").read_text(encoding="utf-8"), "A = 1\n")


if __name__ == "__main__":
    unittest.main()
