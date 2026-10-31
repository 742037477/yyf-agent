"""T79: requests are data; only a confirmed owner amendment can grant more."""
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from threading import Barrier
from unittest.mock import patch

from m2k import tools, trust
from m2k.adjudicate import adjudicate, approval_needs
from m2k.cli import main
from m2k.contract import Budget, load_contract
from m2k.external import CliAgent
from m2k.kernel import Kernel, KernelError, status
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.obs import receipt_body, request
from m2k.types import Verdict, WorkerStatus
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, owner_approval, review, step
from tests.test_amend import AmendCase
from tests.test_external_agent import ENV, FAKE, AgentCase
from tests.test_obs import WitnessFiles
from tests.test_recall import RecallCase, good_a, good_b


QUESTION = {"kind": "question", "text": "请确认目标和输出格式。"}
ASK = step("ask_owner", **QUESTION)
WRITE = step("write_file", path="calc.py", content=GOOD_CODE)


class OwnerWorkerTest(AgentCase, KernelCase):
    def assert_awaiting(self, report, by="worker", kind="question", reviewed=False):
        self.assertEqual((report.verdict, report.reported_verdict), (Verdict.UNKNOWN, Verdict.UNKNOWN))
        self.assertTrue(report.reasons[0].startswith(f"awaiting owner: {kind}: "), report.reasons)
        self.assertEqual(report.awaiting["by"], by)
        self.assertEqual(report.awaiting["kind"], kind)
        self.assertEqual(report.awaiting["seq"], self.ledger.last(report.task_id, "owner.request").seq)
        self.assertIsNotNone(report.candidate)
        self.assertIsNotNone(report.test)
        if reviewed:
            self.assertIsNotNone(report.audit)
            self.assertEqual(report.audit.verdict, Verdict.UNKNOWN)
        else:
            self.assertIsNone(report.audit)
        self.assertIsNone(report.delivered_to)
        if reviewed:
            self.assertEqual(len(self.reviewer.calls), 2)
        else:
            self.assertFalse(self.reviewer.calls)
        self.assertFalse(self.ledger.events(report.task_id, "delivery.intent"))
        if reviewed:
            self.assertEqual(self.ledger.last(report.task_id, "audit.result").payload["decision"], "NONE")
        else:
            self.assertFalse(self.ledger.events(report.task_id, "audit.result"))

    def test_native_request_never_reaches_gates_or_execute(self):
        self.reviewer = ScriptedModel([], "r")
        worker = ScriptedModel([ASK], "w")
        with patch("m2k.worker.check_all") as gate, patch("m2k.tools.execute") as execute:
            report = Kernel(self.ledger, worker, self.reviewer).run(self.contract())
        self.assert_awaiting(report)
        self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
        self.assertIn("question", report.worker.reason)
        self.assertEqual(report.usage["model_calls"], 1)
        self.assertEqual(report.usage["rounds"], 1)
        self.assertEqual(self.ledger.last("t1", "owner.request").payload,
                         dict(QUESTION, request=None, by="worker"))
        self.assertIn("ask_owner", worker.calls[0][0]["content"])
        self.assertNotIn("ask_owner", self.contract().tools)
        self.assertNotIn("ask_owner", tools.TOOLS)
        gate.assert_not_called()
        execute.assert_not_called()

    def test_valid_requests_are_recorded_without_applying_requested_changes(self):
        cases = [("scope", {"writes": ["elsewhere/"], "tools": ["run_command"], "network": True}),
                 ("budget", {"budget": {"max_rounds": 999, "max_tokens": 999999}})]
        for kind, req in cases:
            with self.subTest(kind=kind):
                contract = self.contract(task_id=kind)
                self.reviewer = ScriptedModel([], "r")
                report = Kernel(self.ledger, ScriptedModel([step("ask_owner", kind=kind, text="需要授权", request=req)]),
                                self.reviewer).run(contract)
                self.assert_awaiting(report, kind=kind)
                recorded = self.ledger.last(kind, "task.submitted").payload["contract"]
                self.assertEqual(recorded["budget"], asdict(contract.budget))
                self.assertFalse(recorded["network"])
                self.assertEqual(self.ledger.last(kind, "owner.request").payload["request"], req)

    def test_invalid_args_use_invalid_reply_feedback_and_stall(self):
        bad = [{}, {"kind": "other", "text": "x"}, {"kind": [], "text": "x"},
               {"kind": "question"}, {"kind": "question", "text": None},
               {"kind": "question", "text": " "}, {"kind": "question", "text": "x" * 2001},
               dict(QUESTION, extra=True), dict(QUESTION, request=[]), dict(QUESTION, request=None),
               {"kind": "scope", "text": "x", "request": {"network": 1}},
               {"kind": "scope", "text": "x", "request": {"writes": "x"}},
               {"kind": "scope", "text": "x", "request": {"writes": [1]}},
               {"kind": "scope", "text": "x", "request": {"tools": [""]}},
               {"kind": "scope", "text": "x", "request": {"unknown": []}},
               {"kind": "budget", "text": "x", "request": {}},
               {"kind": "budget", "text": "x", "request": {"budget": []}},
               *[{"kind": "budget", "text": "x", "request": {"budget": {k: v}}}
                 for k, v in (("other", 1), ("max_rounds", True), ("max_tokens", None),
                              ("max_model_calls", 1.5), ("max_commands", -1))]]
        for i, args in enumerate(bad):
            with self.subTest(args=args):
                worker = ScriptedModel([step("ask_owner", **args)] * 3, "w")
                report = Kernel(self.ledger, worker, ScriptedModel([], "r")).run(
                    self.contract(task_id=f"bad-{i}", check="pass\n"))
                self.assertEqual(report.worker.status, WorkerStatus.STALLED)
                self.assertEqual(len(self.ledger.events(report.task_id, "proposal.invalid")), 3)
                self.assertFalse(self.ledger.events(report.task_id, "owner.request"))
                self.assertIsNone(report.awaiting)
                self.assertIn("INVALID REPLY", worker.calls[1][-1]["content"])

    def external(self, payload=None, mode="good"):
        # Reuse T37's real fake CLI, adding only the T79 file protocol before it runs.
        code = ('import sys, runpy\nfrom pathlib import Path\n'
                'p = Path(".m2k/ask.json")\nassert not p.exists(), "stale request survived"\n')
        if payload is not None:
            code += f'p.parent.mkdir(exist_ok=True)\np.write_bytes({payload!r})\n'
        code += 'sys.argv = sys.argv[1:]\nrunpy.run_path(sys.argv[0], run_name="__main__")\n'
        return CliAgent([sys.executable, "-c", code, FAKE, mode, str(self.root / "seen")],
                        name="fake-codex", env=ENV, timeout_s=2 if mode == "sleep" else 60, usage="codex-jsonl")

    def test_external_request_uses_the_same_protocol_and_is_not_in_candidate(self):
        self.reviewer = ScriptedModel([], "r")
        report = Kernel(self.ledger, self.external(json.dumps(QUESTION).encode()), self.reviewer).run(self.contract())
        self.assert_awaiting(report)
        self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
        self.assertFalse(any(p.startswith(".m2k/") for p in report.candidate.files))
        self.assertIn(".m2k/ask.json", self.seen("good", "prompt.txt"))

    def test_external_invalid_files_stall_and_failure_timeout_token_limit_ignore_the_file(self):
        invalid = [b"not json", b"[]", b"\xff", b"x" * 20_001,
                   json.dumps(dict(QUESTION, request=1)).encode()]
        for i, payload in enumerate(invalid):
            with self.subTest(i=i):
                report = Kernel(self.ledger, self.external(payload), ScriptedModel([], "r")).run(
                    self.contract(task_id=f"invalid-{i}"))
                self.assertEqual(report.worker.status, WorkerStatus.STALLED)
                self.assertTrue(report.worker.reason.startswith("invalid .m2k/ask.json:"))
                self.assertIsNone(report.awaiting)
        for mode, expected in (("fail", WorkerStatus.MODEL_ERROR), ("sleep", WorkerStatus.STALLED),
                               ("budget", WorkerStatus.BUDGET_EXHAUSTED)):
            with self.subTest(mode=mode):
                report = Kernel(self.ledger, self.external(json.dumps(QUESTION).encode(), mode), ScriptedModel([], "r")).run(
                    self.contract(task_id=mode, budget=Budget(max_tokens=50000)))
                self.assertEqual(report.worker.status, expected)
                requests = self.ledger.events(mode, "owner.request")
                self.assertEqual([e.payload["by"] for e in requests], ["framework"] if mode == "budget" else [])

    def test_deeply_nested_ask_file_stalls_and_still_freezes_and_records_verdict(self):
        report = Kernel(self.ledger, self.external(b"[" * 3000 + b"]" * 3000), ScriptedModel([], "r")).run(
            self.contract())
        self.assertEqual(report.worker.status, WorkerStatus.STALLED)
        self.assertTrue(report.worker.reason.startswith("invalid .m2k/ask.json:"), report.worker.reason)
        self.assertIsNone(report.awaiting)
        self.assertIsNone(report.delivered_to)
        self.assertEqual(self.ledger.last("t1", "candidate.frozen").payload["digest"], report.candidate.digest)
        self.assertIsNotNone(self.ledger.last("t1", "test.result"))
        self.assertEqual(self.ledger.last("t1", "verdict").payload["verdict"], report.verdict.value)

    def test_seed_stale_file_is_deleted_before_the_external_agent_starts(self):
        seed = self.contract(task_id="seed").workspace
        (seed / ".m2k").mkdir()
        (seed / ".m2k/ask.json").write_text(json.dumps(QUESTION), encoding="utf-8")
        contract = replace(self.contract(), workspace=self.root / "fresh", seed=seed)
        report = Kernel(self.ledger, self.external(), ScriptedModel([review("GO")], "r")).run(contract)
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertIsNone(report.awaiting)
        self.assertTrue((seed / ".m2k/ask.json").exists())

    def test_plain_waiting_paths_keep_the_request_in_memory_without_global_reads(self):
        for mode in ("native", "external", "framework"):
            with self.subTest(mode=mode):
                contract = self.contract(task_id=mode, budget=Budget(max_rounds=1))
                worker = (self.external(json.dumps(QUESTION).encode()) if mode == "external" else
                          ScriptedModel([WRITE if mode == "framework" else ASK], "w"))
                self.reviewer = ScriptedModel([], "r")
                events = self.ledger.events
                def scoped(task_id=None, kind=None):
                    self.assertIsNotNone(task_id, "plain runs and adjudication must read one task at a time")
                    self.assertNotEqual(kind, "owner.request", "the request should come from the worker or append result")
                    return events(task_id, kind)
                with patch.object(self.ledger, "events", side_effect=scoped):
                    report = Kernel(self.ledger, worker, self.reviewer).run(contract)
                    self.assertNotEqual(adjudicate(self.ledger, mode).verdict, "PASS")
                self.assert_awaiting(report, by="framework" if mode == "framework" else "worker",
                                     kind="budget" if mode == "framework" else "question", reviewed=mode == "framework")

    def test_framework_budget_request_on_first_and_repair_runs(self):
        for repair in (False, True):
            with self.subTest(repair=repair):
                contract = replace(self.contract(task_id=f"budget-{repair}",
                                   budget=Budget(max_model_calls=4 if repair else 1)), max_repairs=2)
                steps = [step("write_file", path="calc.py", content=BAD_CODE), step("finish"), WRITE] if repair else [WRITE]
                self.reviewer = ScriptedModel([review("NO_GO")] if repair else [], "r")
                report = Kernel(self.ledger, ScriptedModel(steps, "w"), self.reviewer).run(contract)
                self.assertEqual(report.worker.status, WorkerStatus.BUDGET_EXHAUSTED)
                self.assertEqual(report.verdict, Verdict.UNKNOWN)
                self.assertEqual(report.reasons[0], f"repair attempts: {int(repair)}")
                self.assertTrue(report.reasons[1].startswith("awaiting owner: budget:"))
                self.assertEqual(report.awaiting["by"], "framework")
                self.assertEqual(report.awaiting["text"], report.worker.reason)
                self.assertEqual(len(self.ledger.events(contract.task_id, "owner.request")), 1)
                self.assertEqual(len(self.ledger.events(contract.task_id, "repair.started")), int(repair))
                self.assertEqual(len(self.reviewer.calls), int(repair))
                self.assertIsNone(report.audit)
                self.assertIsNone(report.delivered_to)


class OwnerTreeTest(RecallCase):
    def test_child_question_stops_parent_without_reassignment_or_later_children(self):
        events = self.ledger.events
        def scoped(task_id=None, kind=None):
            self.assertIsNotNone(task_id)
            self.assertNotEqual(kind, "owner.request")
            return events(task_id, kind)
        with patch.object(self.ledger, "events", side_effect=scoped):
            report = self.kernel([ASK] + good_a() + good_b(), []).run(self.tree(reassign=2))
        self.assertEqual(report.worker.status, WorkerStatus.AWAITING_OWNER)
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        self.assertEqual(len(self.k.worker_model.calls), 1)
        self.assertFalse(self.k.reviewer_model.calls)
        self.assertEqual([e.payload["task_id"] for e in self.ledger.events("p1", "subtask.started")], ["p1.a"])
        self.assertFalse(self.ledger.events("p1.a.e2"))
        self.assertFalse(self.ledger.events("p1.b"))
        self.assertEqual(status(self.ledger, "p1")["awaiting"][0]["task"], "p1.a")
        self.assertEqual(self.ledger.last("p1.a", "worker.stopped").payload["status"], "AWAITING_OWNER")
        event = self.ledger.last("p1.a", "owner.request")
        self.assertEqual(report.awaiting, dict(event.payload, seq=event.seq))

    def test_child_budget_exhaustion_keeps_the_previous_failure_behavior(self):
        report = self.kernel([step("read_file", path="check_a.py")], []).run(
            self.tree(reassign=0, a_budget=(1, 2, 1)))
        self.assertEqual(report.worker.status, WorkerStatus.SUBTASK_FAILED)
        self.assertEqual(report.verdict, Verdict.FAIL)
        self.assertIsNone(report.awaiting)
        self.assertFalse(self.ledger.events(kind="owner.request"))

    def test_answer_constraint_invalidates_all_subtask_reuse(self):
        first = self.tree(reassign=0)
        self.kernel(good_a() + good_b(), [review("GO")] * 3).run(first)
        answer = "Owner's answer: 两个部分都保持原有接口。"
        second = replace(first, task_id="p2", workspace=self.root / "ws/p2", constraints=(answer,),
                         budget=Budget(max_rounds=30, max_model_calls=40, max_commands=12))
        kernel = self.kernel(good_a() + good_b(), [review("GO")] * 3)
        result = kernel.run(second, amends="p1", approval=owner_approval(self.ledger, "p1", second))
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        impact = self.ledger.last("p2", "task.submitted").payload["amends"]["impact"]
        self.assertEqual(impact, {"reused": {}, "rerun": {"a": "parent changed: constraints", "b": "an earlier subtask reruns"}})
        for messages in kernel.worker_model.calls + kernel.reviewer_model.calls:
            self.assertIn(answer, "\n".join(m["content"] for m in messages))


class ApprovalNeedsTest(KernelCase):
    def setUp(self):
        super().setUp()
        self.old = json.loads(json.dumps(asdict(replace(self.contract(), writes=("pkg/",), tools=("read_file",),
                                                       budget=Budget(10, 12, 100, 4))), default=str))
        self.ledger.append("t1", "task.submitted", {"contract": self.old})

    def test_scope_widening_and_narrowing(self):
        cases = [(dict(tools=["read_file", "write_file"]), "tools"), (dict(network=True), "network"),
                 (dict(writes=[]), "writes"), (dict(writes=["pkg/", "other.py"]), "writes"),
                 (dict(writes=["pkg/sub/", "pkg/file.py"]), None), (dict(tools=[]), None)]
        for change, field in cases:
            with self.subTest(change=change):
                owner = dict(self.old, **change)
                needs = approval_needs(self.ledger, "t1", owner)
                self.assertEqual(needs, {"requests": [], "scope": {field: [self.old[field], owner[field]]} if field else {},
                                         "budget": {}})
        self.ledger.append("t1", "task.submitted", {"contract": dict(self.old, writes=[])})
        self.assertEqual(approval_needs(self.ledger, "t1", self.old)["scope"], {})

    def test_each_budget_raise_including_unbounded_compares_owner_budgets(self):
        for field, old in self.old["budget"].items():
            for new, raises in ((old + 1, True), (None, True), (old - 1, False), (old, False)):
                with self.subTest(field=field, new=new):
                    owner = dict(self.old, budget=dict(self.old["budget"], **{field: new}))
                    self.assertEqual(approval_needs(self.ledger, "t1", owner)["budget"], {field: [old, new]} if raises else {})
        previous = dict(self.old, budget=dict.fromkeys(self.old["budget"], 1))
        self.ledger.append("t1", "task.submitted", {"contract": previous, "amends": {"owner_budget": self.old["budget"]}})
        self.assertFalse(any(approval_needs(self.ledger, "t1", self.old).values()))
        self.ledger.append("t1", "task.submitted", {"contract": dict(self.old, budget=dict.fromkeys(self.old["budget"]))})
        self.assertEqual(approval_needs(self.ledger, "t1", self.old)["budget"], {})

    def test_requests_follow_parent_links_in_global_sequence_order(self):
        self.ledger.append("child", "task.submitted", {"parent": "t1"})
        self.ledger.append("nested", "task.submitted", {"parent": "child"})
        wanted = [self.ledger.append(tid, "owner.request", dict(QUESTION, by="worker")).seq
                  for tid in ("nested", "t1", "child")]
        self.ledger.append("t1.unrelated", "owner.request", QUESTION)
        self.assertEqual(approval_needs(self.ledger, "t1", self.old)["requests"], wanted)


class OwnerAmendTest(WitnessFiles, AmendCase):
    def waiting_version(self):
        first = self.contract()
        report = self.run_version(first, [WRITE, ASK])
        self.assertEqual(report.verdict, Verdict.UNKNOWN)
        return first

    def test_missing_stale_and_wrong_source_approvals_write_no_events(self):
        first = self.waiting_version()
        second = self.next_contract(first, budget=replace(first.budget, max_model_calls=40))
        self.refuse(second, "t1", "approval required.*requests.*budget.*m2k answer.*terminal")
        self.assertFalse(second.workspace.exists())
        for approval in ({"by": "m2k answer", "text": None, "needs": {}},
                         dict(owner_approval(self.ledger, "t1", second), by="worker")):
            with self.subTest(approval=approval):
                before = self.ledger.events()
                with self.assertRaisesRegex(KernelError, "situation changed"):
                    self.run_version(replace(second, task_id=str(len(before)) + approval["by"],
                                             workspace=self.root / approval["by"]), [], amends="t1", approval=approval)
                self.assertEqual(self.ledger.events(), before)
                self.assertFalse((self.root / approval["by"]).exists())

    def test_scope_and_budget_each_require_approval_before_any_event(self):
        first = replace(self.contract(), tools=("write_file",), writes=("calc.py",), budget=Budget(10, 12, 1000, 4))
        self.run_version(first, [WRITE, step("finish")])
        changes = [dict(tools=first.tools + ("read_file",)), dict(network=True), dict(writes=()),
                   *[dict(budget=replace(first.budget, **{k: v + 1})) for k, v in asdict(first.budget).items()]]
        for i, change in enumerate(changes):
            with self.subTest(change=change):
                self.refuse(self.next_contract(first, f"refuse-{i}", **change), "t1", "approval required.*m2k answer")

    def test_approved_raise_has_exact_fields_valid_obs_and_effective_credential(self):
        observer, _, public, log = self.client()
        caps = {"tools": list(self.contract().tools), "max_rounds": 100, "max_model_calls": 100,
                "max_tokens": 100000, "max_commands": 100, "network": False}
        setup, _ = trust.dev_setup(self.root / "keys", caps)
        first = self.contract(budget=Budget(10, 12, 10000, 4))
        self.run_version(first, [WRITE, step("finish")], trust=setup, obs=observer)
        second = self.next_contract(first, budget=Budget(12, 14, 11000, 5), constraints=("Owner's answer: 保留接口",))
        approval = owner_approval(self.ledger, "t1", second, "保留接口")
        report = self.run_version(second, [step("finish")], amends="t1", approval=approval, trust=setup, obs=observer)
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertEqual(self.submission("t2")["amends"]["approval"], approval)
        raised, = self.ledger.events("t2", "budget.raised")
        self.assertEqual(raised.payload, {"by": "owner", "task": "t2", "amends": "t1", "fields": approval["needs"]["budget"]})
        kinds = [e.kind for e in self.ledger.events("t2") if e.kind != "obs.receipt"]
        self.assertEqual(kinds[:3], ["task.submitted", "budget.raised", "credential.issued"])
        witnessed = next(e.payload["receipt"] for e in self.ledger.events("t2", "obs.receipt")
                         if e.payload["receipt"]["body"]["ledger_seq"] == raised.seq)
        receipt_body(witnessed, public, request(raised, self.ledger.ledger_id))
        effective = replace(second, budget=Budget(**self.submission("t2")["contract"]["budget"]))
        cred = self.ledger.last("t2", "credential.issued").payload["credential"]["body"]
        self.assertEqual(cred["contract_sha256"], effective.digest())
        self.assertEqual(adjudicate(self.ledger, "t2", obs_public_key=public, obs_log=log).verdict, "PASS")

    def test_two_concurrent_answers_have_one_winner_and_no_loser_events(self):
        first = self.waiting_version()
        before = self.ledger.events("t1")
        barrier = Barrier(2)
        def submit(tid):
            contract = self.next_contract(first, tid, budget=replace(first.budget, max_model_calls=40))
            with Ledger(self.ledger.path) as ledger:
                approval = owner_approval(ledger, "t1", contract)
                append = ledger.append
                def synchronized(task_id, kind, *args, **kwargs):
                    if kind == "task.submitted":
                        barrier.wait(timeout=10)
                    return append(task_id, kind, *args, **kwargs)
                worker, reviewer = ScriptedModel([step("finish")], "w"), ScriptedModel([review("GO")], "r")
                with patch.object(ledger, "append", side_effect=synchronized):
                    try:
                        result = Kernel(ledger, worker, reviewer).run(contract, amends="t1", approval=approval)
                    except KernelError as e:
                        result = e
                return tid, result, len(worker.calls), len(reviewer.calls)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, tid) for tid in ("left", "right")]
            rows = [f.result(timeout=30) for f in futures]
        losers = [row for row in rows if isinstance(row[1], KernelError)]
        self.assertEqual(len(losers), 1)
        loser, error, w, r = losers[0]
        self.assertIn("already amended", str(error))
        self.assertEqual((w, r), (0, 0))
        self.assertEqual(self.ledger.events(loser), [])
        winner, report, w, r = next(row for row in rows if not isinstance(row[1], KernelError))
        self.assertEqual((report.reported_verdict, w, r), (Verdict.PASS, 1, 1), report.reasons)
        self.assertEqual(len(self.ledger.events(winner, "budget.raised")), 1)
        self.assertEqual(self.ledger.events("t1"), before)
        self.assertEqual(self.ledger.verify_chain(), (True, None))

    def test_request_inserted_just_before_submit_is_rechecked_under_the_transaction(self):
        first, _ = self.first()
        self.ledger.append("child", "task.submitted", {"parent": "t1"})
        second = self.next_contract(first, budget=replace(first.budget, max_model_calls=40))
        approval = owner_approval(self.ledger, "t1", second)
        append = self.ledger.append
        def changed(tid, kind, *args, **kwargs):
            if tid == "t2" and kind == "task.submitted":
                with Ledger(self.ledger.path) as other:
                    other.append("child", "owner.request", dict(QUESTION, request=None, by="worker"))
            return append(tid, kind, *args, **kwargs)
        from m2k.adjudicate import approval_needs as compute
        transactions = []
        def transactional(*args):
            transactions.append(self.ledger._db.in_transaction)
            return compute(*args)
        with patch.object(self.ledger, "append", side_effect=changed), \
                patch("m2k.kernel.approval_needs", side_effect=transactional), self.assertRaisesRegex(KernelError, "situation changed"):
            self.run_version(second, [], amends="t1", approval=approval)
        self.assertEqual(self.ledger.events("t2"), [])
        self.assertFalse(self.worker.calls)
        self.assertEqual(transactions, [False, True])


class OwnerCliTest(WitnessFiles, AmendCase):
    def cli_files(self, budget=""):
        config, task = self.root / "m2k.toml", self.root / "task.toml"
        config.write_text('[sandbox]\nbackend = "none"\n[models.worker]\nmodel = "w"\n'
                          '[models.reviewer]\nmodel = "r"\n', encoding="utf-8")
        task.write_text('[task]\nid = "t2"\ngoal = "keep add working"\nworkspace = "ws/t2"\n'
                        '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n' + budget, encoding="utf-8")
        return ["--config", str(config), "--ledger", str(self.ledger.path)], str(task)

    def trusted_cli_files(self):
        args, task = self.cli_files('[budget]\nmax_tokens = 1000\n')
        caps = {"tools": list(self.contract(deliver=False).tools), "max_rounds": 100, "max_model_calls": 100,
                "max_tokens": 100000, "max_commands": 100, "network": False}
        setup, root_key = trust.dev_setup(self.root / "keys", caps)
        (self.root / "keys/root-pin.json").write_bytes(trust.canonical(
            {"trust_domain": "TEST_ONLY", "public_key_b64": trust.public_key_b64(root_key)}))
        (self.root / "keys/charter.json").write_bytes(trust.canonical(setup.charter_env))
        _, public, key, log = self.keys()
        config = self.root / "m2k.toml"
        config.write_text(config.read_text(encoding="utf-8") +
                          '[trust]\ndomain = "TEST_ONLY"\nroot_pin = "keys/root-pin.json"\n'
                          'charter = "keys/charter.json"\nissuer_key = "keys/issuer.pem"\n'
                          'revocations = "keys/revocations.json"\nrelease = "keys/release.json"\n'
                          f'[obs]\nkey = "{key.name}"\nlog = "{log.name}"\npublic_key_b64 = "{public}"\n',
                          encoding="utf-8")
        (self.root / "keys/release.json").write_bytes(trust.canonical(trust.sign(
            {"schema": trust.RELEASE, "trust_domain": "TEST_ONLY", "manifest_id": "owner-test", "slot": "test",
             "version": 1, "code": "dev", "config": trust.config_digest(config.read_bytes()), "issued_at": 1}, root_key)))
        path = self.root / "task.toml"
        successor = path.read_text(encoding="utf-8")
        path.write_text(successor.replace('"t2"', '"t1"').replace('ws/t2', 'ws/t1'), encoding="utf-8")
        with patch("m2k.cli.build_model", side_effect=[ScriptedModel([WRITE, ASK], "w"), ScriptedModel([], "r")]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(args + ["run", task]), 3)
        self.assertIsNotNone(self.ledger.last("m2k.startup", "release.checked"))
        self.assertIsNotNone(self.ledger.last("m2k.startup", "obs.receipt"))
        path.write_text(successor + 'max_model_calls = 40\n', encoding="utf-8")
        return args, task

    def test_kernel_refusal_keeps_empty_workspace_for_same_contract_answer(self):
        self.first()
        args, task = self.cli_files('[budget]\nmax_model_calls = 40\n')
        contract = load_contract(task)
        contract.workspace.mkdir()
        before = self.ledger.events()
        with self.assertRaisesRegex(KernelError, "approval required"):
            self.run_version(contract, [], amends="t1")
        self.assertEqual(list(contract.workspace.iterdir()), [])
        self.assertEqual(self.ledger.events(), before)
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="YES"), \
                patch("m2k.cli.build_model", side_effect=[ScriptedModel([step("finish")], "w"),
                                                        ScriptedModel([review("GO")], "r")]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(args + ["answer", "t1", task, "--untrusted"]), 0)
        self.assertEqual(self.ledger.last("t2", "verdict").payload["verdict"], "PASS")

    def test_trusted_plain_amend_refusal_leaves_ledger_and_obs_unchanged(self):
        args, task = self.trusted_cli_files()
        before, witnessed = self.ledger.events(), (self.root / "obs.jsonl").read_bytes()
        with patch("m2k.cli.build_model", side_effect=[ScriptedModel([], "w"), ScriptedModel([], "r")]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(SystemExit, "m2k answer"):
            main(args + ["run", "--amends", "t1", task])
        self.assertEqual(self.ledger.events(), before)
        self.assertEqual((self.root / "obs.jsonl").read_bytes(), witnessed)
        self.assertFalse((self.root / "ws/t2").exists())

    def test_trusted_stale_answer_leaves_no_startup_events(self):
        args, task = self.trusted_cli_files()
        before, witnessed = self.ledger.events(), (self.root / "obs.jsonl").read_bytes()
        inserted = []
        def confirm(_):
            self.assertEqual(self.ledger.events(), before)
            inserted.append(self.ledger.append("t1", "owner.request", dict(QUESTION, request=None, by="worker")))
            return "YES"
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=confirm), \
                patch("m2k.cli.build_model", side_effect=[ScriptedModel([], "w"), ScriptedModel([], "r")]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(SystemExit, "情况已改变"):
            main(args + ["answer", "t1", task])
        self.assertEqual(self.ledger.events(), before + inserted)
        self.assertEqual((self.root / "obs.jsonl").read_bytes(), witnessed)
        self.assertFalse((self.root / "ws/t2").exists())

    def test_trusted_answer_invalid_predecessors_are_clear_and_write_nothing(self):
        args, task = self.trusted_cli_files()
        self.ledger.append("child", "task.submitted", dict(self.submission("t1"), parent="t1"))
        self.ledger.append("unfinished", "task.submitted", self.submission("t1"))
        self.ledger.append("legacy", "task.submitted", {})
        self.ledger.append("unfrozen", "task.submitted", self.submission("t1"))
        self.ledger.append("unfrozen", "verdict", {"verdict": "UNKNOWN"})
        for tid, reason in (("missing", "任务不存在"), ("child", "子任务"), ("unfinished", "尚无判定"),
                            ("legacy", "没有契约"), ("unfrozen", "没有冻结候选")):
            before, witnessed = self.ledger.events(), (self.root / "obs.jsonl").read_bytes()
            with self.subTest(tid=tid), patch("sys.stdin.isatty", return_value=True), \
                    patch("builtins.input") as confirm, redirect_stderr(io.StringIO()) as err, \
                    self.assertRaisesRegex(SystemExit, reason):
                main(args + ["answer", tid, task])
            confirm.assert_not_called()
            self.assertNotIn("Traceback", err.getvalue())
            self.assertEqual(self.ledger.events(), before)
            self.assertEqual((self.root / "obs.jsonl").read_bytes(), witnessed)

    def test_trusted_cancellation_happens_before_any_startup_record(self):
        args, task = self.trusted_cli_files()
        before, witnessed = self.ledger.events(), (self.root / "obs.jsonl").read_bytes()
        def cancel(_):
            self.assertEqual(self.ledger.events(), before)
            return "NO"
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=cancel), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(args + ["answer", "t1", task]), 1)
        self.assertEqual(self.ledger.events(), before)
        self.assertEqual((self.root / "obs.jsonl").read_bytes(), witnessed)

    def test_non_tty_rejected_before_config_or_any_ledger_write(self):
        before = self.ledger.events()
        with patch("sys.stdin.isatty", return_value=False), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["--config", "missing.toml", "answer", "t1", "missing-task.toml"]), 2)
        self.assertIn("只能由所有者在交互终端里运行", err.getvalue())
        self.assertEqual(self.ledger.events(), before)

    def test_cancellation_and_invalid_text_write_nothing(self):
        self.first()
        args, task = self.cli_files()
        before = self.ledger.events()
        for value in ("NO", "yes", " YES", "YES ", ""):
            with self.subTest(value=value), patch("sys.stdin.isatty", return_value=True), \
                    patch("builtins.input", return_value=value), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args + ["answer", "t1", task]), 1)
                self.assertIn("已取消", out.getvalue())
                self.assertEqual(self.ledger.events(), before)
                self.assertFalse((self.root / "ws/t2").exists())
        for text in ("", " ", "x" * 2001):
            with self.subTest(text=text[:10]), patch("sys.stdin.isatty", return_value=True), \
                    patch("builtins.input") as confirm, redirect_stderr(io.StringIO()):
                self.assertEqual(main(args + ["answer", "t1", task, "--text", text]), 2)
                confirm.assert_not_called()
                self.assertEqual(self.ledger.events(), before)

    def test_yes_runs_the_answer_as_a_signed_contract_constraint_and_shows_status(self):
        first = self.contract()
        self.run_version(first, [WRITE, ASK])
        args, task = self.cli_files('[budget]\nmax_model_calls = 40\n')
        worker, reviewer = ScriptedModel([step("finish")], "w"), ScriptedModel([review("GO")], "r")
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="YES") as confirm, \
                patch("m2k.cli.build_model", side_effect=[worker, reviewer]), redirect_stdout(io.StringIO()) as out, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(main(args + ["answer", "t1", task, "--untrusted", "--text", "保留接口"]), 0)
        self.assertEqual(confirm.call_args.args, ("输入 YES 确认（其他任何输入取消）：",))
        for text in ("修订任务：t1；新版本：2", "待答请求：序号", "任务 t1", "question", QUESTION["text"],
                     '"request": null', "预算 max_model_calls：30 → 40", "所有者回答：保留接口", "t2: PASS"):
            self.assertIn(text, out.getvalue())
        submission = self.submission("t2")
        self.assertEqual(submission["contract"]["constraints"], ["Owner's answer: 保留接口"])
        self.assertEqual(submission["amends"]["approval"]["text"], "保留接口")
        for messages in worker.calls + reviewer.calls:
            self.assertIn("Owner's answer: 保留接口", "\n".join(m["content"] for m in messages))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(args + ["status", "t1"]), 0)
        state = json.loads(out.getvalue())
        self.assertEqual(state["answered_by"], "t2")
        self.assertEqual(state["awaiting"], [{"seq": self.ledger.last("t1", "owner.request").seq, "task": "t1", **QUESTION}])

    def test_plain_amends_rejects_raise_without_traceback_or_new_events(self):
        self.first()
        args, task = self.cli_files('[budget]\nmax_model_calls = 40\n')
        before = self.ledger.events()
        with patch("m2k.cli.build_model", side_effect=[ScriptedModel([], "w"), ScriptedModel([], "r")]), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, \
                self.assertRaisesRegex(SystemExit, "所有者批准.*m2k answer"):
            main(args + ["run", "--untrusted", "--amends", "t1", task])
        self.assertEqual(self.ledger.events(), before)
        self.assertNotIn("Traceback", err.getvalue())

    def test_run_waits_with_exit_three_and_prints_the_answer_command(self):
        args, task = self.cli_files()
        self.contract(task_id="t2")
        for steps, budget in (([ASK], ""), ([step("read_file", path="check.py")], '[budget]\nmax_rounds = 1\n')):
            with self.subTest(budget=budget):
                if self.ledger.events("t2"):
                    args, task = self.cli_files(budget)
                    path = self.root / "task.toml"
                    path.write_text(path.read_text(encoding="utf-8").replace('"t2"', '"t3"').replace('ws/t2', 'ws/t3'), encoding="utf-8")
                    self.contract(task_id="t3")
                with patch("m2k.cli.build_model", side_effect=[ScriptedModel(steps, "w"), ScriptedModel([], "r")]), \
                        redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                    self.assertEqual(main(args + ["run", "--untrusted", task]), 3)
                self.assertIn("⏸ 待授权（", out.getvalue())
                self.assertIn("<新契约.toml>", out.getvalue())
                self.assertIn("m2k answer", out.getvalue())
