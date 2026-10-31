"""Owner check, part "summary": examples/expenses/summary.py totals expenses (any objects with date, category, amount)."""
import datetime
from decimal import Decimal
from types import SimpleNamespace as E

from _common import fail

try:
    from summary import by_category, by_month, total
except Exception as e:  # noqa: BLE001
    fail(f"cannot import by_category, by_month and total from examples/expenses/summary.py: {e!r}")

d = datetime.date
items = [E(date=d(2026, 10, 2), category="food", amount=Decimal("7.25"), note=""),
         E(date=d(2026, 9, 3), category="food", amount=Decimal("12.50"), note="lunch"),
         E(date=d(2026, 9, 1), category="rent", amount=Decimal("900"), note=""),
         E(date=d(2025, 12, 31), category="books", amount=Decimal("0.10"), note="")]
cats = by_category(items)
if cats != {"books": Decimal("0.10"), "food": Decimal("19.75"), "rent": Decimal("900")} or list(cats) != ["books", "food", "rent"]:
    fail(f"by_category wrong or not sorted by category: {cats!r}")
months = by_month(items)
if months != {"2025-12": Decimal("0.10"), "2026-09": Decimal("912.50"), "2026-10": Decimal("7.25")} \
        or list(months) != ["2025-12", "2026-09", "2026-10"]:
    fail(f"by_month wrong or not sorted by month: {months!r}")
if total(items) != Decimal("919.85") or not isinstance(total(items), Decimal):
    fail(f"total wrong: {total(items)!r}")
if (by_category([]), by_month([]), total([])) != ({}, {}, Decimal("0")):
    fail("empty input gives {}, {} and Decimal('0')")
print("summary OK")
