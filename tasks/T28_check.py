"""Owner acceptance for card T28. The worker cannot change this file (it is a protected acceptance file).

Run from the repository root:  python tasks/T28_check.py --part N   (N = 1..5; the card is done in five runs)
Each part requires the attacks and defences of all earlier parts too; part 5 is the whole card.
"""
import argparse
import io
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULE = "tests.test_stage3_attack"
PARTS = {1: range(1, 7), 2: range(7, 12), 3: [12, 13, 14, 15, 16, 20], 4: [17, 18, 19], 5: []}
D_CHAIN, D_PLAN, D_ROOM, D_RESERVE, D_REPEAT, D_SCOPE = (
    "child chain and recall checks off", "plan re-check in the kernel off", "planner budget headroom off",
    "atomic reservation off", "repeated-failure check off", "write-scope gate off")
NEW_DEFENCES = {1: [D_CHAIN, D_PLAN, D_SCOPE], 2: [D_REPEAT], 3: [D_RESERVE, D_ROOM], 4: [], 5: []}
NAME, EXPECTED = "前任留下伪造交接说明", "接手者只看账本事实，不受误导，PASS"
BEFORE = "Worker 伪造字节码缓存劫持验收"


class Recorder(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def run_tests(label: str) -> Recorder:
    for name in [m for m in sys.modules if m == MODULE]:
        del sys.modules[name]
    suite = unittest.defaultTestLoader.loadTestsFromName(MODULE)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0, resultclass=Recorder).run(suite)
    print(f"{label}: {result.testsRun} tests, {len(result.failures)} failures, {len(result.errors)} errors, "
          f"{len(result.expectedFailures)} expected failures")
    return result


def fail(msg: str) -> None:
    print(f"T28 CHECK FAILED: {msg}")
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--part", type=int, choices=sorted(PARTS), default=5)
    part = parser.parse_args().part
    attacks = [f"a{n:02d}" for p in sorted(PARTS) if p <= part for n in PARTS[p]]
    wanted = {d for p in sorted(PARTS) if p <= part for d in NEW_DEFENCES[p]}
    print(f"part {part}: attacks {', '.join(attacks)}")
    plain = run_tests("as written")
    if not plain.wasSuccessful():
        fail("the attack tests do not pass")
    method = lambda test_id: test_id.rsplit(".", 1)[-1]
    weak = [method(t.id()) for t, _ in plain.expectedFailures]
    for a in attacks:
        if not any(method(t).startswith(f"test_{a}_") for t in plain.passed + weak):
            fail(f"no test for attack {a} (a test method named test_{a}_<name>)")
    if weak:
        print("WEAKNESSES FOUND (expectedFailure; the reviewer must decide):", ", ".join(weak))

    import m2k.gates as gates
    import m2k.kernel as kernel
    import m2k.ledger as ledger
    from m2k.types import GateDecision
    defences = [
        (D_CHAIN, mock.patch.object(kernel.Kernel, "_verify_child", lambda self, c, p: None)),
        (D_PLAN, mock.patch.object(kernel, "check_subtasks", lambda c: None)),
        (D_ROOM, mock.patch.object(kernel.Kernel, "_plan_fits", lambda self, c, s: None)),
        (D_RESERVE, mock.patch.object(ledger.Ledger, "reserve", lambda self, *a, **k: True)),
        (D_REPEAT, mock.patch.object(kernel.Kernel, "_scope_digest",
                                     staticmethod(lambda cand, writes: uuid.uuid4().hex))),
        (D_SCOPE, mock.patch.object(gates.WriteScopeGate, "check",
                                    lambda self, ctx, p: GateDecision(True, "write_scope"))),
    ]
    for label, patch in defences:
        if label not in wanted:
            continue
        with patch:
            result = run_tests(label)
        if result.wasSuccessful():
            fail(f"tests still pass with the {label}: they do not exercise that defence")

    if part == 5:
        offline_scenario()
    full = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(full.stderr[-1500:])
    if full.returncode != 0:
        fail("full test suite fails")
    print(f"T28 PART {part} CHECK OK" if part < 5 else "T28 CHECK OK")


def offline_scenario() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run([sys.executable, "-m", "m2k", "accept", "--offline", "--dir", tmp], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(out.stdout[-2500:])
    lines = out.stdout.splitlines()
    idx = next((i for i, line in enumerate(lines) if NAME in line), None)
    before = next((i for i, line in enumerate(lines) if BEFORE in line), None)
    if idx is None or not lines[idx].lstrip().startswith("✅"):
        fail(f"offline scenario '{NAME}' missing or not ✅")
    if before is None or idx > before:
        fail(f"'{NAME}' must come before '{BEFORE}' (in OFFLINE, before s_bytecode)")
    if idx + 1 >= len(lines) or EXPECTED not in lines[idx + 1]:
        fail(f"expected text must be exactly: {EXPECTED}")
    if out.returncode != 0:
        fail("m2k accept --offline: not every scenario passed")


if __name__ == "__main__":
    main()
