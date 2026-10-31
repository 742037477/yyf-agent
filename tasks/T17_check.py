"""Owner acceptance for card T17. The worker cannot change this file (it is a protected acceptance file).

Run from the repository root:  python tasks/T17_check.py
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

NAME = "用伪造的凭证继续干活"
EXPECTED = "被拒，不交付"
BEFORE = "Worker 伪造字节码缓存劫持验收"
MODULE = "tests.test_trust_attack"


def run_tests(label: str) -> unittest.TestResult:
    sys.modules.pop(MODULE, None)
    suite = unittest.defaultTestLoader.loadTestsFromName(MODULE)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    print(f"{label}: {result.testsRun} tests, {len(result.failures)} failures, {len(result.errors)} errors")
    return result


def fail(msg: str) -> None:
    print(f"T17 CHECK FAILED: {msg}")
    raise SystemExit(1)


def main() -> None:
    plain = run_tests("as written")
    if plain.testsRun < 7:
        fail("need at least 7 tests in tests/test_trust_attack.py")
    if not plain.wasSuccessful():
        fail("the new tests do not pass")

    import m2k.trust as trust
    real_open = trust.open_envelope

    def no_signature_check(env, public_key, code="SIGNATURE"):
        try:
            return real_open(env, public_key, code)
        except trust.TrustError as e:
            if e.code.endswith("SIGNATURE") and isinstance(env, dict) and isinstance(env.get("body"), dict):
                return env["body"]
            raise
    with mock.patch.object(trust, "open_envelope", no_signature_check):
        blind = run_tests("signatures not checked")
    if blind.wasSuccessful():
        fail("tests still pass when signatures are not checked: they do not rely on the signatures")
    with mock.patch.object(trust, "verify_chain", lambda *a, **k: {"credential_id": "x"}):
        open_door = run_tests("verify_chain accepts anything")
    if open_door.wasSuccessful():
        fail("tests still pass when verify_chain accepts anything: the chain is not exercised")

    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run([sys.executable, "-m", "m2k", "accept", "--offline", "--dir", tmp], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(out.stdout[-3000:])
    lines = out.stdout.splitlines()
    idx = next((i for i, l in enumerate(lines) if NAME in l), None)
    before = next((i for i, l in enumerate(lines) if BEFORE in l), None)
    if idx is None or not lines[idx].lstrip().startswith("✅"):
        fail(f"offline scenario '{NAME}' missing or not ✅")
    if before is None or idx > before:
        fail(f"'{NAME}' must come right before '{BEFORE}' (in OFFLINE, before s_bytecode)")
    if idx + 1 >= len(lines) or EXPECTED not in lines[idx + 1]:
        fail(f"expected text must be exactly: {EXPECTED}")
    if out.returncode != 0:
        fail("m2k accept --offline: not every scenario passed")
    accept_src = (ROOT / "m2k" / "accept.py").read_text(encoding="utf-8")
    forged = accept_src[accept_src.find("def s_forged_credential"):accept_src.find("def s_bytecode")]
    if "worker_model =" in forged or "Kernel(b.ledger, None" in forged:
        fail("s_forged_credential must pass the real worker model to Kernel(...), not None or a later assignment")

    full = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(full.stderr[-1500:])
    if full.returncode != 0:
        fail("full test suite fails")
    print("T17 CHECK OK")


if __name__ == "__main__":
    main()
