"""Offline demo: the full chain with scripted models, no API key, no cost."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .cli import print_report
from .contract import Acceptance, Budget, TaskContract
from .kernel import Kernel
from .ledger import Ledger
from .models import ScriptedModel
from .verify import scripted_review

CALC = '''def add(a, b):
    return a + b


def sub(a, b):
    return a - b
'''

TEST = '''import unittest

from calc import add, sub


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)


if __name__ == "__main__":
    unittest.main()
'''


def _step(thought: str, tool: str, **args) -> str:
    return json.dumps({"thought": thought, "tool": tool, "args": args}, ensure_ascii=False)


def run_demo(base: Path) -> int:
    base = base.resolve()
    tid = time.strftime("demo-%Y%m%d-%H%M%S")
    seed = base / "seeds" / tid
    seed.mkdir(parents=True)
    (seed / "test_calc.py").write_text(TEST, encoding="utf-8")
    contract = TaskContract(
        task_id=tid,
        goal="Create calc.py with add(a, b) and sub(a, b) to pass the owner's test_calc.py.",
        workspace=base / "workspaces" / tid,
        acceptance=Acceptance("{python} test_calc.py -v", 60, files=("test_calc.py",)),
        budget=Budget(max_rounds=10, max_model_calls=12, max_commands=5),
        constraints=("Standard library only.",),
        deliver_to=base / "delivered", seed=seed)
    worker = ScriptedModel([
        _step("See what is there.", "list_files", path="."),
        _step("Try writing outside the workspace (the gate should stop this).", "write_file",
              path="../escape.txt", content="should never be written"),
        _step("Write the implementation.", "write_file", path="calc.py", content=CALC),
        _step("Run the acceptance check myself.", "run_command", command="{python} test_calc.py -v"),
        _step("Done.", "finish", summary="calc.py with add/sub and passing tests"),
    ], name="scripted-worker")
    reviewer = ScriptedModel([scripted_review("GO", "add/sub correct", "tests cover both")],
                             name="scripted-reviewer")
    with Ledger(base / "ledger.sqlite") as ledger:
        report = Kernel(ledger, worker, reviewer).run(contract)
        events = ledger.events(tid)
        ok, _ = ledger.verify_chain()
    print("ledger events:")
    for e in events:
        payload = json.dumps(e.payload, ensure_ascii=False)
        print(f"  {e.seq:>4} {e.kind:<18} {payload[:110]}")
    print_report(report)
    print(f"ledger hash chain: {'OK' if ok else 'BROKEN'}  ({base / 'ledger.sqlite'})")
    return 0 if report.verdict.value == "PASS" else 1
