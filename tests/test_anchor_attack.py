"""Attack drill: rewrite an event and recompute the chain; only the external anchors notice."""
import json
import tempfile
import unittest
from pathlib import Path

from m2k import anchor
from m2k.contract import Acceptance, Budget, TaskContract
from m2k.kernel import Kernel
from m2k.ledger import GENESIS, Ledger, _canonical, _event_hash
from m2k.models import ScriptedModel
from m2k.verify import scripted_review

GOOD = "def add(a, b):\n    return a + b\n"
CHECK = "from calc import add\nassert add(2, 3) == 5, 'add is wrong'\nprint('ok')\n"


def _s(tool, **args):
    return json.dumps({"thought": "", "tool": tool, "args": args})


class AnchorAttackTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ledger = self.enterContext(Ledger(self.root / "ledger.sqlite"))
        self.anchors = self.root / "anchors.jsonl"
        self.run_task()
        anchor.append(self.anchors, self.ledger, "drill")

    def run_task(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        (workspace / "check.py").write_text(CHECK, encoding="utf-8")
        contract = TaskContract("t1", "Write calc.py with add(a, b).", workspace,
                                Acceptance("{python} check.py", 60, files=("check.py",)),
                                Budget(max_rounds=12, max_model_calls=15, max_commands=6),
                                deliver_to=self.root / "out")
        Kernel(self.ledger,
               ScriptedModel([_s("write_file", path="calc.py", content=GOOD), _s("finish", summary="done")]),
               ScriptedModel([scripted_review("GO", "correct")])).run(contract)

    def rewrite(self, seq, mutate):
        """Edit one event's payload, then recompute its hash and every later hash."""
        db = self.ledger._db
        payload = json.loads(db.execute("SELECT payload FROM events WHERE seq = ?", (seq,)).fetchone()[0])
        db.execute("UPDATE events SET payload = ? WHERE seq = ?", (_canonical(mutate(payload)), seq))
        prev = GENESIS
        rows = db.execute("SELECT seq, task_id, kind, op_id, payload, ts FROM events ORDER BY seq").fetchall()
        for number, task_id, kind, op_id, payload, ts in rows:
            h = _event_hash(prev, task_id, kind, op_id, payload, ts)
            db.execute("UPDATE events SET prev_hash = ?, hash = ? WHERE seq = ?", (prev, h, number))
            prev = h

    def test_rewrite_and_recompute_hides_the_change_from_the_chain(self):
        anchored = anchor.head(self.ledger)["seq"]
        verdict = self.ledger.last("t1", "verdict")
        self.assertLess(verdict.seq, anchored)
        self.rewrite(verdict.seq, lambda p: {**p, "reasons": ["forged: nothing to report"]})
        self.assertEqual(self.ledger.verify_chain(), (True, None))
        self.assertEqual(anchor.check(self.anchors, self.ledger),
                         [f"ledger rewritten at anchored seq {anchored}"])

    def test_truncating_the_anchored_tail_keeps_the_chain_consistent(self):
        anchored = anchor.head(self.ledger)["seq"]
        self.ledger._db.execute("DELETE FROM events WHERE seq >= ?", (anchored,))
        self.assertEqual(self.ledger.verify_chain(), (True, None))
        self.assertEqual(anchor.check(self.anchors, self.ledger),
                         [f"ledger truncated at anchored seq {anchored}"])

    def test_untouched_ledger_matches_the_anchor(self):
        self.assertEqual(self.ledger.verify_chain(), (True, None))
        self.assertEqual(anchor.check(self.anchors, self.ledger), [])


if __name__ == "__main__":
    unittest.main()
