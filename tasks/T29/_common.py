"""Shared by the T29 owner checks: where the tool lives and how to write a sample CSV. Owner file, protected."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "examples" / "expenses"
sys.path.insert(0, str(TOOL))

SAMPLE = ("date,category,amount,note\n"
          "2026-09-03,food,12.50,lunch\n"
          "2026-09-01,rent,900,September\n"
          "2026-10-02, Food ,7.25,\n"
          "2026-10-15,travel,40.10,\"train, return\"\n")


def csv_file(text: str) -> Path:
    folder = Path(tempfile.mkdtemp())
    path = folder / "expenses.csv"
    path.write_text(text, encoding="utf-8", newline="")
    return path


def fail(msg: str) -> None:
    print(f"CHECK FAILED: {msg}")
    raise SystemExit(1)
