"""T49: reuse only an unaffected prefix, from ledger facts fixed at submission."""
import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from threading import Barrier
from unittest.mock import patch

from m2k.adjudicate import adjudicate, amendment_impact
from m2k.cli import main
from m2k.contract import Budget
from m2k.kernel import Kernel, KernelError, status
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.types import Verdict
from tests.helpers import owner_approval, review, step
from tests.test_obs import WitnessFiles
from tests.test_recall import RecallCase, good_a, good_b, wrong_a
from tests.test_task_tree import CHECK_ALL


class ReuseCase(RecallCase):
    def first_version(self, tid="p1", steps=None, reassign=0, **changes):
        first = replace(self.tree(tid=tid, reassign=reassign), **changes)
        result = self.kernel(good_a() + good_b() if steps is None else steps, [review("GO")] * 4).run(first)
        return first, result

    def next_version(self, first, tid="p2", **changes):
        return replace(first, task_id=tid, workspace=first.workspace.parent / tid, seed=None,
                       budget=Budget(max_rounds=30, max_model_calls=40, max_commands=12), **changes)

    def overlay(self, contract, files):
        seed = self.root / (contract.task_id + "-seed")
        seed.mkdir()
        for rel, text in files.items():
            path = seed / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return replace(contract, seed=seed)

    def submission(self, tid="p2"):
        return self.ledger.last(tid, "task.submitted").payload

    def impact(self, tid="p2"):
        return self.submission(tid)["amends"]["impact"]

    def amend(self, contract, previous="p1", steps=(), reviews=1):
        kernel = self.kernel(list(steps), [review("GO")] * reviews)
        return kernel, kernel.run(contract, amends=previous, approval=owner_approval(self.ledger, previous, contract))


class ReuseTest(ReuseCase):
    def test_parent_acceptance_overlay_reuses_both_and_delivers_after_fresh_checks(self):
        first, old = self.first_version()
        self.assertEqual(old.reported_verdict, Verdict.PASS, old.reasons)
        before = self.ledger.events()
        second = self.next_version(first)
        second = replace(second, acceptance=replace(second.acceptance, command="{python} check.py",
                                                   files=second.acceptance.files + ("check.py",)))
        second = self.overlay(second, {"check.py": CHECK_ALL + "print('new parent acceptance')\n"})
        kernel, result = self.amend(second)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(self.impact(), {"reused": {"a": "p1.a", "b": "p1.b"}, "rerun": {}})
        reused = self.ledger.events("p2", "subtask.reused")
        self.assertEqual([e.payload for e in reused], [
            {"id": sid, "task_id": "p1." + sid,
             "candidate": self.ledger.last("p1." + sid, "candidate.frozen").payload["digest"]}
            for sid in ("a", "b")])
        self.assertFalse(kernel.worker_model.calls)
        self.assertEqual(len(kernel.reviewer_model.calls), 1)
        for kind in ("budget.reserved", "subtask.started", "effect.intent", "credential.issued"):
            self.assertFalse(self.ledger.events("p2", kind), kind)
        self.assertFalse((second.workspace.parent / "p2.subtasks").exists())
        self.assertEqual(kernel.child_credentials, {})
        self.assertEqual(result.worker.status.value, "FINISHED")
        self.assertIn("new parent acceptance", result.test.output)
        self.assertEqual(len(self.ledger.events("p2", "test.result")), 1)
        self.assertEqual(result.usage["model_calls"], 1)
        self.assertEqual((result.delivered_to / "a.py").read_text(encoding="utf-8"), "A = 1\n")
        self.assertEqual(adjudicate(self.ledger, "p2").verdict, "PASS")
        self.assertEqual([e for e in self.ledger.events() if e.task_id != "p2"], before)
        self.assertEqual(status(self.ledger, "p2")["reused"], ["a", "b"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--ledger", str(self.ledger.path), "status", "p2"]), 0)
        self.assertEqual(json.loads(out.getvalue())["reused"], ["a", "b"])

    def test_changed_subtask_reruns_it_and_the_following_suffix(self):
        for sid in ("a", "b"):
            with self.subTest(sid=sid):
                tid = "old-" + sid
                first, _ = self.first_version(tid)
                subs = tuple(replace(s, goal=s.goal + " carefully") if s.id == sid else s for s in first.subtasks)
                second = self.next_version(first, "new-" + sid, subtasks=subs)
                steps = good_b() if sid == "b" else good_a() + good_b()
                _, result = self.amend(second, tid, steps, reviews=2 if sid == "b" else 3)
                self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
                expected = ({"reused": {"a": tid + ".a"}, "rerun": {"b": "subtask changed"}} if sid == "b" else
                            {"reused": {}, "rerun": {"a": "subtask changed", "b": "an earlier subtask reruns"}})
                self.assertEqual(self.impact(second.task_id), expected)
                self.assertEqual([e.payload["id"] for e in self.ledger.events(second.task_id, "subtask.started")],
                                 ["b"] if sid == "b" else ["a", "b"])

    def test_budget_and_reassignment_changes_alone_reuse(self):
        first, _ = self.first_version()
        subs = tuple(replace(s, budget=replace(s.budget, max_rounds=9), max_reassign=2) for s in first.subtasks)
        _, result = self.amend(self.next_version(first, subtasks=subs))
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(self.impact()["reused"], {"a": "p1.a", "b": "p1.b"})

    def test_parent_and_owner_file_changes_have_the_first_applicable_reason(self):
        cases = (("goal", {}, "parent changed: goal"),
                 ("constraints", {}, "parent changed: constraints"),
                 ("protected", {}, "parent changed: protected"),
                 ("timeout", {}, "parent changed: acceptance.timeout_s"),
                 ("acceptance", {"check_a.py": "assert True\n"}, "acceptance file changed"),
                 ("scope", {"a.py": "A = 9\n"}, "files in its write scope changed"),
                 ("scope-added", {"pkg/data.txt": "new\n"}, "files in its write scope changed"),
                 ("owner", {"data.txt": "new\n"}, "owner files changed: data.txt"))
        for name, overlay, reason in cases:
            with self.subTest(name=name):
                tid = "old-" + name
                first = self.tree(tid=tid, reassign=0)
                if name == "scope-added":
                    first = replace(first, subtasks=(replace(first.subtasks[0], writes=("a.py", "pkg/")), first.subtasks[1]))
                self.kernel(good_a() + good_b(), [review("GO")] * 3).run(first)
                second = self.next_version(first, "new-" + name)
                if name == "goal":
                    second = replace(second, goal="a revised parent goal")
                elif name == "constraints":
                    second = replace(second, constraints=("keep the same results",))
                elif name == "protected":
                    second = replace(second, protected=("check_all.py",))
                elif name == "timeout":
                    second = replace(second, acceptance=replace(second.acceptance, timeout_s=61))
                if overlay:
                    second = self.overlay(second, overlay)
                _, result = self.amend(second, tid, good_a() + good_b(), reviews=3)
                self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
                self.assertEqual(self.impact(second.task_id)["rerun"], {"a": reason, "b": "an earlier subtask reruns"})

    def test_a_merged_prefix_survives_a_later_subtask_failure(self):
        first, old = self.first_version(steps=good_a() + [step("write_file", path="b.py", content="B = 9\n"), step("finish")])
        self.assertEqual(old.verdict, Verdict.FAIL)
        _, result = self.amend(self.next_version(first), steps=good_b(), reviews=2)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(self.impact(), {"reused": {"a": "p1.a"}, "rerun": {"b": "not merged in the earlier version"}})

    def test_a_child_pass_without_a_completed_merge_cannot_be_reused(self):
        first = self.tree(reassign=0)
        kernel = self.kernel(good_a(), [review("GO")])
        with patch.object(kernel, "_merge", return_value="merge failed"):
            old = kernel.run(first)
        self.assertEqual(old.verdict, Verdict.FAIL)
        self.assertEqual(adjudicate(self.ledger, "p1.a").verdict, "PASS")
        _, result = self.amend(self.next_version(first), steps=good_a() + good_b(), reviews=3)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(self.impact()["rerun"],
                         {"a": "not merged in the earlier version", "b": "an earlier subtask reruns"})

    def test_strict_owner_file_reason_is_limited_to_ten_paths(self):
        first, old = self.first_version()
        start = dict(old.candidate.files)
        start.update({f"owner-{i:02}.txt": "0" * 64 for i in range(12)})
        impact = amendment_impact(self.ledger, first.task_id, self.submission("p1")["contract"], start)
        reason = impact["rerun"]["a"]
        self.assertTrue(reason.startswith("owner files changed: "))
        self.assertEqual(len(reason.split(": ", 1)[1].split(", ")), 10)
        self.assertNotIn("owner-10.txt", reason)

    def test_reusing_every_child_still_requires_new_parent_acceptance_and_review(self):
        for failure in ("acceptance", "review"):
            with self.subTest(failure=failure):
                tid = "old-" + failure
                first, _ = self.first_version(tid)
                second = self.next_version(first, "new-" + failure)
                if failure == "acceptance":
                    second = self.overlay(second, {"check_all.py": "assert False, 'new acceptance failed'\n"})
                kernel = self.kernel([], [review("NO_GO" if failure == "review" else "GO")])
                result = kernel.run(second, amends=tid, approval=owner_approval(self.ledger, tid, second))
                self.assertEqual(result.reported_verdict, Verdict.FAIL)
                self.assertIsNone(result.delivered_to)
                self.assertEqual(set(self.impact(second.task_id)["reused"]), {"a", "b"})
                self.assertFalse(kernel.worker_model.calls)
                self.assertEqual(len(kernel.reviewer_model.calls), 1)

    def test_reassigned_source_and_three_versions_stay_on_the_child_that_ran(self):
        first, old = self.first_version(steps=wrong_a() + good_a() + good_b(), reassign=1)
        self.assertEqual(old.reported_verdict, Verdict.PASS, old.reasons)
        second = self.next_version(first)
        _, result = self.amend(second)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(self.impact()["reused"], {"a": "p1.a.e2", "b": "p1.b"})
        _, result = self.amend(self.next_version(second, "p3"), "p2")
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(self.impact("p3")["reused"], {"a": "p1.a.e2", "b": "p1.b"})
        prior = self.submission("p3")["amends"]["prior_usage"]
        self.assertEqual(prior["model_calls"], 11)

    def test_source_adjudication_failure_reruns_without_writing_onto_the_source(self):
        first, _ = self.first_version()
        self.ledger.append("p1.a", "outside.changed", {"changed": ["owner-config"]})
        before = self.ledger.events("p1.a")
        _, result = self.amend(self.next_version(first), steps=good_a() + good_b(), reviews=3)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual(self.impact()["rerun"]["a"], "earlier result does not adjudicate PASS")
        self.assertEqual(self.ledger.events("p1.a"), before)

    def test_model_planned_predecessor_and_predecessor_without_subtasks_cannot_reuse(self):
        for planned in (False, True):
            with self.subTest(planned=planned):
                tid = "planned" if planned else "single"
                first = self.tree(tid=tid, reassign=0)
                explicit = first.subtasks
                first = replace(first, subtasks=(), plan="model" if planned else "")
                steps = good_a()[:-1] + good_b()
                if planned:
                    parts = tuple((s.acceptance.files[0], s.acceptance.command) for s in explicit)
                    first = replace(first, acceptance=replace(first.acceptance, parts=parts))
                    proposal = {"subtasks": [{"id": s.id, "goal": s.goal, "writes": list(s.writes),
                                              "acceptance_files": list(s.acceptance.files),
                                              "budget": {"max_rounds": 4, "max_model_calls": 5, "max_commands": 2}}
                                             for s in explicit]}
                    steps = [json.dumps(proposal)] + good_a() + good_b()
                old = self.kernel(steps, [review("GO")] * 3).run(first)
                self.assertEqual(old.reported_verdict, Verdict.PASS, old.reasons)
                second = self.next_version(first, tid + "-new", subtasks=explicit, plan="")
                _, result = self.amend(second, tid, good_a() + good_b(), reviews=3)
                self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
                self.assertEqual(self.impact(second.task_id)["reused"], {})
                reasons = self.impact(second.task_id)["rerun"]
                self.assertIn("planned by a model" if planned else "owner-written subtasks", reasons["a"])

    def test_amendment_without_subtasks_keeps_the_t48_payload(self):
        first, _ = self.first_version()
        second = self.next_version(first, subtasks=())
        _, result = self.amend(second, steps=[step("finish")])
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        for key in ("impact", "start_files"):
            self.assertNotIn(key, self.submission()["amends"])
        for key in ("impact", "start_files", "amends"):
            self.assertNotIn(key, self.submission("p1"))

    def test_budget_counts_only_rerun_subtasks_but_checks_every_subtask_rule(self):
        first, _ = self.first_version()
        subs = (first.subtasks[0], replace(first.subtasks[1], goal="write b carefully"))
        second = replace(self.next_version(first, subtasks=subs),
                         budget=Budget(max_rounds=8, max_model_calls=13, max_commands=2))
        for tid, change in (("short", {"budget": replace(second.budget, max_model_calls=11)}),
                            ("invalid", {"subtasks": (replace(subs[0], max_reassign=4), subs[1])}),
                            ("invalid-type", {"subtasks": (replace(subs[0], max_reassign="bad"), subs[1])}),
                            ("invalid-budget", {"subtasks": (replace(subs[0], budget=replace(subs[0].budget, max_rounds=None)), subs[1])}),
                            ("duplicate", {"subtasks": (subs[0], subs[0])})):
            with self.subTest(tid=tid):
                refused = replace(second, task_id=tid, workspace=second.workspace.parent / tid, **change)
                before = self.ledger.events()
                with self.assertRaises(KernelError):
                    self.amend(refused, steps=good_b(), reviews=2)
                self.assertEqual(self.ledger.events(), before)
        _, result = self.amend(second, steps=good_b(), reviews=2)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual([e.payload["child"] for e in self.ledger.events("p2", "budget.reserved")], ["p2.b"])
        self.assertEqual(self.submission()["contract"]["budget"]["max_model_calls"], 6)

    def test_cli_amendment_defers_budget_sums_but_keeps_the_owner_budget(self):
        first, _ = self.first_version()
        config = self.root / "m2k.toml"
        config.write_text('[sandbox]\nbackend = "none"\n[models.worker]\nmodel = "worker"\n'
                          '[models.reviewer]\nmodel = "reviewer"\n', encoding="utf-8")
        task = self.root / "p2.toml"
        task.write_text('[task]\nid = "p2"\ngoal = "build a and b"\nworkspace = "ws/p2"\ndeliver_to = "out"\n'
                        '[acceptance]\ncommand = "{python} check_all.py"\ntimeout_s = 60\n'
                        'files = ["check_a.py", "check_b.py", "check_all.py"]\n'
                        '[budget]\nmax_rounds = 5\nmax_model_calls = 8\nmax_commands = 1\n'
                        '[[subtasks]]\nid = "a"\ngoal = "write a.py"\nwrites = ["a.py"]\n'
                        'acceptance = "{python} check_a.py"\nacceptance_files = ["check_a.py"]\n'
                        'budget = {max_rounds = 6, max_model_calls = 8, max_commands = 2}\n'
                        '[[subtasks]]\nid = "b"\ngoal = "write b.py"\nwrites = ["b.py"]\n'
                        'acceptance = "{python} check_b.py"\nacceptance_files = ["check_b.py"]\n'
                        'budget = {max_rounds = 4, max_model_calls = 5, max_commands = 2}\n', encoding="utf-8")
        worker, reviewer = ScriptedModel([], "worker"), ScriptedModel([review("GO")], "reviewer")
        args = ["--config", str(config), "--ledger", str(self.ledger.path), "run", "--untrusted", "--amends", first.task_id, str(task)]
        with patch("m2k.cli.build_model", side_effect=[worker, reviewer]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 0)
        self.assertEqual(self.submission()["amends"]["owner_budget"],
                         {"max_rounds": 5, "max_model_calls": 8, "max_commands": 1, "max_tokens": None})
        self.assertEqual(self.impact()["reused"], {"a": "p1.a", "b": "p1.b"})
        self.assertFalse(worker.calls)
        self.assertEqual(adjudicate(self.ledger, "p2").verdict, "PASS")

    def test_reuse_events_follow_contract_order_even_when_it_is_not_alphabetical(self):
        first = self.tree(reassign=0)
        first = replace(first, subtasks=tuple(reversed(first.subtasks)))
        result = self.kernel(good_b() + good_a(), [review("GO")] * 3).run(first)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        _, result = self.amend(self.next_version(first))
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual([e.payload["id"] for e in self.ledger.events("p2", "subtask.reused")], ["b", "a"])
        self.assertEqual(adjudicate(self.ledger, "p2").verdict, "PASS")

    def test_old_child_workspace_changed_after_decision_does_not_affect_reuse(self):
        first, _ = self.first_version()
        second = self.next_version(first)
        before = self.ledger.events()
        with Ledger(self.ledger.path) as other:
            append = other.append

            def change_child_after_decision(tid, kind, *args, **kwargs):
                if tid == "p2" and kind == "task.submitted":
                    self.assertEqual(args[0]["amends"]["impact"]["reused"], {"a": "p1.a", "b": "p1.b"})
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        child_file = first.workspace.parent / "p1.subtasks" / "a" / "a.py"
                        pool.submit(child_file.write_text, "A = 999\n", encoding="utf-8").result(timeout=5)
                return append(tid, kind, *args, **kwargs)

            worker, reviewer = ScriptedModel([], "w"), ScriptedModel([review("GO")], "r")
            with patch.object(other, "append", side_effect=change_child_after_decision):
                result = Kernel(other, worker, reviewer).run(second, amends="p1", approval=owner_approval(other, "p1", second))
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertEqual((second.workspace / "a.py").read_text(encoding="utf-8"), "A = 1\n")
        self.assertEqual(adjudicate(self.ledger, "p2").verdict, "PASS")
        self.assertFalse(worker.calls)
        self.assertEqual([e for e in self.ledger.events() if e.task_id != "p2"], before)

    def test_two_connections_reusing_the_same_version_leave_no_loser_events(self):
        first, _ = self.first_version()
        before = self.ledger.events()
        barrier = Barrier(2)

        def submit(tid):
            contract = self.next_version(first, tid)
            worker, reviewer = ScriptedModel([], "w"), ScriptedModel([review("GO")], "r")
            with Ledger(self.ledger.path) as ledger:
                append = ledger.append

                def synchronized_append(task_id, kind, *args, **kwargs):
                    if kind == "task.submitted":
                        barrier.wait(timeout=10)
                    return append(task_id, kind, *args, **kwargs)

                with patch.object(ledger, "append", side_effect=synchronized_append):
                    try:
                        result = Kernel(ledger, worker, reviewer).run(contract, amends="p1", approval=owner_approval(ledger, "p1", contract))
                    except KernelError as error:
                        result = error
                return tid, result, worker.calls, reviewer.calls

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, tid) for tid in ("left", "right")]
            results = [f.result(timeout=30) for f in futures]
        losers = [row for row in results if isinstance(row[1], KernelError)]
        self.assertEqual(len(losers), 1)
        loser, error, work, reviews = losers[0]
        self.assertIn("already amended", str(error))
        self.assertEqual((work, reviews), ([], []))
        self.assertEqual(self.ledger.events(loser), [])
        winner, report, work, reviews = next(row for row in results if not isinstance(row[1], KernelError))
        self.assertEqual(report.reported_verdict, Verdict.PASS, report.reasons)
        self.assertEqual((len(work), len(reviews)), (0, 1))
        self.assertEqual(len(self.ledger.events(winner, "subtask.reused")), 2)
        self.assertEqual(adjudicate(self.ledger, winner).verdict, "PASS")
        self.assertEqual([e for e in self.ledger.events() if e.task_id not in {winner, loser}], before)
        self.assertEqual(self.ledger.verify_chain(), (True, None))


class InheritedReuseTest(WitnessFiles, ReuseCase):
    def reused_version(self):
        first, result = self.first_version()
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        second = self.next_version(first)
        _, result = self.amend(second)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        return second

    def assert_reruns(self, previous, tid="p3", ledger=None, observer=None):
        ledger = ledger or self.ledger
        contract = self.next_version(previous, tid)
        kernel = Kernel(ledger, ScriptedModel(good_a() + good_b(), "w"),
                        ScriptedModel([review("GO")] * 3, "r"), obs=observer)
        before = ledger.events()
        result = kernel.run(contract, amends=previous.task_id, approval=owner_approval(ledger, previous.task_id, contract))
        impact = ledger.last(tid, "task.submitted").payload["amends"]["impact"]
        self.assertEqual(impact, {"reused": {}, "rerun": {
            "a": "not merged in the earlier version", "b": "an earlier subtask reruns"}})
        self.assertEqual(len(kernel.worker_model.calls), 4)
        self.assertEqual([e.payload["id"] for e in ledger.events(tid, "subtask.started")], ["a", "b"])
        self.assertFalse(ledger.events(tid, "subtask.reused"))
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        self.assertIsNotNone(result.delivered_to)
        checks = {"obs_public_key": observer.public_key_b64, "obs_log": observer.log_path} if observer else {}
        self.assertEqual(adjudicate(ledger, tid, **checks).verdict, "PASS")
        self.assertEqual([e for e in ledger.events() if e.task_id not in {tid, tid + ".a", tid + ".b"}], before)

    def forged_chain(self, depth=1, observer=None):
        first = self.tree(reassign=0)
        result = Kernel(self.ledger, ScriptedModel(good_a() + good_b(), "w"),
                        ScriptedModel([review("GO")] * 3, "r"), obs=observer).run(first)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        previous = first
        for version in range(2, depth + 2):
            current = self.next_version(previous, f"p{version}")
            forged = {"reused": {"a": "p1.b", "b": "p1.b"}, "rerun": {}}
            kernel = Kernel(self.ledger, ScriptedModel([], "w"), ScriptedModel([review("GO")], "r"), obs=observer)
            with patch("m2k.kernel.amendment_impact", return_value=forged):
                result = kernel.run(current, amends=previous.task_id, approval=owner_approval(self.ledger, previous.task_id, current))
            self.assertNotEqual(result.reported_verdict, Verdict.PASS)
            previous = current
        return previous

    def test_another_subtasks_pass_is_not_an_inherited_result(self):
        self.assert_reruns(self.forged_chain())

    def test_forged_inheritance_is_rejected_with_obs(self):
        observer, _, _, _ = self.client()
        self.assert_reruns(self.forged_chain(observer=observer), observer=observer)

    def test_a_forgery_in_an_intermediate_version_is_resolved_recursively(self):
        self.assert_reruns(self.forged_chain(depth=2), tid="p4")

    def test_recorded_impact_must_agree_with_the_inherited_event(self):
        second = self.reused_version()
        forged = deepcopy(self.submission())
        forged["amends"]["impact"]["reused"]["a"] = "p1.b"
        self.ledger.append("p2", "task.submitted", forged)
        self.assert_reruns(second)

    def test_even_identical_duplicate_inherited_events_are_rejected(self):
        second = self.reused_version()
        event = self.ledger.events("p2", "subtask.reused")[0]
        self.ledger.append("p2", "subtask.reused", event.payload)
        self.assert_reruns(second)

    def test_origin_requires_the_exact_parent_start_finish_and_verified_merge(self):
        first, result = self.first_version()
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        for name in ("parent", "started-id", "started-source", "finished-source", "candidate", "status", "verified", "op"):
            with self.subTest(name=name), Ledger(self.root / (name + ".sqlite")) as ledger:
                for e in self.ledger.events():
                    payload, op = deepcopy(e.payload), e.op_id
                    if name == "parent" and e.task_id == "p1.a" and e.kind == "task.submitted":
                        payload["parent"] = "another-version"
                    if e.task_id == "p1":
                        if e.kind == "subtask.started" and payload["id"] == "a":
                            if name == "started-id":
                                payload["id"] = "b"
                            elif name == "started-source":
                                payload["task_id"] = "p1.b"
                        if name == "finished-source" and e.kind == "subtask.finished" and payload["id"] == "a":
                            payload["task_id"] = "p1.b"
                        if op == "merge-a":
                            if name == "op":
                                op = "different-op"
                            if name == "candidate" and e.kind == "effect.intent":
                                payload["args"]["candidate"] = "0" * 64
                            if e.kind == "effect.result":
                                if name == "status":
                                    payload["status"] = "FAILED"
                                elif name == "verified":
                                    payload["detail"]["verified"] = False
                    ledger.append(e.task_id, e.kind, payload, op_id=op)
                self.assert_reruns(first, tid="p2-" + name, ledger=ledger)

    def test_rules_b_and_c_compare_with_where_the_source_ran(self):
        self.reused_version()
        original = deepcopy(self.submission())
        origin = self.submission("p1")["contract"]
        start = self.ledger.last("p2", "candidate.frozen").payload["files"]
        for name in ("subtask", "goal", "constraints", "protected", "acceptance.timeout_s"):
            with self.subTest(name=name):
                middle = deepcopy(original)
                contract = middle["contract"]
                if name == "subtask":
                    contract["subtasks"][0]["goal"] += " differently"
                elif name == "acceptance.timeout_s":
                    contract["acceptance"]["timeout_s"] += 1
                elif name == "goal":
                    contract["goal"] += " differently"
                else:
                    contract[name] = ["check_all.py"]
                self.ledger.append("p2", "task.submitted", middle)
                # The current contract matches O: an intervening contract cannot replace O as the comparison.
                self.assertEqual(amendment_impact(self.ledger, "p2", origin, start),
                                 {"reused": {"a": "p1.a", "b": "p1.b"}, "rerun": {}})
                # Matching only the intervening contract cannot reuse a result judged under O's different fields.
                reason = "subtask changed" if name == "subtask" else "parent changed: " + name
                self.assertEqual(amendment_impact(self.ledger, "p2", contract, start),
                                 {"reused": {}, "rerun": {"a": reason, "b": "an earlier subtask reruns"}})

    def test_an_inheritance_cycle_cannot_resolve_a_source(self):
        self.reused_version()
        forged = deepcopy(self.submission())
        forged["amends"]["task"] = "p2"
        self.ledger.append("p2", "task.submitted", forged)
        impact = amendment_impact(self.ledger, "p2", forged["contract"], forged["amends"]["start_files"])
        self.assertEqual(impact, {"reused": {}, "rerun": {
            "a": "not merged in the earlier version", "b": "an earlier subtask reruns"}})

    def test_adjudicator_rechecks_inheritance_of_a_finished_successor(self):
        second = self.reused_version()
        _, result = self.amend(self.next_version(second, "p3"), "p2")
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        event = self.ledger.events("p2", "subtask.reused")[0]
        self.ledger.append("p2", "subtask.reused", event.payload)
        before = self.ledger.events()
        result = adjudicate(self.ledger, "p3")
        self.assertNotEqual(result.verdict, "PASS")
        self.assertIn("amends.impact differs from the ledger", result.reasons)
        self.assertEqual(self.ledger.events(), before)
