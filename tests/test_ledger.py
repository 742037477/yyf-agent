import sqlite3
import tempfile
import unittest
from pathlib import Path

from m2k.ledger import Ledger, LedgerError


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "l.sqlite"
        self.l = Ledger(self.path)

    def tearDown(self):
        self.l.close()
        self._tmp.cleanup()

    def test_append_and_read_back(self):
        self.l.append("a", "x", {"n": 1})
        self.l.append("b", "y", {"n": 2})
        self.l.append("a", "z", {"中文": "ok"})
        self.assertEqual([e.kind for e in self.l.events("a")], ["x", "z"])
        self.assertEqual(self.l.events("a")[1].payload, {"中文": "ok"})
        self.assertEqual(self.l.task_ids(), ["a", "b"])

    def test_append_guard_runs_inside_transaction_before_writing(self):
        before = self.l.events()
        checked = []

        def guard():
            self.assertTrue(self.l._db.in_transaction)
            self.assertEqual(self.l.events(), before)
            checked.append(True)
            return None

        event = self.l.append("a", "task.submitted", {"n": 1}, guard=guard)
        self.assertEqual(checked, [True])
        self.assertFalse(self.l._db.in_transaction)
        self.assertEqual(self.l.events(), [event])
        self.assertEqual(self.l.verify_chain(), (True, None))

    def test_rejected_guard_rolls_back_without_events_and_releases_the_lock(self):
        first = self.l.append("old", "task.submitted")
        queries = []
        self.l._db.set_trace_callback(queries.append)
        with self.assertRaisesRegex(LedgerError, "already claimed"):
            self.l.append("rejected", "task.submitted", guard=lambda: "already claimed")
        self.assertEqual(queries, ["BEGIN IMMEDIATE", "ROLLBACK"])
        self.assertFalse(self.l._db.in_transaction)
        self.assertEqual(self.l.events(), [first])
        self.assertEqual(self.l.events("rejected"), [])
        with Ledger(self.path) as other:
            second = other.append("retry", "task.submitted", guard=lambda: None)
        self.assertEqual(self.l.events(), [first, second])
        self.assertEqual(second.prev_hash, first.hash)
        self.assertEqual(self.l.verify_chain(), (True, None))

    def test_guard_exception_rolls_back_and_keeps_the_connection_usable(self):
        def guard():
            raise ValueError("guard failed")

        with self.assertRaisesRegex(ValueError, "guard failed"):
            self.l.append("rejected", "task.submitted", guard=guard)
        self.assertFalse(self.l._db.in_transaction)
        self.assertEqual(self.l.events(), [])
        event = self.l.append("retry", "task.submitted")
        self.assertEqual(self.l.events(), [event])

    def test_event_reads_one_sequence_with_the_same_conversion_as_events(self):
        first = self.l.append("a", "x", {"中文": [1, {"nested": True}]}, op_id="one")
        second = self.l.append("b", "y", {"n": None})
        expected = self.l.events()
        queries = []
        self.l._db.set_trace_callback(queries.append)
        for event in (first, second):
            with self.subTest(seq=event.seq):
                queries.clear()
                self.assertEqual(self.l.event(event.seq), expected[event.seq - 1])
                self.assertEqual(len(queries), 1)
                self.assertIn(f"WHERE seq = {event.seq}", queries[0])
                self.assertNotIn("ORDER BY", queries[0])

    def test_event_returns_none_for_a_missing_sequence(self):
        self.assertIsNone(self.l.event(1))
        self.l.append("a", "x")
        self.assertIsNone(self.l.event(0))
        self.assertIsNone(self.l.event(999999))

    def test_chain_verifies_and_detects_tampering(self):
        for i in range(5):
            self.l.append("a", "e", {"i": i})
        self.assertEqual(self.l.verify_chain(), (True, None))
        db = sqlite3.connect(self.path)
        db.execute("""UPDATE events SET payload='{"i":99}' WHERE seq=3""")
        db.commit()
        db.close()
        self.assertEqual(self.l.verify_chain(), (False, 3))

    def test_deleting_an_event_breaks_the_chain(self):
        for i in range(3):
            self.l.append("a", "e", {"i": i})
        db = sqlite3.connect(self.path)
        db.execute("DELETE FROM events WHERE seq=2")
        db.commit()
        db.close()
        self.assertEqual(self.l.verify_chain(), (False, 3))

    def test_operation_id_is_single_use(self):
        self.l.append("a", "effect.intent", {}, op_id="r1")
        with self.assertRaises(LedgerError):
            self.l.append("a", "effect.intent", {}, op_id="r1")
        self.l.append("a", "effect.result", {}, op_id="r1")   # different kind is fine
        self.l.append("b", "effect.intent", {}, op_id="r1")   # different task is fine

    def test_usage_is_derived_and_unknown_tokens_are_flagged(self):
        self.l.append("a", "model.called", {"role": "worker", "input_tokens": 5, "output_tokens": 7})
        self.l.append("a", "model.called", {"role": "reviewer", "input_tokens": None, "output_tokens": None})
        self.l.append("a", "effect.intent", {"tool": "run_command"}, op_id="r1")
        self.l.append("a", "effect.intent", {"tool": "write_file"}, op_id="r2")
        u = self.l.usage("a")
        self.assertEqual(u, {"rounds": 1, "model_calls": 2, "tokens": 12, "tokens_unknown_calls": 1, "commands": 1})

    def test_reopen_keeps_history(self):
        self.l.append("a", "x", {})
        self.l.close()
        self.l = Ledger(self.path)
        self.l.append("a", "y", {})
        self.assertEqual(len(self.l.events("a")), 2)
        self.assertTrue(self.l.verify_chain()[0])

    def test_ledger_id_is_first_hash_stays_fixed_and_survives_reopen(self):
        self.assertIsNone(self.l.ledger_id)
        first = self.l.append("a", "x", {})
        self.assertEqual(self.l.ledger_id, first.hash)
        self.l.append("b", "y", {})
        self.assertEqual(self.l.ledger_id, first.hash)
        self.l.close()
        self.l = Ledger(self.path)
        self.assertEqual(self.l.ledger_id, first.hash)

    def test_ledger_id_rechecks_empty_then_caches_one_bounded_read(self):
        self.assertIsNone(self.l.ledger_id)
        with Ledger(self.path) as other:
            first = other.append("other", "x", {})
        queries = []
        self.l._db.set_trace_callback(queries.append)
        self.assertEqual(self.l.ledger_id, first.hash)
        self.assertEqual(len(queries), 1)
        self.assertIn("ORDER BY seq LIMIT 1", queries[0])
        queries.clear()
        self.assertEqual(self.l.ledger_id, first.hash)
        self.assertEqual(self.l.ledger_id, first.hash)
        self.assertEqual(queries, [])

    def test_ledger_id_is_read_only(self):
        with self.assertRaises(AttributeError):
            self.l.ledger_id = "replacement"


if __name__ == "__main__":
    unittest.main()
