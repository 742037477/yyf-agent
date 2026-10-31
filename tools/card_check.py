"""Scope gate for task PRs. Reads the task card named in the PR title ("T03: ...") and checks:
  1. every changed file matches the card's "Files you may change" patterns (plus the card itself)
  2. lines added under m2k/ do not exceed the card's "Line budget"
Exit 1 with a plain list of violations. Standard library only.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def find_card(title: str) -> Path:
    m = re.match(r"\s*(T\d+)\b", title)
    if not m:
        sys.exit(f"PR title must start with the card id, e.g. 'T03: ...' (got {title!r})")
    cards = sorted((ROOT / "tasks").glob(f"{m.group(1)}-*.md"))
    if len(cards) != 1:
        sys.exit(f"expected exactly one card tasks/{m.group(1)}-*.md, found {len(cards)}")
    return cards[0]


def parse_card(text: str) -> tuple[list[str], int | None]:
    files = re.search(r"\*\*Files you may change:\*\*([^\n]*)", text)
    budget = re.search(r"\*\*Line budget:\*\*\s*≤?\s*(\d+)", text)
    lines = [files.group(1)] if files else []
    if files:
        for line in text[files.end():].splitlines()[1:]:
            if not line.strip() or line.startswith(("- **", "#")):
                break
            lines.append(line)
    patterns = re.findall(r"`([^`]+)`", "\n".join(lines))
    return patterns, int(budget.group(1)) if budget else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--title", required=True)
    a = ap.parse_args()
    card = find_card(a.title)
    patterns, budget = parse_card(card.read_text(encoding="utf-8"))
    if not patterns:
        sys.exit(f"{card.name} has no '**Files you may change:**' list")
    allowed = patterns + [card.relative_to(ROOT).as_posix()]
    changed = [f for f in git("diff", "--name-only", f"{a.base}...HEAD").splitlines() if f]
    problems = [f"out of scope: {f}" for f in changed if not any(fnmatch.fnmatch(f, p) for p in allowed)]
    added = 0
    for line in git("diff", "--numstat", f"{a.base}...HEAD", "--", "m2k/").splitlines():
        n = line.split("\t")[0]
        added += int(n) if n.isdigit() else 0
    if budget is not None and added > budget:
        problems.append(f"line budget exceeded: {added} lines added under m2k/ (budget {budget})")
    print(f"card: {card.name}\nallowed: {allowed}\nchanged: {changed}\nadded under m2k/: {added} (budget {budget})")
    for p in problems:
        print(f"::error::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
