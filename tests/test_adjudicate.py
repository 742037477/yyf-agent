"""T39: the Gate delivers only what the read-only adjudicator recomputes as PASS from the ledger alone."""
import io
import sqlite3
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from m2k import trust
from m2k.adjudicate import adjudicate
from m2k.cli import main
from m2k.contract import Budget
from m2k.kernel import Kernel
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.types import Verdict
from tests.helpers import BAD_CODE, GOOD_CODE, KernelCase, owner_approval, review, step
from tests.test_amend import AmendCase
from tests.test_obs import WitnessFiles
from tests.test_reuse import ReuseCase
from tests.test_task_tree import TreeCase, good_a, good_b

WRITE_GOOD = [step("write_file", path="calc.py", content=GOOD_CODE), step("finish")]
CAPS = {"tools": ["list_files", "read_file", "write_file", "edit_file", "run_command"], "max_rounds": 100,
        "max_model_calls": 100, "max_tokens": 10_000_000, "max_commands": 100, "network": False}


class AdjudicateTest(KernelCase):
    def test_workspace_observations_do_not_change_the_read_only_verdict(self):
        self.run_task(WRITE_GOOD, [review("GO")])
        self.ledger.append("t1", "workspace.changed", {"added": ["calc.py"], "changed": [], "removed": [], "files": 2})
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "PASS")

    def test_any_outside_change_blocks_even_a_forged_pass_tail(self):
        path = str(self.root / "owner-config")
        for tid, payload in (("changed", {"added": [], "changed": [path], "removed": []}),
                             ("unverified", {"unverified": [path]})):
            with self.subTest(tid=tid):
                self.run_task(WRITE_GOOD, [review("GO")], task_id=tid)
                test = self.ledger.last(tid, "test.result").payload
                verdict = self.ledger.last(tid, "verdict").payload
                self.ledger.append(tid, "outside.changed", payload)
                self.ledger.append(tid, "outside.changed", {"added": [], "changed": [], "removed": []})
                self.ledger.append(tid, "test.result", test)
                self.ledger.append(tid, "verdict", verdict)
                result = adjudicate(self.ledger, tid)
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn(repr(path), " ".join(result.reasons))

    def test_a_pass_is_recomputed_before_the_gate_delivers(self):
        _, r = self.run_task(WRITE_GOOD, [review("GO")])
        self.assertEqual(r.reported_verdict, Verdict.PASS)
        self.assertIsNotNone(r.delivered_to)
        adj = adjudicate(self.ledger, "t1")
        self.assertEqual((adj.verdict, adj.reasons), ("PASS", []))
        gate = self.ledger.last("t1", "gate.adjudication").payload
        self.assertEqual((gate["verdict"], gate["candidate"]), ("PASS", r.candidate.digest))

    def test_descendant_changes_block_a_pass_tail_via_parent_links(self):
        for depth, key in ((1, "changed"), (2, "unverified")):
            with self.subTest(depth=depth):
                tid, first, retry, nested = (f"{name}-{depth}" for name in ("parent", "attempt", "retry", "nested"))
                path = str(self.root / f"owner-config-{depth}")
                self.run_task(WRITE_GOOD, [review("GO")], task_id=tid)
                test = self.ledger.last(tid, "test.result").payload
                verdict = self.ledger.last(tid, "verdict").payload
                for child, parent in ((first, tid), (retry, tid), (nested, first)):
                    self.ledger.append(child, "task.submitted", {"parent": parent})
                self.ledger.append(first if depth == 1 else nested, "outside.changed", {key: [path]})
                self.ledger.append(tid, "test.result", test)
                self.ledger.append(tid, "verdict", verdict)
                result = adjudicate(self.ledger, tid)
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn(repr(path), " ".join(result.reasons))

    def test_an_unrelated_tasks_outside_change_does_not_block_pass(self):
        self.run_task(WRITE_GOOD, [review("GO")])
        self.ledger.append("t1.unrelated", "task.submitted", {"parent": "another-root"})
        self.ledger.append("t1.unrelated", "outside.changed", {"unverified": [str(self.root / "owner-config")]})
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "PASS")

    def test_a_lying_kernel_is_not_delivered(self):
        with patch("m2k.kernel.combine", return_value=Verdict.PASS):   # the kernel claims PASS over a failed test
            _, r = self.run_task([step("write_file", path="calc.py", content=BAD_CODE), step("finish")], [review("GO")])
        self.assertEqual(r.verdict, Verdict.PASS)
        self.assertEqual(r.reported_verdict, Verdict.UNKNOWN)
        self.assertIsNone(r.delivered_to)
        self.assertEqual(self.ledger.events("t1", "delivery.intent"), [])
        self.assertIn("adjudication: acceptance FAIL", self.ledger.last("t1", "delivery.refused").payload["reason"])

    def test_every_top_level_verdict_is_adjudicated_without_a_delivery_target(self):
        cases = (("PASS", WRITE_GOOD, review("GO")),
                 ("FAIL", [step("finish")], review("GO")),
                 ("UNKNOWN", WRITE_GOOD, "unparseable review"))
        for verdict, steps, answer in cases:
            with self.subTest(verdict=verdict):
                _, report = self.run_task(steps, [answer], task_id=verdict.lower(), deliver=False)
                self.assertEqual(report.verdict.value, verdict)
                self.assertEqual(report.reported_verdict.value, verdict)
                gates = self.ledger.events(report.task_id, "gate.adjudication")
                self.assertEqual(len(gates), 1)
                self.assertEqual(gates[0].payload["verdict"], verdict)
                self.assertEqual(len(self.ledger.events(report.task_id, "verdict")), 1)
                self.assertEqual(self.ledger.events(report.task_id, "delivery.intent"), [])

    def test_a_pass_appended_afterwards_does_not_change_the_facts(self):
        self.run_task([step("write_file", path="calc.py", content=BAD_CODE), step("finish")], [review("GO")])
        cand = self.ledger.last("t1", "candidate.frozen").payload["digest"]
        self.ledger.append("t1", "verdict", {"verdict": "PASS", "reasons": [], "candidate": cand})
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "FAIL")

    def test_an_edited_ledger_fails_and_a_garbled_review_is_unknown(self):
        self.run_task(WRITE_GOOD, [review("GO")])
        db = sqlite3.connect(self.ledger.path)
        db.execute("UPDATE events SET payload = replace(payload, 'GO', 'GO ') WHERE kind = 'audit.result'")
        db.commit()
        db.close()
        self.assertIn("ledger hash chain broken", adjudicate(self.ledger, "t1").reasons[0])
        _, r = self.run_task(WRITE_GOOD, ["not json at all"], task_id="t2")
        self.assertEqual((r.verdict, adjudicate(self.ledger, "t2", check_chain=False).verdict), (Verdict.UNKNOWN, "UNKNOWN"))

    def test_a_credential_that_does_not_cover_the_contract_fails(self):
        setup, _ = trust.dev_setup(self.root / "keys", CAPS)
        c = self.contract(budget=Budget(max_rounds=10, max_model_calls=10, max_tokens=100000, max_commands=10))
        Kernel(self.ledger, ScriptedModel(WRITE_GOOD, "w"), ScriptedModel([review("GO")], "r"), trust=setup).run(c)
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "PASS")
        cred = self.ledger.last("t1", "credential.issued").payload["credential"]
        forged = dict(cred, body=dict(cred["body"], contract_sha256="0" * 64))
        self.ledger.append("t1", "credential.issued", {"credential": forged})
        self.assertIn("credential does not cover", " ".join(adjudicate(self.ledger, "t1").reasons))

    def test_cli_with_a_pin_rejects_a_trusted_run_without_a_release_record(self):
        setup, root = trust.dev_setup(self.root / "keys", CAPS)
        contract = self.contract(budget=Budget(max_rounds=10, max_model_calls=10, max_tokens=100000, max_commands=10))
        result = Kernel(self.ledger, ScriptedModel(WRITE_GOOD, "w"), ScriptedModel([review("GO")], "r"), trust=setup).run(contract)
        self.assertEqual(result.reported_verdict, Verdict.PASS)
        self.assertIsNotNone(result.delivered_to)
        pin = self.root / "root-pin.json"
        pin.write_bytes(trust.canonical({"trust_domain": "TEST_ONLY", "public_key_b64": trust.public_key_b64(root)}))
        config = self.root / "m2k.toml"
        config.write_text('[trust]\ndomain = "TEST_ONLY"\nroot_pin = "root-pin.json"\n', encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--config", str(config), "--ledger", str(self.ledger.path), "adjudicate", "t1"])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("no release manifest recorded (run predates T64 or bypassed the CLI)", out.getvalue())
        self.assertEqual(adjudicate(self.ledger, "t1").verdict, "PASS")

    def test_cli_exit_code(self):
        self.run_task(WRITE_GOOD, [review("GO")])
        self.run_task([step("finish")], [review("GO")], task_id="t2")
        for tid, code, verdict in (("t1", 0, '"verdict": "PASS"'), ("t2", 1, '"verdict": "FAIL"')):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--ledger", str(self.ledger.path), "adjudicate", tid]), code)
            self.assertIn(verdict, out.getvalue())


class AdjudicateTreeTest(TreeCase):
    def test_every_merged_subtask_must_adjudicate_pass_too(self):
        _, r = self.run_tree(self.parent(), good_a() + good_b(), [review("GO")] * 3)
        self.assertEqual(r.verdict.value, "PASS", r.reasons)
        self.assertEqual(adjudicate(self.ledger, "p1").verdict, "PASS")
        child = self.ledger.last("p1.a", "candidate.frozen").payload["digest"]
        self.ledger.append("p1.a", "test.result", {"verdict": "FAIL", "exit_code": 1, "reason": "", "output": "",
                                                   "candidate": child})
        adj = adjudicate(self.ledger, "p1")
        self.assertEqual(adj.verdict, "FAIL")
        self.assertIn("subtask a does not adjudicate PASS", adj.reasons[0])


class AdjudicateAmendTest(AmendCase):
    def versions(self):
        first, _ = self.first()
        self.run_version(self.next_contract(first), [step("finish")], amends="t1")
        return deepcopy(self.submission("t2"))

    def test_an_honest_new_version_adjudicates_like_the_original_without_writes(self):
        self.versions()
        before = self.ledger.events()
        for tid in ("t1", "t2"):
            result = adjudicate(self.ledger, tid)
            self.assertEqual((result.verdict, result.reasons), ("PASS", []))
            self.assertEqual(result.candidate, self.ledger.last(tid, "candidate.frozen").payload["digest"])
        self.assertEqual(self.ledger.events(), before)

    def test_forged_usage_version_and_candidate_are_not_pass(self):
        original = self.versions()
        cases = {"prior_usage": dict(original["amends"]["prior_usage"], model_calls=0),
                 "version": 9, "from_candidate": "0" * 64}
        for field, value in cases.items():
            with self.subTest(field=field):
                forged = deepcopy(original)
                forged["amends"][field] = value
                self.ledger.append("t2", "task.submitted", forged)
                result = adjudicate(self.ledger, "t2")
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn(field, " ".join(result.reasons))
                self.ledger.append("t2", "task.submitted", original)

    def test_another_successor_blocks_both_branches(self):
        original = self.versions()
        for event in self.ledger.events("t2"):
            self.ledger.append("fork", event.kind, event.payload, op_id=event.op_id)
        for tid in ("t2", "fork"):
            with self.subTest(tid=tid):
                result = adjudicate(self.ledger, tid)
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn("already amended", " ".join(result.reasons))
        self.assertEqual(self.submission("fork")["amends"], original["amends"])

    def test_missing_child_legacy_and_unfinished_predecessors_are_not_pass(self):
        original = self.versions()
        for tid, payload, message in (("missing", None, "missing"),
                                      ("child", {"parent": "parent", "contract": {}}, "parent"),
                                      ("legacy", {}, "contract"),
                                      ("unfinished", {"contract": {}}, "verdict")):
            with self.subTest(tid=tid):
                if payload is not None:
                    self.ledger.append(tid, "task.submitted", payload)
                    if tid != "unfinished":
                        self.ledger.append(tid, "verdict", {"verdict": "FAIL"})
                forged = deepcopy(original)
                forged["amends"]["task"] = tid
                self.ledger.append("t2", "task.submitted", forged)
                result = adjudicate(self.ledger, "t2")
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn(message, " ".join(result.reasons))


class AdjudicateOwnerTest(WitnessFiles, AmendCase):
    def approved(self, observer=None, raise_budget=True, waiting=False):
        first = self.contract()
        tail = step("ask_owner", kind="question", text="需要确认") if waiting else step("finish")
        self.run_version(first, [step("write_file", path="calc.py", content=GOOD_CODE), tail], obs=observer)
        second = self.next_contract(first, budget=replace(first.budget, max_model_calls=40) if raise_budget else first.budget)
        result = self.run_version(second, [step("finish")], amends="t1", obs=observer,
                                  approval=owner_approval(self.ledger, "t1", second))
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        return self.ledger.events()

    def test_honest_budget_and_request_approvals_adjudicate_pass_without_writes(self):
        self.approved()
        before = self.ledger.events()
        result = adjudicate(self.ledger, "t2")
        self.assertEqual((result.verdict, result.reasons), ("PASS", []))
        self.assertEqual(self.ledger.events(), before)

    def test_missing_forged_needs_and_wrong_approver_are_not_pass(self):
        original = self.approved(raise_budget=False, waiting=True)
        for case in ("missing", "needs", "by"):
            with self.subTest(case=case), Ledger(self.root / (case + ".sqlite")) as ledger:
                for e in original:
                    data = deepcopy(e.payload)
                    if e.task_id == "t2" and e.kind == "task.submitted":
                        if case == "missing":
                            del data["amends"]["approval"]
                        else:
                            data["amends"]["approval"][case] = {} if case == "needs" else "worker"
                    ledger.append(e.task_id, e.kind, data, op_id=e.op_id)
                before = ledger.events()
                result = adjudicate(ledger, "t2")
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn("amends.approval", " ".join(result.reasons))
                self.assertEqual(ledger.events(), before)

    def test_budget_raise_missing_extra_wrong_fields_and_unneeded_raise_are_not_pass(self):
        original = self.approved()
        raised = self.ledger.last("t2", "budget.raised")
        for case in ("missing", "extra", "fields", "unneeded"):
            with self.subTest(case=case), Ledger(self.root / (case + ".sqlite")) as ledger:
                for e in original:
                    data = deepcopy(e.payload)
                    if e == raised:
                        if case == "missing":
                            continue
                        if case == "fields":
                            data["fields"] = {"max_model_calls": [30, 41]}
                        if case == "extra":
                            ledger.append(e.task_id, e.kind, data)
                    if case == "unneeded" and e.task_id == "t2" and e.kind == "task.submitted":
                        data["amends"]["owner_budget"]["max_model_calls"] = 30
                    ledger.append(e.task_id, e.kind, data, op_id=e.op_id)
                result = adjudicate(ledger, "t2")
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn("budget.raised", " ".join(result.reasons))

    def test_budget_receipt_is_required_and_bound_to_the_raised_event(self):
        observer, key, public, log = self.client()
        original = self.approved(observer)
        self.assertEqual(adjudicate(self.ledger, "t2", obs_public_key=public, obs_log=log).verdict, "PASS")
        raised = self.ledger.last("t2", "budget.raised")
        for case in ("missing", "invalid", "other-event"):
            with self.subTest(case=case), Ledger(self.root / (case + ".sqlite")) as ledger:
                for e in original:
                    data = deepcopy(e.payload)
                    if (e.kind == "obs.receipt" and e.task_id == "t2"
                            and data["receipt"]["body"]["ledger_seq"] == raised.seq):
                        if case == "missing":
                            continue
                        if case == "invalid":
                            data["receipt"]["signature_b64"] = "invalid"
                        else:
                            data["receipt"] = trust.sign(dict(data["receipt"]["body"], kind="task.submitted"), key)
                    ledger.append(e.task_id, e.kind, data, op_id=e.op_id)
                result = adjudicate(ledger, "t2", obs_public_key=public)
                self.assertNotEqual(result.verdict, "PASS")
                self.assertIn("no valid OBS receipt for budget.raised", " ".join(result.reasons))


class AdjudicateReuseTest(ReuseCase):
    def versions(self, change_b=False):
        first, _ = self.first_version()
        second = self.next_version(first)
        if change_b:
            second = replace(second, subtasks=(second.subtasks[0], replace(second.subtasks[1], goal="revised b")))
        _, result = self.amend(second, steps=good_b() if change_b else (), reviews=2 if change_b else 1)
        self.assertEqual(result.reported_verdict, Verdict.PASS, result.reasons)
        return deepcopy(self.submission())

    def assert_problem(self, ledger, text):
        before = ledger.events()
        result = adjudicate(ledger, "p2")
        self.assertNotEqual(result.verdict, "PASS")
        self.assertIn(text, " ".join(result.reasons))
        self.assertEqual(ledger.events(), before)

    def test_an_honest_reuse_is_pass_and_adjudication_writes_nothing(self):
        self.versions()
        before = self.ledger.events()
        result = adjudicate(self.ledger, "p2")
        self.assertEqual((result.verdict, result.reasons), ("PASS", []))
        self.assertEqual(self.ledger.events(), before)

    def test_forged_impact_moving_a_changed_subtask_into_reused_is_not_pass(self):
        forged = self.versions(change_b=True)
        del forged["amends"]["impact"]["rerun"]["b"]
        forged["amends"]["impact"]["reused"]["b"] = "p1.b"
        self.ledger.append("p2", "task.submitted", forged)
        self.assert_problem(self.ledger, "amends.impact")

    def test_forged_start_files_missing_impact_and_wrong_sources_are_not_pass(self):
        original = self.versions()
        for name in ("start_files", "impact", "source", "candidate"):
            with self.subTest(name=name):
                forged = deepcopy(original)
                if name == "start_files":
                    forged["amends"]["start_files"]["a.py"] = "0" * 64
                elif name == "impact":
                    del forged["amends"]["impact"]
                elif name == "source":
                    forged["amends"]["impact"]["reused"]["a"] = "p1.b"
                else:
                    forged["amends"]["impact"]["reused"]["a"] = "missing-child"
                self.ledger.append("p2", "task.submitted", forged)
                self.assert_problem(self.ledger, "amends." + ("start_files" if name == "start_files" else "impact"))
                self.ledger.append("p2", "task.submitted", original)

    def test_missing_extra_out_of_order_and_wrong_reuse_events_are_not_pass(self):
        self.versions()
        original = self.ledger.events()
        positions = [i for i, e in enumerate(original) if e.task_id == "p2" and e.kind == "subtask.reused"]
        for name in ("missing", "extra", "order", "source", "candidate"):
            with self.subTest(name=name), Ledger(self.root / (name + ".sqlite")) as ledger:
                events = list(original)
                if name == "missing":
                    events.pop(positions[0])
                elif name == "extra":
                    events.insert(positions[0], events[positions[0]])
                elif name == "order":
                    a, b = positions
                    events[a], events[b] = events[b], events[a]
                for e in events:
                    payload = deepcopy(e.payload)
                    if e == original[positions[0]]:
                        if name == "source":
                            payload["task_id"] = "p1.b"
                        elif name == "candidate":
                            payload["candidate"] = "0" * 64
                    ledger.append(e.task_id, e.kind, payload, op_id=e.op_id)
                self.assert_problem(ledger, "subtask.reused")

    def test_reused_subtasks_may_not_also_start_or_merge(self):
        self.versions()
        original = self.ledger.events()
        for kind in ("subtask.started", "effect.intent", "effect.result"):
            with self.subTest(kind=kind), Ledger(self.root / (kind + ".sqlite")) as ledger:
                for e in original:
                    ledger.append(e.task_id, e.kind, e.payload, op_id=e.op_id)
                if kind == "subtask.started":
                    ledger.append("p2", kind, {"id": "a", "task_id": "p2.a", "epoch": 1})
                elif kind == "effect.intent":
                    ledger.append("p2", kind, {"tool": "merge_subtask", "args": {"subtask": "a"}}, op_id="merge-a")
                else:
                    ledger.append("p2", kind, {"tool": "merge_subtask", "status": "COMPLETED",
                                               "detail": {"verified": True}}, op_id="merge-a")
                self.assert_problem(ledger, "subtask.reused id also started or merged")
