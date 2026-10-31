"""Aggregation helpers for expense records.

Each helper accepts any iterable of objects exposing ``date``,
``category`` and ``amount`` attributes (e.g. ``records.Expense``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Protocol


class _ItemLike(Protocol):
    date: object
    category: str
    amount: Decimal


def _sum_by(items: Iterable[_ItemLike], key) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for item in items:
        name = key(item)
        totals[name] = totals.get(name, Decimal("0")) + item.amount
    return {name: totals[name] for name in sorted(totals)}


def by_category(items: Iterable[_ItemLike]) -> dict[str, Decimal]:
    """Total amounts per category, keys sorted."""
    return _sum_by(items, lambda item: item.category)


def by_month(items: Iterable[_ItemLike]) -> dict[str, Decimal]:
    """Total amounts per month as ``YYYY-MM``, keys sorted."""
    return _sum_by(items, lambda item: f"{item.date.year:04d}-{item.date.month:02d}")


def total(items: Iterable[_ItemLike]) -> Decimal:
    """Grand total; ``Decimal('0')`` for an empty iterable."""
    result = Decimal("0")
    for item in items:
        result += item.amount
    return result
