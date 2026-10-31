"""Owner acceptance for card T19 Part C. The worker cannot change this file (it is a protected acceptance file).

Run from the repository root:  python tasks/T19_check.py
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "examples" / "trust_demo" / "greet.py"


def fail(msg: str) -> None:
    print(f"T19 CHECK FAILED: {msg}")
    raise SystemExit(1)


def main() -> None:
    if not TARGET.is_file():
        fail("examples/trust_demo/greet.py does not exist")
    spec = importlib.util.spec_from_file_location("t19_greet", TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("m2k", "世界", ""):
        got = module.greet(name)
        if got != f"hello, {name}":
            fail(f"greet({name!r}) returned {got!r}, expected {'hello, ' + name!r}")
    full = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(full.stderr[-1500:])
    if full.returncode != 0:
        fail("full test suite fails")
    print("T19 CHECK OK")


if __name__ == "__main__":
    main()
