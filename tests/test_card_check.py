"""Task-card file lists stop at the next metadata bullet, heading, or blank line."""
import re
import unittest
from pathlib import Path

from tools.card_check import parse_card


ROOT = Path(__file__).resolve().parents[1]


class CardCheckTest(unittest.TestCase):
    def test_one_line_list_and_budget(self):
        card = '- **Files you may change:** `m2k/a.py`, `tests/test_*.py`\n- **Line budget:** ≤ 80 added lines\n'
        self.assertEqual(parse_card(card), (["m2k/a.py", "tests/test_*.py"], 80))

    def test_wrapped_t69_list(self):
        card = (ROOT / "tasks/T69-small-fixes-2.md").read_text(encoding="utf-8")
        line = next(line for line in card.splitlines() if line.startswith("- **Files you may change:**"))
        expected = re.findall(r"`([^`]+)`", line)
        wrapped = "- **Files you may change:** " + ",\n  ".join(
            ", ".join(f"`{path}`" for path in expected[start:start + 6]) for start in range(0, len(expected), 6))
        patterns, budget = parse_card(card.replace(line, wrapped))
        self.assertEqual(patterns, expected)
        self.assertEqual(budget, 160)

    def test_status_bullet_stops_wrapped_list_without_blank_line(self):
        card = '- **Files you may change:** `m2k/a.py`,\n  `tests/a.py`\n- **Status:** `OPEN`\n'
        self.assertEqual(parse_card(card)[0], ["m2k/a.py", "tests/a.py"])

    def test_list_can_start_on_the_next_line(self):
        self.assertEqual(parse_card('- **Files you may change:**\n  `m2k/a.py`\n')[0], ["m2k/a.py"])

    def test_later_paths_never_extend_the_list(self):
        for separator in ("\n", " \t\n", "# Goal\n", "## Required behaviour\n", '- **Status:** `OPEN`\n'):
            for newline in ("\n", "\r\n"):
                with self.subTest(separator=separator, newline=newline):
                    card = ('- **Files you may change:** `m2k/a.py`,\n  `tests/a.py`\n' + separator
                            + 'Goal mentions `outside.py`.\n## Required behaviour\nDo not allow `other/*`.\n')
                    self.assertEqual(parse_card(card.replace("\n", newline))[0], ["m2k/a.py", "tests/a.py"])

    def test_every_existing_card_keeps_all_previously_allowed_patterns(self):
        cards = list((ROOT / "tasks").glob("*.md"))
        self.assertTrue(cards)
        for path in cards:
            with self.subTest(card=path.name):
                card = path.read_text(encoding="utf-8")
                old = re.search(r"\*\*Files you may change:\*\*(.+)", card)
                expected = set(re.findall(r"`([^`]+)`", old.group(1))) if old else set()
                self.assertTrue(expected.issubset(parse_card(card)[0]))
