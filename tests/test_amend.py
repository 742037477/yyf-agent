"""T48: finished tasks form a linear version chain with fresh evidence and shared budgets."""
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from threading import Barrier
from unittest.mock import patch

from m2k import trust
from m2k.adjudicate import adjudicate
from m2k.cli import main
from m2k.contract import Budget
from m2k.kernel import Kernel, KernelError
from m2k.ledger import Ledger
from m2k.models import ModelReply, ScriptedModel
from m2k.tools import Workspace
from m2k.types import Verdict
from m2k.verify import freeze, freeze_one
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, owner_approval, review, step
from tests.test_external_agent import AgentCase
from tests.test_recall import RecallCase, good_a, good_b, wrong_a


class AmendCase(KernelCase):
    def run_version(self, contract, steps, decisions=("GO",), amends=None, approval=None, **options):
        self.worker = ScriptedModel(steps, "worker")
        self.reviewer = ScriptedModel([review(d, "recorded review reason") for d in decisions], "reviewer")
        return Kernel(self.ledger, self.worker, self.reviewer, **options).run(contract, amends=amends, approval=approval)

    def first(self, code=GOOD_CODE, **options):
        contract = self.contract(**options)
        result = self.run_version(contract, [step("write_file", thought="PRIVATE-THOUGHT", path="calc.py", content=code),
                                             step("finish", summary="PRIVATE-SUMMARY")])
        return contract, result

    def next_contract(self, previous, task_id="t2", **changes):
        return replace(previous, task_id=task_id, workspace=previous.workspace.parent / task_id, seed=None, **changes)

    def submission(self, task_id):
        return self.ledger.last(task_id, "task.submitted").payload

    def refuse(self, contract, previous, message):
        before = self.ledger.events()
        with self.assertRaisesRegex(KernelError, message):
            self.run_version(contract, [], amends=previous)
        self.assertEqual(self.ledger.events(), before)
        self.assertEqual(self.ledger.events(contract.task_id), [])


class AmendTest(AgentCase, AmendCase):
    def test_top_level_records_the_canonical_contract(self):
        contract, _ = self.first()
        submitted = self.submission("t1")
        data = json.loads(json.dumps(asdict(contract), default=str))
        self.assertEqual(submitted["contract"], data)
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        self.assertEqual(hashlib.sha256(raw.encode("utf-8")).hexdigest(), submitted["contract_sha256"])
        self.assertNotIn("amends", submitted)

    def test_failed_version_continues_exact_files_and_delivers_with_fresh_evidence(self):
        first = replace(self.contract(), context="MODEL-CONTEXT")
        (first.workspace / "nested").mkdir()
        (first.workspace / "nested" / "data.bin").write_bytes(b"\x00\xff\r\n")
        old = self.run_version(first, [step("write_file", thought="PRIVATE-THOUGHT", path="calc.py", content=BAD_CODE),
                                       step("finish", summary="PRIVATE-SUMMARY")], decisions=("NO_GO",))
        self.assertEqual(old.verdict, Verdict.FAIL)
        old_events = self.ledger.events("t1")
        expected = {rel: (first.workspace / rel).read_bytes() for rel in old.candidate.files}
        (first.workspace / "__pycache__").mkdir(exist_ok=True)
        (first.workspace / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
        second = self.next_contract(first, goal="Fix add to return the sum.",
                                    acceptance=replace(first.acceptance, timeout_s=31))

        def inspect(messages):
            self.assertEqual(freeze(Workspace(second.workspace)), old.candidate)
            self.assertEqual({rel: (second.workspace / rel).read_bytes() for rel in old.candidate.files}, expected)
            self.assertFalse((second.workspace / "__pycache__").exists())
            return step("write_file", path="calc.py", content=GOOD_CODE)

        result = self.run_version(second, [inspect, step("finish")], amends="t1")
        self.assertEqual((result.verdict, result.reported_verdict), (Verdict.PASS, Verdict.PASS), result.reasons)
        self.assertEqual((result.delivered_to / "calc.py").read_bytes(), GOOD_CODE.encode())
        amendment = self.submission("t2")["amends"]
        self.assertEqual(amendment["from_candidate"], old.candidate.digest)
        self.assertEqual(amendment["changed"], ["acceptance", "goal"])
        prompt = self.worker.calls[0][1]["content"]
        for fact in ("version 2", "amends t1", "acceptance, goal", "FAIL", "recorded review reason", "Remaining budget"):
            self.assertIn(fact, prompt)
        for secret in ("PRIVATE-THOUGHT", "PRIVATE-SUMMARY"):
            self.assertNotIn(secret, prompt)
            self.assertNotIn(secret, self.reviewer.calls[0][0]["content"])
        audit = self.reviewer.calls[0][0]["content"]
        line = "Contract version 2; the owner changed: acceptance, goal. Judge only against the current Goal, Constraints and acceptance."
        self.assertIn(line, audit)
        self.assertLess(audit.index(line), audit.index("<<<UNTRUSTED task-context"))
        self.assertIn("1 files changed or new since the task started", audit)
        self.assertEqual(self.ledger.events("t1"), old_events)
        for kind in ("test.result", "audit.result", "verdict"):
            event = self.ledger.last("t2", kind)
            self.assertGreater(event.seq, old_events[-1].seq)
            self.assertEqual(event.payload["candidate"], result.candidate.digest)

    def test_seed_overlays_the_candidate_before_acceptance_hashes_and_baseline(self):
        first, old = self.first(BAD_CODE)
        seed = self.root / "owner-v2"
        seed.mkdir()
        check = "from calc import add\nassert add(8, 3) == 11\nprint('new owner acceptance')\n"
        (seed / "check.py").write_text(check, encoding="utf-8")
        second = replace(self.next_contract(first), seed=seed)

        def inspect(messages):
            self.assertEqual((second.workspace / "calc.py").read_bytes(), (first.workspace / "calc.py").read_bytes())
            self.assertEqual((second.workspace / "check.py").read_bytes(), (seed / "check.py").read_bytes())
            baseline = freeze(Workspace(second.workspace))
            self.assertEqual(self.submission("t2")["baseline"], baseline.digest)
            return step("write_file", path="calc.py", content=GOOD_CODE)

        result = self.run_version(second, [inspect, step("finish")], amends="t1")
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertIn("new owner acceptance", result.test.output)
        self.assertEqual(self.submission("t2")["acceptance_files"]["check.py"], freeze_one(seed / "check.py"))
        self.assertEqual(self.submission("t2")["protected"]["check.py"], freeze_one(seed / "check.py"))
        self.assertEqual(self.submission("t2")["amends"]["changed"], [])
        self.assertEqual(self.submission("t2")["amends"]["from_candidate"], old.candidate.digest)

    def test_model_call_budget_is_reduced_and_the_existing_gate_stops_the_worker(self):
        first, _ = self.first(budget=Budget(max_model_calls=5))
        prior = self.ledger.usage("t1")
        result = self.run_version(self.next_contract(first), [step("read_file", path="calc.py"),
                                  step("read_file", path="check.py"), step("finish")], amends="t1")
        submitted = self.submission("t2")
        self.assertEqual(submitted["amends"]["prior_usage"], prior)
        self.assertEqual(submitted["amends"]["owner_budget"], asdict(first.budget))
        self.assertEqual(submitted["contract"]["budget"]["max_model_calls"], 5 - prior["model_calls"])
        self.assertEqual(result.worker.status.value, "BUDGET_EXHAUSTED")
        self.assertIn("model call budget", result.worker.reason)
        self.assertEqual(len(self.worker.calls), 2)
        self.assertEqual(len(self.reviewer.calls), 0)
        self.assertIsNone(result.delivered_to)

    def test_each_exhausted_budget_field_refuses_without_writing(self):
        first = self.contract(budget=Budget(max_tokens=1000))
        self.run_version(first, [step("write_file", path="calc.py", content=GOOD_CODE),
                                step("run_command", command='{python} -c "pass"'), step("finish")])
        prior = self.ledger.usage("t1")
        for field in asdict(first.budget):
            used = prior[field.removeprefix("max_")]
            with self.subTest(field=field):
                second = self.next_contract(first, task_id=f"t2-{field}", budget=replace(first.budget, **{field: used}))
                self.refuse(second, "t1", f"budget already used up by earlier versions.*{field}.*{used}.*{used}")
                self.assertFalse(second.workspace.exists())
        retry = self.next_contract(first, task_id="retry")
        result = self.run_version(retry, [step("finish")], amends="t1")
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)

    def test_all_remaining_limits_are_used_before_credential_issue(self):
        caps = {"tools": list(self.contract().tools), "max_rounds": 100, "max_model_calls": 100,
                "max_tokens": 10000, "max_commands": 100, "network": False}
        setup, _ = trust.dev_setup(self.root / "keys", caps)
        first = self.contract(budget=Budget(max_tokens=2000))
        self.run_version(first, [step("write_file", path="calc.py", content=GOOD_CODE),
                                step("run_command", command='{python} -c "pass"'), step("finish")], trust=setup)
        second = self.next_contract(first, budget=Budget(max_rounds=25, max_model_calls=40, max_tokens=3000, max_commands=35))
        result = self.run_version(second, [step("finish")], amends="t1", trust=setup,
                                  approval=owner_approval(self.ledger, "t1", second))
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        effective = {k: v - self.ledger.usage("t1")[k.removeprefix("max_")] for k, v in asdict(second.budget).items()}
        submitted = self.submission("t2")
        self.assertEqual(submitted["contract"]["budget"], effective)
        self.assertEqual(submitted["amends"]["changed"], ["budget"])
        credential = self.ledger.last("t2", "credential.issued").payload["credential"]["body"]
        self.assertEqual(credential["contract_sha256"], replace(second, budget=Budget(**effective)).digest())

    def test_unknown_token_calls_are_recorded_and_only_known_tokens_reduce_the_budget(self):
        first = self.contract(budget=Budget(max_tokens=1000))
        worker = ScriptedModel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")], "worker")
        complete = worker.complete

        def unknown(messages):
            reply = complete(messages)
            return ModelReply(reply.text, None, None, reply.model)

        with patch.object(worker, "complete", side_effect=unknown):
            Kernel(self.ledger, worker, ScriptedModel([review("GO")], "reviewer")).run(first)
        result = self.run_version(self.next_contract(first), [step("finish")], amends="t1")
        submitted = self.submission("t2")
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(submitted["amends"]["prior_usage"]["tokens_unknown_calls"], 2)
        self.assertEqual(submitted["contract"]["budget"]["max_tokens"], 980)

    def test_third_version_counts_both_earlier_versions_and_compares_owner_budgets(self):
        first, _ = self.first()
        second = self.next_contract(first, goal="Keep add working.")
        self.run_version(second, [step("finish")], amends="t1")
        third = self.next_contract(second, task_id="t3", deliver_to=self.root / "another-output")
        result = self.run_version(third, [step("finish")], amends="t2")
        prior = {k: self.ledger.usage("t1")[k] + self.ledger.usage("t2")[k] for k in self.ledger.usage("t1")}
        amendment = self.submission("t3")["amends"]
        self.assertEqual(amendment["version"], 3)
        self.assertEqual(amendment["prior_usage"], prior)
        self.assertEqual(amendment["changed"], [])
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(adjudicate(self.ledger, "t3").verdict, "PASS")

    def test_missing_child_unfinished_and_pre_t48_tasks_are_refused(self):
        cases = (("missing", None, "missing"), ("child", {"parent": "p"}, "parent"),
                 ("unfinished", {"contract": {}}, "verdict"), ("legacy", {}, "contract"))
        for task_id, payload, message in cases:
            with self.subTest(task_id=task_id):
                if payload is not None:
                    self.ledger.append(task_id, "task.submitted", payload)
                    if task_id != "unfinished":
                        self.ledger.append(task_id, "verdict", {"verdict": "FAIL"})
                fresh = self.contract(task_id=f"new-{task_id}")
                self.refuse(fresh, task_id, message)

    def test_a_second_successor_is_refused(self):
        first, _ = self.first()
        self.run_version(self.next_contract(first), [step("finish")], amends="t1")
        self.refuse(self.next_contract(first, task_id="fork"), "t1", "already amended")

    def test_two_connections_amend_atomically_before_any_worker_runs(self):
        first, _ = self.first()
        old_events = self.ledger.events("t1")
        barrier = Barrier(2)
        contracts = [self.next_contract(first, task_id=tid) for tid in ("left", "right")]

        def submit(contract):
            worker = ScriptedModel([step("finish")], "worker")
            reviewer = ScriptedModel([review("GO")], "reviewer")
            with Ledger(self.ledger.path) as ledger:
                append = ledger.append

                def synchronized_append(tid, kind, *args, **kwargs):
                    if kind == "task.submitted":
                        barrier.wait(timeout=10)  # both have passed preflight and copied the candidate
                    return append(tid, kind, *args, **kwargs)

                kernel = Kernel(ledger, worker, reviewer)
                with patch.object(ledger, "append", side_effect=synchronized_append), \
                        patch.object(kernel, "_work", wraps=kernel._work) as work:
                    try:
                        outcome = kernel.run(contract, amends="t1")
                    except KernelError as error:
                        outcome = error
                    return contract, outcome, len(worker.calls), len(reviewer.calls), work.call_count

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, contract) for contract in contracts]
            results = [future.result(timeout=30) for future in futures]
        refused = [row for row in results if isinstance(row[1], KernelError)]
        self.assertEqual(len(refused), 1, [(c.task_id, type(r).__name__, w, a) for c, r, w, a, _ in results])
        rejected, error, worker_calls, reviewer_calls, work_calls = refused[0]
        self.assertRegex(str(error), "already amended by another successor: t1")
        self.assertEqual((worker_calls, reviewer_calls, work_calls), (0, 0, 0))
        self.assertEqual(self.ledger.events(rejected.task_id), [])
        winner, report, worker_calls, reviewer_calls, work_calls = next(
            row for row in results if not isinstance(row[1], KernelError))
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertEqual((worker_calls, reviewer_calls, work_calls), (1, 1, 1))
        self.assertEqual(adjudicate(self.ledger, winner.task_id).verdict, "PASS")
        self.assertEqual((report.delivered_to / "calc.py").read_text(encoding="utf-8"), GOOD_CODE)
        self.assertEqual(self.ledger.events("t1"), old_events)
        self.refuse(self.next_contract(first, task_id="fork"), "t1", "already amended")
        retry = self.next_contract(winner, task_id="retry")
        result = self.run_version(retry, [step("finish")], amends=winner.task_id)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(self.ledger.verify_chain(), (True, None))

    def test_changed_removed_or_added_workspace_files_are_refused(self):
        for mutation in ("changed", "removed", "added"):
            with self.subTest(mutation=mutation):
                first, _ = self.first(task_id=mutation)
                if mutation == "removed":
                    (first.workspace / "calc.py").unlink()
                else:
                    (first.workspace / ("calc.py" if mutation == "changed" else "new.txt")).write_bytes(b"changed")
                second = self.next_contract(first, task_id=f"next-{mutation}")
                self.refuse(second, first.task_id, "earlier version's workspace no longer matches its last frozen candidate")
                self.assertFalse(second.workspace.exists())

    def test_new_workspace_must_be_empty_before_copy_with_or_without_seed(self):
        first, _ = self.first()
        for seeded in (False, True):
            with self.subTest(seeded=seeded):
                second = self.next_contract(first, task_id=f"occupied-{seeded}")
                second.workspace.mkdir()
                owner = second.workspace / "owner.txt"
                owner.write_bytes(b"keep")
                if seeded:
                    second = replace(second, seed=first.workspace)
                self.refuse(second, "t1", "empty")
                self.assertEqual(list(second.workspace.iterdir()), [owner])
                self.assertEqual(owner.read_bytes(), b"keep")

    def test_the_last_repaired_candidate_is_the_continuation_point(self):
        first = replace(self.contract(), max_repairs=1)
        self.run_version(first, [step("write_file", path="calc.py", content=BAD_CODE), step("finish"),
                                 step("write_file", path="calc.py", content=GOOD_CODE), step("finish")],
                         decisions=("NO_GO", "GO"))
        frozen = self.ledger.events("t1", "candidate.frozen")
        self.assertEqual(len(frozen), 2)
        second = self.next_contract(first)
        result = self.run_version(second, [step("finish")], amends="t1")
        self.assertEqual(self.submission("t2")["amends"]["from_candidate"], frozen[-1].payload["digest"])
        self.assertEqual(result.candidate.digest, frozen[-1].payload["digest"])
        self.assertNotEqual(result.candidate.digest, frozen[0].payload["digest"])

    def test_an_old_pass_does_not_replace_a_new_review(self):
        first, old = self.first()
        result = self.run_version(self.next_contract(first), [step("finish")], decisions=("NO_GO",), amends="t1")
        self.assertEqual(result.candidate, old.candidate)
        self.assertEqual(result.verdict, Verdict.FAIL)
        self.assertIsNone(result.delivered_to)
        self.assertEqual(len(self.ledger.events("t2", "test.result")), 1)
        self.assertEqual(len(self.reviewer.calls), 1)
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "PASS")

    def test_external_agent_receives_the_same_version_briefing(self):
        first, _ = self.first(BAD_CODE)
        second = self.next_contract(first, goal="Fix addition.")
        result = Kernel(self.ledger, self.agent("good"), ScriptedModel([review("GO")], "reviewer")).run(second, amends="t1")
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        prompt = self.seen("good", "prompt.txt")
        for fact in ("version 2", "amends t1", "goal", "FAIL", "Remaining budget"):
            self.assertIn(fact, prompt)
        self.assertNotIn("PRIVATE-THOUGHT", prompt)
        self.assertNotIn("PRIVATE-SUMMARY", prompt)

    def test_briefing_caps_recorded_reasons_and_excludes_the_old_raw_review(self):
        first = self.contract()
        answer = review("NO_GO", "recorded reason")

        def raw_review(messages):
            return json.dumps(dict(json.loads(answer(messages)), private="PRIVATE-RAW-REVIEW"))

        old = Kernel(self.ledger, ScriptedModel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")]),
                     ScriptedModel([raw_review])).run(first)
        self.ledger.append("t1", "verdict", {"verdict": "FAIL", "candidate": old.candidate.digest,
                                            "reasons": ["r" * 1500 + "OMITTED-REASON-TAIL"]})
        result = self.run_version(self.next_contract(first), [step("finish")], amends="t1")
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        prompt = self.worker.calls[0][1]["content"]
        self.assertIn("r" * 1500, prompt)
        self.assertNotIn("OMITTED-REASON-TAIL", prompt)
        self.assertNotIn("PRIVATE-RAW-REVIEW", prompt)

    def test_cli_amends_end_to_end_and_reports_refusals_without_tracebacks(self):
        first, _ = self.first(BAD_CODE)
        config = self.root / "m2k.toml"
        config.write_text('[sandbox]\nbackend = "none"\n[models.worker]\nmodel = "worker"\n'
                          '[models.reviewer]\nmodel = "reviewer"\n', encoding="utf-8")
        task = self.root / "v2.toml"
        task.write_text('[task]\nid = "t2"\ngoal = "Fix addition"\nworkspace = "ws/t2"\ndeliver_to = "out"\n'
                        '[acceptance]\ncommand = "{python} check.py"\nfiles = ["check.py"]\n', encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        args = ["--config", str(config), "--ledger", str(self.ledger.path), "run", "--untrusted", "--amends", "t1", str(task)]
        models = [ScriptedModel([step("write_file", path="calc.py", content=GOOD_CODE), step("finish")], "worker"),
                  ScriptedModel([review("GO")], "reviewer")]
        with patch("m2k.cli.build_model", side_effect=models), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(args), 0)
        self.assertIn("t2: PASS", out.getvalue())
        self.assertEqual(self.submission("t2")["amends"]["task"], first.task_id)
        self.assertTrue((self.root / "out" / "t2" / "calc.py").is_file())
        task.write_text(task.read_text(encoding="utf-8").replace('id = "t2"', 'id = "t3"').replace('ws/t2', 'ws/t3'), encoding="utf-8")
        with patch("m2k.cli.build_model", side_effect=[ScriptedModel([], "worker"), ScriptedModel([], "reviewer")]), \
                redirect_stdout(out), redirect_stderr(err), self.assertRaisesRegex(SystemExit, "already amended"):
            main(args)
        self.assertEqual(self.ledger.events("t3"), [])
        self.assertNotIn("Traceback", err.getvalue())


class AmendTreeTest(RecallCase):
    def test_version_briefing_and_review_line_reach_new_subtasks(self):
        first = self.tree(reassign=0)
        old = self.kernel(good_a() + good_b(), [review("GO")] * 3).run(first)
        self.assertEqual(old.reported_verdict, Verdict.PASS, old.reasons)
        second = replace(first, task_id="p2", workspace=first.workspace.parent / "p2",
                         goal="build a and b carefully", budget=Budget(max_rounds=30, max_model_calls=40, max_commands=12))
        kernel = self.kernel(good_a() + good_b(), [review("GO")] * 3)
        result = kernel.run(second, amends="p1", approval=owner_approval(self.ledger, "p1", second))
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertIn("version 2", kernel.worker_model.calls[0][1]["content"])
        for messages in kernel.reviewer_model.calls:
            self.assertIn("Contract version 2; the owner changed: budget, goal.", messages[0]["content"])

    def test_prior_usage_includes_subtasks_reassigned_epochs_and_nested_descendants(self):
        first = self.tree()
        old = self.kernel(wrong_a() + good_a() + good_b(), [review("GO")] * 4).run(first)
        self.assertEqual(old.reported_verdict, Verdict.PASS, old.reasons)
        self.ledger.append("nested", "task.submitted", {"parent": "p1.a.e2"})
        self.ledger.append("nested", "model.called", {"role": "worker", "input_tokens": None, "output_tokens": None})
        self.ledger.append("p1.unrelated", "task.submitted", {"parent": "elsewhere"})
        self.ledger.append("p1.unrelated", "model.called", {"role": "worker", "input_tokens": 100, "output_tokens": 100})
        ids = ("p1", "p1.a", "p1.a.e2", "p1.b", "nested")
        prior = {key: sum(self.ledger.usage(tid)[key] for tid in ids) for key in self.ledger.usage("p1")}
        second = replace(first, task_id="p2", workspace=first.workspace.parent / "p2", subtasks=())
        worker = ScriptedModel([step("finish")], "worker")
        result = Kernel(self.ledger, worker, ScriptedModel([review("GO")], "reviewer")).run(second, amends="p1")
        submitted = self.ledger.last("p2", "task.submitted").payload
        self.assertEqual(submitted["amends"]["prior_usage"], prior)
        self.assertEqual(submitted["contract"]["budget"]["max_model_calls"], first.budget.max_model_calls - prior["model_calls"])
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertIn("version 2", worker.calls[0][1]["content"])
        for tid in ids[1:-1]:
            self.assertNotIn("contract", self.ledger.last(tid, "task.submitted").payload)
