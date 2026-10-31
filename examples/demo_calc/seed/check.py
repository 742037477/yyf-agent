"""Acceptance check for demo_calc. Exit code 0 = pass."""
from calc import add, div, mul, sub

assert add(2, 3) == 5
assert sub(5, 3) == 2
assert mul(4, 3) == 12
assert div(9, 3) == 3
try:
    div(1, 0)
except ValueError as e:
    assert "division by zero" in str(e), e
else:
    raise AssertionError("div(1, 0) must raise ValueError")
print("all checks passed")
