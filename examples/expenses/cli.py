"""Command line expense report.

Usage: ``python examples/expenses/cli.py FILE``

Prints per-category and per-month totals plus the grand total for the
validated CSV at *FILE*.  A malformed file exits 1 with ``error: ...`` on
stderr; a missing argument exits 2 with a usage line on stderr.
"""

from __future__ import annotations

import sys
from decimal import Decimal

import records
import summary

USAGE = "usage: python cli.py FILE"


def _format(amount: Decimal) -> str:
    return f"{amount:.2f}"


def build_report(items) -> str:
    """Return the full report text for *items*."""
    lines = ["By category:"]
    for category, amount in summary.by_category(items).items():
        lines.append(f"  {category}: {_format(amount)}")
    lines.append("By month:")
    for month, amount in summary.by_month(items).items():
        lines.append(f"  {month}: {_format(amount)}")
    lines.append(f"Total: {_format(summary.total(items))}")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        items = records.load(argv[1])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(build_report(items))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
