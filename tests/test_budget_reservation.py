"""T23: atomic budget reservations and release across the subtask lifecycle."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from m2k.kernel import Kernel, KernelError
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.types import Verdict, WorkerStatus
from tests.helpers import KernelCase, review, step
from tests.test_task_tree import TreeCase, good_a, good_b


class ReservationTest(KernelCase):
    def called(self, tid, role="worker", tokens=0):
        self.ledger.append(tid, "model.called", {
            "role": role, "input_tokens": tokens, "output_tokens": 0})

    def test_within_limits_records_one_reservation(self):
        amounts = {"rounds": 2, "model_calls": 3, "tokens": 40, "commands": 1}
        self.assertTrue(self.ledger.reserve("p", "p.a", amounts, amounts))
        events = self.ledger.events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].task_id, events[0].kind, events[0].payload),
                         ("p", "budget.reserved", {"child": "p.a", "amounts": amounts}))
        self.assertEqual(self.ledger.verify_chain(), (True, None))

    def test_over_limits_records_only_refusal_and_shortfall(self):
        amounts = {"model_calls": 6, "commands": 3, "rounds": 2}
        limits = {"model_calls": 4, "commands": 1, "rounds": 2}
        self.assertFalse(self.ledger.reserve("p", "p.a", amounts, limits))
        events = self.ledger.events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].kind, events[0].payload),
                         ("budget.refused", {"child": "p.a", "amounts": amounts,
                                             "short": {"model_calls": 2, "commands": 2}}))
        self.assertEqual(self.ledger.verify_chain(), (True, None))

    def test_none_limit_is_ignored(self):
        self.called("p", tokens=100)
        self.assertTrue(self.ledger.reserve("p", "p.a", {"tokens": 1000000, "model_calls": 2},
                                           {"tokens": None, "model_calls": 3}))

    def test_parent_usage_counts_for_each_resource(self):
        self.called("p", tokens=7)
        self.called("p", role="reviewer", tokens=3)
        self.ledger.append("p", "effect.intent", {"tool": "run_command"}, op_id="cmd")
        amounts = {"rounds": 2, "model_calls": 3, "tokens": 4, "commands": 2}
        limits = {"rounds": 2, "model_calls": 4, "tokens": 12, "commands": 2}
        self.assertFalse(self.ledger.reserve("p", "p.a", amounts, limits))
        self.assertEqual(self.ledger.last("p", "budget.refused").payload["short"],
                         {"rounds": 1, "model_calls": 1, "tokens": 2, "commands": 1})

    def test_duplicate_reservation_and_release_record_nothing_new(self):
        self.ledger.release("p", "p.a")
        self.assertEqual(self.ledger.events(), [])
        self.assertTrue(self.ledger.reserve("p", "p.a", {"model_calls": 5}, {"model_calls": 5}))
        self.assertFalse(self.ledger.reserve("p", "p.a", {"model_calls": 1}, {"model_calls": 5}))
        self.assertEqual(len(self.ledger.events()), 1)
        self.ledger.release("p", "p.a")
        self.ledger.release("p", "p.a")
        self.assertFalse(self.ledger.reserve("p", "p.a", {"model_calls": 1}, {"model_calls": 5}))
        self.assertEqual([e.kind for e in self.ledger.events()], ["budget.reserved", "budget.released"])

    def test_active_reservation_counts_in_full_then_release_counts_usage(self):
        limits = {"model_calls": 10}
        self.assertTrue(self.ledger.reserve("p", "p.a", {"model_calls": 5}, limits))
        for _ in range(2):
            self.called("p.a")
        self.assertFalse(self.ledger.reserve("p", "p.b", {"model_calls": 6}, limits))
        self.assertEqual(self.ledger.last("p", "budget.refused").payload["short"], {"model_calls": 1})
        self.ledger.release("p", "p.a")
        self.assertTrue(self.ledger.reserve("p", "p.b", {"model_calls": 8}, limits))

    def test_released_usage_counts_for_all_resources(self):
        amounts = {"rounds": 4, "model_calls": 5, "tokens": 100, "commands": 3}
        self.assertTrue(self.ledger.reserve("p", "p.a", amounts, amounts))
        self.called("p.a", tokens=10)
        self.called("p.a", role="reviewer", tokens=10)
        self.ledger.append("p.a", "effect.intent", {"tool": "run_command"}, op_id="cmd")
        self.ledger.release("p", "p.a")
        remainder = {"rounds": 3, "model_calls": 3, "tokens": 80, "commands": 2}
        self.assertTrue(self.ledger.reserve("p", "p.b", remainder, amounts))
        self.assertFalse(self.ledger.reserve("p", "p.c", {"model_calls": 1}, amounts))
        self.assertEqual(self.ledger.last("p", "budget.refused").payload["short"], {"model_calls": 1})

    def test_other_parents_do_not_consume_this_pool(self):
        self.assertTrue(self.ledger.reserve("other", "other.a", {"model_calls": 5}, {"model_calls": 5}))
        self.called("unrelated")
        self.ledger.release("p", "other.a")
        self.assertEqual(self.ledger.events("p"), [])
        self.assertTrue(self.ledger.reserve("p", "p.a", {"model_calls": 5}, {"model_calls": 5}))

    def test_two_connections_race_for_last_share_twenty_rounds(self):
        def reserve(parent, child, barrier):
            with Ledger(self.ledger.path) as ledger:
                barrier.wait(timeout=10)
                return ledger.reserve(parent, child, {"model_calls": 3}, {"model_calls": 5})

        with ThreadPoolExecutor(max_workers=2) as pool:
            for i in range(20):
                parent, barrier = f"p{i}", Barrier(2)
                futures = [pool.submit(reserve, parent, f"{parent}.{sid}", barrier) for sid in ("a", "b")]
                results = [f.result(timeout=15) for f in futures]
                with self.subTest(round=i):
                    self.assertEqual(sorted(results), [False, True])
                    self.assertEqual([e.kind for e in self.ledger.events(parent)],
                                     ["budget.reserved", "budget.refused"])
                    self.assertEqual(self.ledger.last(parent, "budget.refused").payload["short"],
                                     {"model_calls": 1})
        self.assertEqual(self.ledger.verify_chain(), (True, None))


class KernelReservationTest(TreeCase):
    def test_two_subtasks_reserve_submit_release_in_order(self):
        contract = self.parent()
        _, report = self.run_tree(contract, good_a() + good_b(), [review("GO")] * 3)
        self.assertEqual(report.verdict, Verdict.PASS, report.reasons)
        events = [(e.kind, e.payload["child"] if e.kind.startswith("budget.") else e.task_id)
                  for e in self.ledger.events()
                  if e.kind.startswith("budget.") or (e.kind == "task.submitted" and e.task_id != "p1")]
        self.assertEqual(events, [("budget.reserved", "p1.a"), ("task.submitted", "p1.a"),
                                  ("budget.released", "p1.a"), ("budget.reserved", "p1.b"),
                                  ("task.submitted", "p1.b"), ("budget.released", "p1.b")])
        self.assertEqual(self.ledger.events("p1", "budget.reserved")[0].payload["amounts"],
                         {"rounds": 4, "model_calls": 5, "tokens": 0, "commands": 2})
        self.assertEqual(self.ledger.verify_chain(), (True, None))

    def test_refusal_fails_parent_without_copying_or_starting_child(self):
        contract = self.parent()
        kernel = Kernel(self.ledger, ScriptedModel(good_a()), ScriptedModel([review("GO")]))

        def refuse(parent_id, child_id, amounts, limits):
            self.assertEqual(limits, {"rounds": 10, "model_calls": 12, "tokens": None, "commands": 4})
            self.ledger.append(parent_id, "budget.refused", {
                "child": child_id, "amounts": amounts, "short": {"model_calls": 2}})
            return False

        with patch.object(Ledger, "reserve", side_effect=refuse):
            report = kernel.run(contract)
        self.assertEqual(report.verdict, Verdict.FAIL)
        self.assertEqual(report.worker.status, WorkerStatus.SUBTASK_FAILED)
        self.assertEqual(report.worker.reason, "subtask a: budget reservation refused ({'model_calls': 2})")
        self.assertEqual(self.ledger.events("p1.a"), [])
        self.assertEqual(self.ledger.events("p1", "budget.released"), [])
        self.assertFalse(kernel._subtask_dir(contract.workspace, contract.subtasks[0]).exists())
        self.assertEqual(kernel.worker_model.calls, [])

    def test_patched_refusal_without_event_still_fails_parent(self):
        with patch.object(Ledger, "reserve", return_value=False):
            _, report = self.run_tree(self.parent(), [], [])
        self.assertEqual(report.verdict, Verdict.FAIL)
        self.assertEqual(report.worker.reason, "subtask a: budget reservation refused ({})")
        self.assertEqual(self.ledger.events("p1.a"), [])

    def test_child_kernel_error_is_released(self):
        contract = self.parent()
        kernel = Kernel(self.ledger, ScriptedModel([]), ScriptedModel([]))
        run = kernel.run

        def fail_child(child, _parent=None):
            if _parent is not None:
                raise KernelError("child failed before submit")
            return run(child)

        with patch.object(kernel, "run", side_effect=fail_child):
            report = kernel.run(contract)
        self.assertEqual(report.verdict, Verdict.FAIL)
        self.assertEqual(report.worker.reason, "subtask a: child failed before submit")
        self.assertEqual([e.payload for e in self.ledger.events("p1", "budget.released")], [{"child": "p1.a"}])
        self.assertEqual(self.ledger.events("p1.a"), [])

    def test_fail_and_unknown_children_are_released(self):
        cases = [("fail", [step("write_file", path="a.py", content="A = 5\n"), step("finish")],
                  [review("GO")], Verdict.FAIL),
                 ("unknown", good_a(), ["not json"], Verdict.UNKNOWN)]
        for tid, steps, reviews, verdict in cases:
            with self.subTest(verdict=verdict):
                _, report = self.run_tree(self.parent(tid=tid), steps, reviews)
                self.assertEqual(report.verdict, verdict)
                self.assertEqual([e.payload for e in self.ledger.events(tid, "budget.released")],
                                 [{"child": f"{tid}.a"}])
                self.assertEqual(self.ledger.events(f"{tid}.b"), [])


if __name__ == "__main__":
    unittest.main()
