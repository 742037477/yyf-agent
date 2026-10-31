import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from m2k.contract import Acceptance, Budget, TaskContract
from m2k.adjudicate import approval_needs
from m2k.kernel import Kernel
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.verify import scripted_review


def step(tool, thought="", **args):
    return json.dumps({"thought": thought, "tool": tool, "args": args})


def review(decision, *reasons):
    return scripted_review(decision, *reasons)


def owner_approval(ledger, task_id, contract, text=None):
    return {"by": "m2k answer", "text": text,
            "needs": approval_needs(ledger, task_id, json.loads(json.dumps(asdict(contract), default=str)))}


GOOD_CODE = "def add(a, b):\n    return a + b\n"
BAD_CODE = "def add(a, b):\n    return a - b\n"
CHECK = "from calc import add\nassert add(2, 3) == 5, 'add is wrong'\nprint('ok')\n"


class KernelCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = Ledger(self.root / "ledger.sqlite")

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def contract(self, task_id="t1", budget=None, deliver=True, command="{python} check.py", timeout_s=60,
                 protected=(), check=CHECK):
        ws = self.root / "ws" / task_id
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "check.py").write_text(check, encoding="utf-8")
        return TaskContract(task_id=task_id, goal="Write calc.py with add(a, b).", workspace=ws,
                            acceptance=Acceptance(command, timeout_s, files=("check.py",)), budget=budget or Budget(),
                            deliver_to=(self.root / "out") if deliver else None, protected=protected)

    def run_task(self, worker_steps, reviewer_steps, **kw):
        c = self.contract(**kw)
        self.worker = ScriptedModel(worker_steps, "w")
        self.reviewer = ScriptedModel(reviewer_steps, "r")
        return c, Kernel(self.ledger, self.worker, self.reviewer).run(c)

    def kinds(self, task_id="t1"):
        return [e.kind for e in self.ledger.events(task_id)]
