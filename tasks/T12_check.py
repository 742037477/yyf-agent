"""Owner acceptance for card T12. The worker cannot change this file (it is a protected acceptance file).

Run from the repository root:  python tasks/T12_check.py
"""
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NAME = "有人改写账本并重算整条哈希链"
TAMPER = "有人事后改账本里的判定"
EXPECTED = "普通校验发现不了；对照外部锚点发现改写"


def run_tests(label: str) -> unittest.TestResult:
    for mod in [m for m in sys.modules if m == "tests.test_anchor_attack"]:
        del sys.modules[mod]
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_anchor_attack")
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    print(f"{label}: {result.testsRun} tests, {len(result.failures)} failures, {len(result.errors)} errors")
    return result


def fail(msg: str) -> None:
    print(f"T12 CHECK FAILED: {msg}")
    raise SystemExit(1)


def main() -> None:
    plain = run_tests("as written")
    if plain.testsRun < 3:
        fail("need at least 3 tests in tests/test_anchor_attack.py")
    if not plain.wasSuccessful():
        fail("the new tests do not pass")

    import m2k.anchor
    import m2k.ledger
    with mock.patch.object(m2k.anchor, "check", lambda *a, **k: []):
        blind = run_tests("anchor.check blinded")
    if blind.wasSuccessful():
        fail("tests still pass when anchor.check finds nothing: they do not rely on the anchors")
    with mock.patch.object(m2k.ledger.Ledger, "verify_chain", lambda self: (False, 1)):
        broken = run_tests("verify_chain always broken")
    if broken.wasSuccessful():
        fail("tests still pass when verify_chain reports a break: the recompute is not checked")

    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run([sys.executable, "-m", "m2k", "accept", "--offline", "--dir", tmp], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(out.stdout[-3000:])
        lines = out.stdout.splitlines()
        idx = next((i for i, l in enumerate(lines) if NAME in l), None)
        tamper = next((i for i, l in enumerate(lines) if TAMPER in l), None)
        if idx is None or not lines[idx].lstrip().startswith("✅"):
            fail(f"offline scenario '{NAME}' missing or not ✅")
        if tamper is None or idx < tamper:
            fail(f"'{NAME}' must run after the tamper scenario (append it in run_accept after s_tamper)")
        if idx + 1 >= len(lines) or EXPECTED not in lines[idx + 1]:
            fail(f"expected text must be exactly: {EXPECTED}")
        if out.returncode != 0:
            fail("m2k accept --offline: not every scenario passed")
        run_dir = next(Path(tmp).glob("accept-*"))
        from m2k.ledger import Ledger
        with Ledger(run_dir / "ledger.sqlite") as shared:
            if shared.verify_chain()[0]:
                fail("the shared bench ledger was repaired: the drill must not touch it")
        if not [q for q in run_dir.rglob("*.sqlite") if q.parent != run_dir]:
            fail("the drill must use its own ledger in a sub-folder of the run folder")

    full = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(full.stderr[-1500:])
    if full.returncode != 0:
        fail("full test suite fails")
    print("T12 CHECK OK")


if __name__ == "__main__":
    main()
