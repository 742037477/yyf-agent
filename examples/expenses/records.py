"""Load and validate expense records from a CSV file.

Standard library only. The CSV header must be exactly ``date,category,amount,note``.
Every malformed row (or the header itself) raises :class:`ValueError` naming the
1-based line number in the file (the header is line 1).
"""

from __future__ import annotations

import csv
import datetime
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

HEADER = ["date", "category", "amount", "note"]


@dataclass(frozen=True)
class Expense:
    """A single validated expense record."""

    date: datetime.date
    category: str
    amount: Decimal
    note: str


def _bad(line: int, reason: str) -> ValueError:
    return ValueError(f"line {line}: {reason}")


def _parse_date(text: str, line: int) -> datetime.date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise _bad(line, f"invalid date {text!r} (expected YYYY-MM-DD)")
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise _bad(line, f"invalid date {text!r} (expected YYYY-MM-DD)") from None


def _parse_amount(text: str, line: int) -> Decimal:
    try:
        amount = Decimal(text.strip())
    except InvalidOperation:
        raise _bad(line, f"invalid amount {text!r}") from None
    if not amount.is_finite() or amount <= 0:
        raise _bad(line, f"amount must be greater than zero, got {text!r}")
    if amount.as_tuple().exponent < -2:
        raise _bad(line, f"amount must have at most two decimal places, got {text!r}")
    return amount


def load(path: str | Path) -> list[Expense]:
    """Read *path* (UTF-8 CSV) and return a list of :class:`Expense`."""
    items: list[Expense] = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for line, row in enumerate(reader, start=1):
            if line == 1:
                if row != HEADER:
                    raise _bad(line, f"bad header {row!r} (expected {','.join(HEADER)})")
                continue
            if len(row) != len(HEADER):
                raise _bad(line, f"expected {len(HEADER)} fields, got {len(row)}")
            raw_date, raw_category, raw_amount, note = row
            date = _parse_date(raw_date.strip(), line)
            category = raw_category.strip().lower()
            if not category:
                raise _bad(line, "category must not be empty")
            amount = _parse_amount(raw_amount, line)
            items.append(Expense(date=date, category=category, amount=amount, note=note))
    return items
