"""T80：预授权放行、并发预算与只读重放。"""
import hashlib
import io
import json
import sys
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict, replace
from unittest.mock import patch

from m2k import trust
from m2k.adjudicate import adjudicate, approval_needs, preauth_limits
from m2k.cli import main
from m2k.contract import Budget, ContractError, parse_contract
from m2k.external import CliAgent
from m2k.kernel import Kernel, KernelError, status
from m2k.ledger import LedgerError
from m2k.models import ScriptedModel
from m2k.obs import receipt_body, request
from m2k.sandbox import NoSandbox
from m2k.types import Verdict, WorkerStatus
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, owner_approval, review, step
from tests.test_external_agent import ENV, FAKE
from tests.test_obs import WitnessFiles


def ask(kind="scope", **req):
    return step("ask_owner", kind=kind, text="需要授权继续完成任务", request=req)


WRITE = step("write_file", path="calc.py", content=GOOD_CODE)
FINISH = step("finish")
CAPS = dict(tools=["list_files", "read_file", "write_file", "edit_file", "run_command"],
            network=True, max_rounds=100, max_model_calls=100, max_tokens=1_000_000, max_commands=100)


class PreauthCase(KernelCase):
    def preauthorized(self, task_id="t1", **changes):
        base = replace(self.contract(task_id=task_id, budget=Budget(10, 12, 10000, 4)),
                       writes=("initial/",), preauth={"writes": ["calc.py"], "tools": ["write_file"],
                                                     "network": True, "budget_percent": 20})
        return replace(base, **changes)

    def run_steps(self, contract, steps, reviews=("GO",), **options):
        self.worker = ScriptedModel(steps, "worker")
        self.reviewer = ScriptedModel([review(d) for d in reviews], "reviewer")
        self.kernel = Kernel(self.ledger, self.worker, self.reviewer, **options)
        return self.kernel.run(contract)

    def grants(self, tid="t1"):
        return self.ledger.events(tid, "authority.granted")


class PreauthContractTest(PreauthCase):
    def raw(self):
        return {"task": {"id": "t1", "goal": "goal", "workspace": "ws"},
                "acceptance": {"command": "{python} check.py", "files": ["check.py"]}}

    def test_bad_keys_and_types(self):
        cases = [None, [], "bad", {"extra": True}, {"writes": "x"}, {"writes": [1]},
                 {"writes": [""]}, {"writes": ["../x"]}, {"writes": ["/x"]},
                 {"writes": ["C:/x"]}, {"tools": "read_file"}, {"tools": [1]},
                 {"tools": ["missing"]}, {"network": 1}, {"network": "true"},
                 *[{"budget_percent": v} for v in (0, 101, True, "20", None, 1.5)]]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ContractError):
                parse_contract(dict(self.raw(), preauth=value), self.root)

    def test_subtasks_and_plan_are_refused(self):
        for change in ({"subtasks": []}, {"task": dict(self.raw()["task"], plan="model"),
                                           "acceptance": dict(self.raw()["acceptance"], parts={"check.py": "{python} check.py"})}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                parse_contract(dict(self.raw(), preauth={}, **change), self.root)

    def test_legacy_digest_and_contract_json_are_identical(self):
        contract = self.contract()
        original = json.loads(json.dumps(asdict(contract), default=str))
        self.assertNotIn("preauth", original)
        self.assertEqual(contract.digest(), hashlib.sha256(json.dumps(original, sort_keys=True,
                             ensure_ascii=False, default=str).encode()).hexdigest())
        self.run_steps(contract, [WRITE, FINISH])
        self.assertEqual(self.ledger.last("t1", "task.submitted").payload["contract"], original)
        self.assertNotIn("start_files", self.ledger.last("t1", "task.submitted").payload)
        self.assertEqual(parse_contract(self.raw(), self.root).preauth, None)

    def test_paths_normalize_and_replace_keeps_preauth(self):
        raw = dict(self.raw(), preauth={"writes": ["pkg\\sub/"]})
        raw["task"]["writes"] = ["initial/"]
        contract = parse_contract(raw, self.root)
        self.assertEqual(contract.preauth, {"writes": ["pkg/sub/"]})
        self.assertEqual(contract.writes, ("initial/",))
        self.assertEqual(replace(contract, network=True).preauth, contract.preauth)
        self.assertEqual(contract.data()["preauth"], contract.preauth)
        for writes in ("path", [1], [""], ["pkg\\..\\outside"]):
            with self.subTest(writes=writes), self.assertRaises(ContractError):
                parse_contract(dict(raw, task=dict(raw["task"], writes=writes)), self.root)


class PreauthNativeTest(WitnessFiles, PreauthCase):
    def test_scope_grants_with_receipt_credential_and_current_gates(self):
        contract = self.preauthorized(tools=("read_file",))
        contract = replace(contract, preauth=dict(contract.preauth, tools=["write_file", "run_command"]))
        setup, _ = trust.dev_setup(self.root / "keys", CAPS)
        observer, _, public, log = self.client()
        networks = []
        sandbox = NoSandbox()
        with patch.object(sandbox, "argv", side_effect=lambda command, cwd, network, info_fd=None:
                          networks.append(network)):
            report = self.run_steps(contract, [ask(writes=["calc.py"], tools=["write_file", "run_command"], network=True),
                                   WRITE, step("run_command", command="{python} check.py"), FINISH],
                                   trust=setup, obs=observer, sandbox=sandbox)
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertTrue(networks and all(networks))
        self.assertEqual(len(self.worker.calls), 4)
        self.assertIn("GRANTED by pre-authorization", self.worker.calls[1][-1]["content"])
        self.assertIn("ask_owner", self.worker.calls[2][-4]["content"])
        self.assertEqual(self.ledger.events("t1", "owner.request"), [])
        event, = self.grants()
        receipt = next(e.payload["receipt"] for e in self.ledger.events("t1", "obs.receipt")
                       if e.payload["receipt"]["body"]["ledger_seq"] == event.seq)
        receipt_body(receipt, public, request(event, self.ledger.ledger_id))
        self.assertEqual(len(self.ledger.events("t1", "credential.issued")), 2)
        self.assertEqual(self.kernel._verify_chain(self.kernel.effective["t1"])["contract_sha256"], event.payload["contract_sha256"])
        self.assertIn("network=True", self.reviewer.calls[0][0]["content"])
        self.assertIn("run_command", self.reviewer.calls[0][0]["content"])
        self.assertEqual(report.effective.network, True)
        self.assertIn("calc.py", report.effective.writes)
        self.assertEqual(adjudicate(self.ledger, "t1", obs_public_key=public, obs_log=log).verdict, "PASS")
        self.assertEqual(status(self.ledger, "t1")["granted"][0]["seq"], event.seq)

    def test_one_part_outside_or_question_stays_awaiting(self):
        cases = [ask(writes=["calc.py", "other.py"]), ask(tools=["write_file", "run_command"]),
                 ask(kind="question"), ask(kind="scope", writes=["calc.py"], network=True)]
        for i, value in enumerate(cases):
            with self.subTest(i=i):
                contract = self.preauthorized(task_id=f"out-{i}")
                if i == 3: contract = replace(contract, preauth=dict(contract.preauth, network=False))
                report = self.run_steps(contract, [value], reviews=())
                self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
                self.assertEqual(report.verdict, Verdict.UNKNOWN)
                self.assertFalse(self.grants(contract.task_id))

    def test_noop_has_no_grant_and_no_owner_request(self):
        report = self.run_steps(self.preauthorized(writes=()), [ask(writes=["any.py"]), WRITE, FINISH])
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertFalse(self.grants())
        self.assertFalse(self.ledger.events("t1", "owner.request"))

    def test_granted_writes_do_not_bypass_acceptance_protection(self):
        contract = self.preauthorized(writes=(), tools=("read_file",))
        report = self.run_steps(contract, [ask(tools=["write_file"]),
                               step("write_file", path="check.py", content="pass"), WRITE, FINISH])
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertEqual(self.ledger.last("t1", "gate.denied").payload["gate"], "protected_path")

    def test_repairs_use_current_authority(self):
        contract = replace(self.preauthorized(), max_repairs=1)
        report = self.run_steps(contract, [ask(writes=["calc.py"]),
                               step("write_file", path="calc.py", content=BAD_CODE), FINISH, WRITE, FINISH],
                               reviews=("NO_GO", "GO"))
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertEqual(len(self.grants()), 1)
        self.assertEqual(len(self.ledger.events("t1", "repair.started")), 1)


class PreauthBudgetTest(PreauthCase):
    def test_framework_raises_each_exhausted_model_field(self):
        for i, (budget, steps) in enumerate(((Budget(2, 10, 1000, 4), [WRITE, step("read_file", path="calc.py"), FINISH]),
                                            (Budget(10, 2, 1000, 4), [WRITE, step("read_file", path="calc.py"), FINISH]),
                                            (Budget(10, 10, 40, 4), [WRITE, step("read_file", path="calc.py"), FINISH]))):
            with self.subTest(i=i):
                contract = self.preauthorized(task_id=f"budget-{i}", writes=(), budget=budget)
                contract = replace(contract, preauth={"budget_percent": 100})
                report = self.run_steps(contract, steps)
                self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
                self.assertEqual(self.grants(contract.task_id)[0].payload["by"], "framework")

    def test_exact_limit_and_one_over_and_unlimited(self):
        for tid, limit, expected in (("exact", 12, Verdict.PASS), ("over", 13, Verdict.UNKNOWN)):
            with self.subTest(tid=tid):
                report = self.run_steps(self.preauthorized(task_id=tid, writes=()),
                                       [ask(kind="budget", budget={"max_rounds": limit}), WRITE, FINISH])
                self.assertEqual(report.reported_verdict, expected, report.reasons)
        contract = self.preauthorized(task_id="unlimited", writes=(), budget=Budget())
        report = self.run_steps(contract, [ask(kind="budget", budget={"max_tokens": 1_000_000}), WRITE, FINISH])
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertFalse(self.grants("unlimited"))

    def test_caps_refusal_preserves_old_credential(self):
        setup, _ = trust.dev_setup(self.root / "keys", dict(CAPS, max_rounds=10))
        contract = self.preauthorized(writes=())
        report = self.run_steps(contract, [ask(kind="budget", budget={"max_rounds": 12})], reviews=(), trust=setup)
        self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
        self.assertFalse(self.grants())
        self.assertEqual(self.kernel.effective["t1"], contract)
        self.assertEqual(len(self.ledger.events("t1", "credential.issued")), 1)
        self.assertEqual(self.kernel._verify_chain(contract)["contract_sha256"], contract.digest())

    def test_chain_allowance_is_not_reset_on_amendment(self):
        first = self.preauthorized(writes=())
        self.run_steps(first, [ask(kind="budget", budget={"max_rounds": 11}), WRITE, FINISH])
        second = replace(first, task_id="t2", workspace=first.workspace.parent / "t2")
        worker = ScriptedModel([ask(kind="budget", budget={"max_rounds": 8}),
                                ask(kind="budget", budget={"max_rounds": 9})], "w2")
        kernel = Kernel(self.ledger, worker, ScriptedModel([], "r2"))
        report = kernel.run(second, amends="t1")
        self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
        self.assertEqual(len(self.grants("t2")), 1)
        self.assertEqual(self.grants("t2")[0].payload["fields"]["budget"][1]["max_rounds"], 8)
        self.assertEqual(self.ledger.last("t2", "task.submitted").payload["contract"]["budget"]["max_rounds"], 7)

    def test_guard_rechecks_and_refusal_changes_neither_current_object(self):
        contract = self.preauthorized(writes=())
        setup, _ = trust.dev_setup(self.root / "keys", CAPS)
        worker = ScriptedModel([ask(kind="budget", budget={"max_rounds": 12})], "w")
        kernel = Kernel(self.ledger, worker, ScriptedModel([], "r"), trust=setup)
        record, seen = kernel._record, []
        def intercept(tid, kind, payload, guard=None):
            if kind == "authority.granted":
                old_contract, old_credential = kernel.effective[tid], kernel.credential
                seen.append((old_contract, old_credential))
                original = guard
                def racing_guard():
                    # 注入另一个版本占用额度：在同一个事务中增加一条竞争事实。
                    old = asdict(contract.budget)
                    self.ledger._append(tid, "authority.granted", {"fields": {"budget": [old, dict(old, max_rounds=11)]}})
                    return original()
                try:
                    return record(tid, kind, payload, guard=racing_guard)
                finally:
                    self.assertIs(kernel.effective[tid], old_contract)
                    self.assertIs(kernel.credential, old_credential)
            return record(tid, kind, payload, guard=guard)
        with patch.object(kernel, "_record", side_effect=intercept):
            report = kernel.run(contract)
        self.assertTrue(seen)
        self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
        self.assertFalse(self.grants())
        self.assertEqual(kernel._verify_chain(contract)["contract_sha256"], contract.digest())

    def test_failed_write_keeps_current_contract_and_credential(self):
        contract = self.preauthorized(writes=())
        append = self.ledger.append
        def refuse(tid, kind, *args, **kw):
            if kind == "authority.granted": raise LedgerError("grant write refused")
            return append(tid, kind, *args, **kw)
        with patch.object(self.ledger, "append", side_effect=refuse):
            report = self.run_steps(contract, [ask(kind="budget", budget={"max_rounds": 12})], reviews=())
        self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
        self.assertIs(self.kernel.effective["t1"], contract)
        self.assertFalse(self.grants())

    def test_all_budget_fields_share_the_same_chain_rule(self):
        for field, old in asdict(Budget(10, 12, 10000, 4)).items():
            for offset, passes in ((old * 20 // 100, True), (old * 20 // 100 + 1, False)):
                with self.subTest(field=field, offset=offset):
                    tid = f"{field}-{offset}"
                    contract = self.preauthorized(task_id=tid, writes=())
                    report = self.run_steps(contract, [ask(kind="budget", budget={field: old + offset}), WRITE, FINISH])
                    self.assertEqual(report.reported_verdict, Verdict.PASS if passes else Verdict.UNKNOWN, report.reasons)

    def test_unlimited_token_field_does_not_break_later_budget_grants(self):
        contract = self.preauthorized(writes=(), budget=Budget())
        report = self.run_steps(contract, [ask(kind="budget", budget={"max_rounds": 21}),
                               ask(kind="budget", budget={"max_model_calls": 31}), WRITE, FINISH])
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertEqual(len(self.grants()), 2)


class PreauthExternalTest(PreauthCase):
    def external(self, requests):
        # 继续复用 T37 假 Agent；启动计数和请求由测试包装器提供。
        code = ('import json, runpy, sys\nfrom pathlib import Path\n'
                'counter = Path(sys.argv[1])\n'
                'n = int(counter.read_text()) if counter.exists() else 0\n'
                'counter.write_text(str(n + 1))\n'
                'p = Path(".m2k/ask.json")\nassert not p.exists()\n'
                f'requests = {requests!r}\n'
                'if n < len(requests):\n'
                ' p.parent.mkdir(exist_ok=True)\n p.write_text(json.dumps(requests[n]), encoding="utf-8")\n'
                'else:\n Path("continuation.txt").write_text("continued", encoding="utf-8")\n'
                'sys.argv = sys.argv[2:]\nrunpy.run_path(sys.argv[0], run_name="__main__")\n')
        return CliAgent([sys.executable, "-c", code, str(self.root / "starts"), FAKE,
                         "good", str(self.root / "seen")], env=ENV, usage="codex-jsonl", watch_s=0.01)

    def run_external(self, requests, contract=None):
        agent = self.external(requests)
        self.reviewer = ScriptedModel([review("GO")], "r")
        self.kernel = Kernel(self.ledger, agent, self.reviewer)
        return self.kernel.run(contract or self.preauthorized())

    def req(self, kind="scope", **req):
        return {"kind": kind, "text": "需要授权", "request": req}

    def test_restart_has_distinct_operations_and_observer_keeps_baseline(self):
        contract = self.preauthorized(preauth={"writes": ["calc.py", "continuation.txt"]})
        report = self.run_external([self.req(writes=["calc.py", "continuation.txt"])], contract)
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertEqual([e.op_id for e in self.ledger.events("t1", "effect.intent")], ["x1", "x2"])
        self.assertEqual([e.op_id for e in self.ledger.events("t1", "effect.result")], ["x1", "x2"])
        self.assertEqual(len(self.ledger.events("t1", "model.called")), 3)
        self.assertEqual(report.usage["tokens"], 3020)
        self.assertFalse((contract.workspace / ".m2k/ask.json").exists())
        self.assertTrue(any("continuation.txt" in e.payload["added"] for e in
                            self.ledger.events("t1", "workspace.changed")))
        prompt = (self.root / "seen/good.prompt.txt").read_text(encoding="utf-8")
        self.assertIn("The owner's pre-authorization granted:", prompt)
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "PASS")

    def test_tools_and_network_are_never_granted(self):
        for i, req in enumerate((self.req(tools=["write_file"]), self.req(network=True),
                                 self.req(writes=["calc.py"], network=False))):
            with self.subTest(req=req):
                report = self.run_external([req], self.preauthorized(task_id=f"external-{i}"))
                self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
                self.assertFalse(self.grants(report.task_id))
                (self.root / "starts").unlink()

    def test_fourth_request_awaits_even_if_it_fits(self):
        contract = self.preauthorized(preauth={"writes": ["calc.py", "one", "two", "three"]})
        requests = [self.req(writes=[name]) for name in ("calc.py", "one", "two", "three")]
        report = self.run_external(requests, contract)
        self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
        self.assertEqual(len(self.grants()), 3)
        self.assertEqual([e.op_id for e in self.ledger.events("t1", "effect.intent")], ["x1", "x2", "x3", "x4"])
        self.assertEqual(self.ledger.last("t1", "owner.request").payload["request"], {"writes": ["three"]})

    def test_token_limit_counts_starts_and_exhaustion_is_not_granted(self):
        contract = self.preauthorized(writes=(), budget=Budget(10, 12, 2900, 4), preauth={"budget_percent": 100})
        report = self.run_external([self.req(kind="budget", budget={"max_tokens": 3000})], contract)
        self.assertEqual(report.worker.status, WorkerStatus.FINISHED)
        self.assertEqual(report.reported_verdict, Verdict.UNKNOWN)  # 审查前预算已用满。
        self.assertEqual(len(self.grants()), 1)
        self.assertEqual(report.usage["tokens"], 3000)
        (self.root / "starts").unlink()
        contract = self.preauthorized(task_id="exhaust", writes=(), budget=Budget(10, 12, 2999, 4),
                                     preauth={"budget_percent": 100})
        report = self.run_external([self.req(writes=[])], contract)
        self.assertEqual(report.worker.status, WorkerStatus.BUDGET_EXHAUSTED)
        self.assertEqual(report.awaiting["by"], "framework")
        self.assertFalse(self.grants("exhaust"))  # 空范围请求没有实际放行；已花掉的 token 不自动追加。


class PreauthAmendTest(PreauthCase):
    def test_each_preauth_widening_needs_owner_approval(self):
        first = self.preauthorized(writes=())
        self.run_steps(first, [WRITE, FINISH])
        for change in ({"writes": ["calc.py", "other/"]}, {"tools": ["write_file", "run_command"]},
                       {"budget_percent": 21}):
            with self.subTest(change=change):
                second = replace(first, task_id="t2", workspace=first.workspace.parent / "t2",
                                 preauth=dict(first.preauth, **change))
                owner = json.loads(json.dumps(second.data(), default=str))
                needs = approval_needs(self.ledger, "t1", owner)
                self.assertEqual(needs["scope"]["preauth"], [first.preauth, second.preauth])
                kernel = Kernel(self.ledger, ScriptedModel([], "w"), ScriptedModel([], "r"))
                before = self.ledger.events()
                with self.assertRaisesRegex(KernelError, "m2k answer"):
                    kernel.run(second, amends="t1")
                self.assertEqual(self.ledger.events(), before)
                self.assertFalse(second.workspace.exists())

    def test_added_preauth_network_and_percent_and_narrowing(self):
        first = self.contract()
        self.run_steps(first, [WRITE, FINISH])
        for preauth in ({"network": True}, {"budget_percent": 20}, {"tools": ["read_file"]}, {"writes": ["pkg/"]}):
            with self.subTest(preauth=preauth):
                owner = json.loads(json.dumps(replace(first, preauth=preauth).data(), default=str))
                self.assertEqual(approval_needs(self.ledger, "t1", owner)["scope"]["preauth"], [None, preauth])
        old = self.ledger.last("t1", "task.submitted").payload
        old["contract"]["preauth"] = {"writes": ["pkg/"], "tools": ["write_file"], "network": True, "budget_percent": 30}
        self.ledger.append("t1", "task.submitted", old)
        owner = dict(old["contract"], preauth={"writes": ["pkg/sub/"], "budget_percent": 20})
        self.assertFalse(approval_needs(self.ledger, "t1", owner)["scope"])

    def test_scope_grants_do_not_carry_into_later_versions(self):
        first = self.preauthorized()
        self.run_steps(first, [ask(writes=["calc.py"]), WRITE, FINISH])
        second = replace(first, task_id="t2", workspace=first.workspace.parent / "t2")
        kernel = Kernel(self.ledger, ScriptedModel([WRITE, FINISH], "w"), ScriptedModel([review("GO")], "r"))
        report = kernel.run(second, amends="t1")
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertEqual(self.ledger.last("t2", "gate.denied").payload["gate"], "write_scope")
        self.assertFalse(self.grants("t2"))

    def test_approved_preauth_widening_runs_and_adjudicates(self):
        first = self.preauthorized(writes=())
        self.run_steps(first, [WRITE, FINISH])
        second = replace(first, task_id="t2", workspace=first.workspace.parent / "t2",
                         preauth=dict(first.preauth, writes=["calc.py", "new.txt"]))
        approval = {"by": "m2k answer", "text": None,
                    "needs": approval_needs(self.ledger, "t1", json.loads(json.dumps(second.data(), default=str)))}
        kernel = Kernel(self.ledger, ScriptedModel([FINISH], "w"), ScriptedModel([review("GO")], "r"))
        report = kernel.run(second, amends="t1", approval=approval)
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertEqual(adjudicate(self.ledger, "t2").verdict, "PASS")


class PreauthAdjudicateTest(WitnessFiles, PreauthCase):
    def honest(self, observed=False):
        contract = self.preauthorized()
        setup, _ = trust.dev_setup(self.root / "keys", CAPS)
        observer, _, self.public, _ = self.client() if observed else (None, None, None, None)
        report = self.run_steps(contract, [ask(writes=["calc.py"]), WRITE, FINISH], trust=setup, obs=observer)
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        return contract

    def clone(self, transform):
        from m2k.ledger import Ledger
        clone = Ledger(":memory:")
        self.addCleanup(clone.close)
        for e in self.ledger.events():
            payload = transform(e, deepcopy(e.payload))
            if payload is not None: clone.append(e.task_id, e.kind, payload, e.op_id)
        return clone

    def test_forged_grants_are_never_pass(self):
        self.honest()
        event, = self.grants()
        def missing_basis(p): p.pop("basis"); return p
        def outside(p):
            p["request"]["request"]["writes"].append("outside.py")
            return p
        cases = [("missing basis", missing_basis),
                 ("wrong basis", lambda p: dict(p, basis={"by": "worker", "clause": {}})),
                 ("outside", outside), ("wrong digest", lambda p: dict(p, contract_sha256="0" * 64)),
                 ("wrong old", lambda p: dict(p, fields={"writes": [[], ["initial/", "calc.py"]]})),
                 ("wrong clause", lambda p: dict(p, basis={"by": "preauth", "clause": {"writes": ["wrong"]}}))]
        for label, change in cases:
            with self.subTest(label=label):
                clone = self.clone(lambda e, p: change(p) if e.seq == event.seq else p)
                result = adjudicate(clone, "t1")
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn("authority.granted", " ".join(result.reasons))

    def test_missing_credential_and_last_old_credential_and_narrow_credential(self):
        self.honest()
        old, latest = self.ledger.events("t1", "credential.issued")
        clone = self.clone(lambda e, p: None if e.seq == latest.seq else p)
        result = adjudicate(clone, "t1")
        self.assertNotEqual(result.verdict, "PASS")
        self.assertIn("credential.issued after grant", " ".join(result.reasons))
        self.ledger.append("t1", "credential.issued", old.payload)
        result = adjudicate(self.ledger, "t1")
        self.assertNotEqual(result.verdict, "PASS")
        self.assertIn("credential does not cover", " ".join(result.reasons))
        smaller = deepcopy(latest.payload)
        smaller["credential"]["body"]["tools"] = []
        self.ledger.append("t1", "credential.issued", smaller)
        self.assertIn("final effective authority", " ".join(adjudicate(self.ledger, "t1").reasons))

    def test_budget_over_allowance_and_final_budget_are_rejected(self):
        contract = self.preauthorized(writes=())
        self.run_steps(contract, [ask(kind="budget", budget={"max_rounds": 12}), WRITE, FINISH])
        grant, = self.grants()
        def over(e, p):
            if e.seq == grant.seq:
                p["request"]["request"]["budget"]["max_rounds"] = 13
                p["fields"]["budget"][1]["max_rounds"] = 13
            return p
        result = adjudicate(self.clone(over), "t1")
        self.assertNotEqual(result.verdict, "PASS")
        self.assertIn("allowance", " ".join(result.reasons))
        for _ in range(11):
            self.ledger.append("t1", "model.called", {"role": "worker", "input_tokens": 1, "output_tokens": 1})
        self.assertIn("final effective budget exceeded", adjudicate(self.ledger, "t1").reasons)

    def test_malformed_budget_history_is_rejected_without_crashing(self):
        contract = self.preauthorized(writes=())
        report = self.run_steps(contract, [ask(kind="budget", budget={"max_rounds": 11}),
                               ask(kind="budget", budget={"max_rounds": 12}), WRITE, FINISH])
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        first, second = self.grants()
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "PASS")
        for budget in ([], [{}]):
            with self.subTest(budget=budget):
                def malformed(e, p):
                    if e.seq == first.seq: p["fields"]["budget"] = budget
                    return p
                clone = self.clone(malformed)
                self.assertEqual(clone.verify_chain(), (True, None))
                result = adjudicate(clone, "t1")
                self.assertNotEqual(result.verdict, "PASS")
                reasons = " ".join(result.reasons)
                self.assertIn(f"authority.granted at seq {first.seq}", reasons)
                self.assertIn(f"authority.granted at seq {second.seq}", reasons)

    def test_invalid_budget_history_refuses_kernel_grants(self):
        contract = self.preauthorized(writes=())
        report = self.run_steps(contract, [ask(kind="budget", budget={"max_rounds": 11}), WRITE, FINISH])
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        grant, = self.grants()
        effective, credential = self.kernel.effective["t1"], self.kernel.credential
        request = {"kind": "budget", "text": "申请额度", "request": {"budget": {"max_rounds": 12}}}
        for budget in ([], [{}], [None, {}], [{}, []], None,
                       *[[{}, {"max_rounds": v}] for v in (True, 12.5, "12")]):
            with self.subTest(budget=budget):
                def malformed(e, p):
                    if e.seq == grant.seq: p["fields"]["budget"] = budget
                    return p
                clone = self.clone(malformed)
                before = clone.events()
                with self.assertRaisesRegex(ValueError, f"authority.granted at seq {grant.seq}"):
                    preauth_limits(clone, "t1", asdict(effective.budget))
                with patch.object(self.kernel, "ledger", clone):
                    self.assertIsNone(self.kernel._grant("t1", request, "worker"))
                self.assertEqual(clone.events(), before)
                self.assertIs(self.kernel.effective["t1"], effective)
                self.assertIs(self.kernel.credential, credential)

    def test_framework_budget_exhaustion_with_malformed_history_stops_normally(self):
        first = self.preauthorized(writes=())
        report = self.run_steps(first, [ask(kind="budget", budget={"max_rounds": 11}), WRITE, FINISH])
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        grant, = self.grants()
        def malformed(e, p):
            if e.seq == grant.seq: p["fields"]["budget"] = []
            return p
        clone = self.clone(malformed)
        self.assertEqual(clone.verify_chain(), (True, None))
        second = replace(first, task_id="t2", workspace=first.workspace.parent / "t2")
        worker = ScriptedModel([step("read_file", path="calc.py")] * 7, "w2")
        report = Kernel(clone, worker, ScriptedModel([], "r2")).run(second, amends="t1")
        self.assertEqual(len(worker.calls), 7)
        self.assertEqual(report.worker.status, WorkerStatus.BUDGET_EXHAUSTED)
        stopped = clone.last("t2", "worker.stopped")
        self.assertIsNotNone(stopped)
        self.assertEqual(stopped.payload["status"], "BUDGET_EXHAUSTED")
        for kind in ("candidate.frozen", "verdict"):
            self.assertIsNotNone(clone.last("t2", kind))
        self.assertEqual(report.reported_verdict, Verdict.UNKNOWN)
        self.assertEqual(report.awaiting["by"], "framework")
        self.assertEqual(report.awaiting["kind"], "budget")
        self.assertFalse(clone.events("t2", "authority.granted"))

    def test_malformed_credential_and_start_files_are_not_pass(self):
        self.honest()
        credential = self.ledger.last("t1", "credential.issued")
        submitted = self.ledger.last("t1", "task.submitted")
        cases = [("body", value) for value in (None, [], [{}])]
        cases += [("credential", value) for value in (None, [], [{}])]
        cases += [("start_files", value) for value in (None, [], [{}])]
        cases.append(("baseline", None))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                def malformed(e, p):
                    if e.seq == credential.seq and field == "body": p["credential"]["body"] = value
                    if e.seq == credential.seq and field == "credential": p[field] = value
                    if e.seq == submitted.seq and field == "start_files": p[field] = value
                    if e.seq == submitted.seq and field == "baseline": p.pop(field)
                    return p
                clone = self.clone(malformed)
                self.assertEqual(clone.verify_chain(), (True, None))
                result = adjudicate(clone, "t1")
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn("authority.granted", " ".join(result.reasons))

    def test_each_grant_and_credential_need_obs_receipts(self):
        self.honest(observed=True)
        event, = self.grants()
        clone = self.clone(lambda e, p: None if e.kind == "obs.receipt" and
                           p["receipt"]["body"]["ledger_seq"] == event.seq else p)
        result = adjudicate(clone, "t1", obs_public_key=self.public)
        self.assertNotEqual(result.verdict, "PASS")
        self.assertIn("no valid OBS receipt for authority.granted", " ".join(result.reasons))

    def test_outside_writes_use_final_scope(self):
        self.honest()
        frozen = self.ledger.last("t1", "candidate.frozen").payload
        files = dict(frozen["files"], **{"other.py": "a" * 64})
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        self.ledger.append("t1", "candidate.frozen", {"files": files, "digest": digest})
        for kind in ("test.result", "audit.result", "verdict"):
            self.ledger.append("t1", kind, dict(self.ledger.last("t1", kind).payload, candidate=digest))
        self.assertIn("files changed outside the final effective write scope", adjudicate(self.ledger, "t1").reasons)


class PreauthCliTest(PreauthCase):
    def test_cli_prints_grants_and_status_lists_them(self):
        contract = self.contract(deliver=False)
        task = self.root / "task.toml"
        task.write_text(f'[task]\nid = "t1"\ngoal = "g"\nworkspace = {json.dumps(str(contract.workspace))}\n'
                        'writes = ["initial/"]\ntools = ["read_file"]\n'
                        '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n'
                        '[preauth]\nwrites = ["calc.py"]\ntools = ["write_file"]\n', encoding="utf-8")
        config = self.root / "config.toml"
        config.write_text('[sandbox]\nbackend = "none"\n[models.worker]\nmodel = "w"\n'
                          '[models.reviewer]\nmodel = "r"\n', encoding="utf-8")
        output = io.StringIO()
        models = [ScriptedModel([ask(writes=["calc.py"], tools=["write_file"]), WRITE, FINISH], "w"),
                  ScriptedModel([review("GO")], "r")]
        with patch("m2k.cli.build_model", side_effect=models), redirect_stdout(output):
            self.assertEqual(main(["--config", str(config), "--ledger", str(self.ledger.path),
                                   "run", "--untrusted", str(task)]), 0)
        self.assertIn("✓ 预授权放行（", output.getvalue())
        self.assertIn("writes ['initial/'] → ['initial/', 'calc.py']", output.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--ledger", str(self.ledger.path), "status", "t1"]), 0)
        self.assertEqual(status(self.ledger, "t1")["granted"][0]["by"], "worker")
