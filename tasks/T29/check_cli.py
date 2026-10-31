"""Owner check, part "cli": `python examples/expenses/cli.py FILE` prints the report; errors go to stderr."""
import subprocess
import sys

from _common import SAMPLE, TOOL, csv_file, fail

CLI = TOOL / "cli.py"
if not CLI.is_file():
    fail("examples/expenses/cli.py is missing")


def run(*args):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True, encoding="utf-8")


EXPECTED = """By category:
  food: 19.75
  rent: 900.00
  travel: 40.10
By month:
  2026-09: 912.50
  2026-10: 47.35
Total: 959.85
"""
r = run(csv_file(SAMPLE))
if r.returncode != 0 or r.stdout.replace("\r\n", "\n") != EXPECTED:
    fail(f"report for the sample is wrong (exit {r.returncode}).\nexpected:\n{EXPECTED}\ngot:\n{r.stdout}\nstderr:\n{r.stderr}")
r = run(csv_file("date,category,amount,note\n2026-09-01,food,ten,x\n"))
if r.returncode != 1 or not r.stderr.startswith("error: ") or "line 2" not in r.stderr or r.stdout:
    fail(f"an invalid file must print 'error: ...line 2...' to stderr, nothing to stdout, exit 1; got exit {r.returncode}, "
         f"stdout {r.stdout!r}, stderr {r.stderr!r}")
r = run()
if r.returncode != 2 or "usage" not in r.stderr.lower():
    fail(f"no argument must print a usage line to stderr and exit 2; got exit {r.returncode}, stderr {r.stderr!r}")
r = run(csv_file("date,category,amount,note\n"))
if r.returncode != 0 or r.stdout.replace("\r\n", "\n") != "By category:\nBy month:\nTotal: 0.00\n":
    fail(f"an empty file prints empty sections and 'Total: 0.00'; got {r.stdout!r}")
print("cli OK")
