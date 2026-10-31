"""Owner check, part "records": examples/expenses/records.py loads and validates the CSV."""
import datetime
from decimal import Decimal

from _common import SAMPLE, csv_file, fail

try:
    from records import Expense, load
except Exception as e:  # noqa: BLE001 - any import problem is a failed check
    fail(f"cannot import Expense and load from examples/expenses/records.py: {e!r}")

rows = load(csv_file(SAMPLE))
if not isinstance(rows, list) or len(rows) != 4 or not all(isinstance(r, Expense) for r in rows):
    fail(f"load() must return a list of 4 Expense objects, got {rows!r}")
first = rows[0]
if (first.date, first.category, first.amount, first.note) != (datetime.date(2026, 9, 3), "food", Decimal("12.50"), "lunch"):
    fail(f"first row parsed wrongly: {first!r}")
if rows[1].amount != Decimal("900") or not isinstance(rows[1].amount, Decimal):
    fail("amounts must be decimal.Decimal values")
if rows[2].category != "food" or rows[2].note != "":
    fail(f"category must be stripped and lower-cased, note may be empty: {rows[2]!r}")
if rows[3].note != "train, return":
    fail(f"quoted notes with commas must survive: {rows[3]!r}")
try:
    first.amount = Decimal(1)
except Exception:  # noqa: BLE001 - frozen dataclass raises FrozenInstanceError
    pass
else:
    fail("Expense must be immutable (a frozen dataclass)")

HEADER = "date,category,amount,note\n"
BAD = {
    "date": (HEADER + "2026-09-01,food,1,ok\n2026-13-01,food,1,bad month\n", 3),
    "empty category": (HEADER + "2026-09-01,  ,1,x\n", 2),
    "amount text": (HEADER + "2026-09-01,food,ten,x\n", 2),
    "amount zero": (HEADER + "2026-09-01,food,0,x\n", 2),
    "amount negative": (HEADER + "2026-09-01,food,-3,x\n", 2),
    "three decimals": (HEADER + "2026-09-01,food,1.005,x\n", 2),
    "missing field": (HEADER + "2026-09-01,food,1\n", 2),
    "wrong header": ("day,category,amount,note\n2026-09-01,food,1,x\n", 1),
}
for name, (text, line) in BAD.items():
    try:
        load(csv_file(text))
    except ValueError as e:
        if f"line {line}" not in str(e):
            fail(f"{name}: the ValueError must name 'line {line}', got: {e}")
    except Exception as e:  # noqa: BLE001
        fail(f"{name}: must raise ValueError, raised {e!r}")
    else:
        fail(f"{name}: must raise ValueError")
if load(csv_file(HEADER)) != []:
    fail("a file with only the header loads as an empty list")
print("records OK")
