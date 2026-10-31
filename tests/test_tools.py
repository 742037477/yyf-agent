"""Unit tests for m2k.tools (Workspace, list_files, read_file, write_file, run_command).

Temporary directories only. Standard library unittest only so this passes on
Windows and Linux. Command tests use {python} -c "..." and keep every timeout
at or below 2 seconds.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from m2k.tools import MAX_READ, ScopeError, Workspace, execute
from m2k.types import EffectStatus

# The exact marker read_file appends when it cuts a file that is too long.
TRUNCATION_MARKER = "\n...[truncated"


class WorkspaceResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        self.ws = Workspace(self.root)

    def test_resolve_relative_path_is_resolved_inside_workspace(self):
        # Workspace.resolve returns fully resolved paths, so compare against a
        # .resolve()d expectation (Windows temp may be C:/Users/RUNNER~1/...).
        self.assertEqual(self.ws.resolve("a/b.txt"), (self.root / "a" / "b.txt").resolve())
        self.assertEqual(self.ws.resolve("a/../b.txt"), (self.root / "b.txt").resolve())
        self.assertEqual(self.ws.resolve("."), self.root.resolve())

    def test_resolve_rejects_paths_that_escape_workspace(self):
        outside_abs = str((self.root.parent / "outside.txt"))
        for bad in ("../x", outside_abs, "a/../../x"):
            with self.subTest(path=bad):
                with self.assertRaises(ScopeError):
                    self.ws.resolve(bad)


class ListFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        self.ws = Workspace(self.root)

    def _write(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def test_list_files_lists_nested_and_skips_noise_dirs(self):
        self._write("top.txt")
        self._write("src/main.py")
        self._write("src/sub/deep.py")
        self._write(".git/config")
        self._write("__pycache__/x.pyc")
        self._write(".m2k/ledger.jsonl")

        result = execute(self.ws, "list_files", {"path": "."})
        self.assertEqual(result.status, EffectStatus.COMPLETED)
        lines = result.output.splitlines()
        self.assertIn("top.txt", lines)
        self.assertIn("src/main.py", lines)
        self.assertIn("src/sub/deep.py", lines)
        for line in lines:
            head = line.split("/", 1)[0]
            self.assertNotIn(head, {".git", "__pycache__", ".m2k"}, line)


class ReadFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        self.ws = Workspace(self.root)

    def test_read_file_returns_utf8_content(self):
        (self.root / "note.txt").write_text("hello \u4e16\u754c\n", encoding="utf-8", newline="\n")
        result = execute(self.ws, "read_file", {"path": "note.txt"})
        self.assertEqual(result.status, EffectStatus.COMPLETED)
        self.assertEqual(result.output, "hello \u4e16\u754c\n")

    def test_read_file_missing_file_fails(self):
        result = execute(self.ws, "read_file", {"path": "missing.txt"})
        self.assertEqual(result.status, EffectStatus.FAILED)
        self.assertIn("not a file", result.output)

    def test_read_file_truncates_long_content(self):
        content = "a" * (MAX_READ + 500)
        (self.root / "big.txt").write_text(content, encoding="utf-8", newline="\n")

        result = execute(self.ws, "read_file", {"path": "big.txt"})
        self.assertEqual(result.status, EffectStatus.COMPLETED)

        head, marker, tail = result.output.partition(TRUNCATION_MARKER)
        self.assertTrue(marker, "expected the truncation marker")
        # The marker must be present and the text before it must be exactly
        # MAX_READ characters: this fails if read_file returns the whole text
        # with the marker merely appended.
        self.assertEqual(len(head), MAX_READ)
        self.assertEqual(head, content[:MAX_READ])
        self.assertIn(str(len(content)), tail)


class WriteFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        self.ws = Workspace(self.root)

    def test_write_file_creates_parent_folders(self):
        result = execute(self.ws, "write_file", {"path": "a/b/c.txt", "content": "data"})
        self.assertEqual(result.status, EffectStatus.COMPLETED)
        self.assertEqual((self.root / "a" / "b" / "c.txt").read_text(encoding="utf-8"), "data")

    def test_write_file_writes_utf8(self):
        content = "\u4e2d\u6587\u5185\u5bb9\n\u7b2c\u4e8c\u884c\n"
        result = execute(self.ws, "write_file", {"path": "\u4e2d\u6587/out.txt", "content": content})
        self.assertEqual(result.status, EffectStatus.COMPLETED)
        raw = (self.root / "\u4e2d\u6587" / "out.txt").read_bytes()
        self.assertEqual(raw, content.encode("utf-8"))
        self.assertEqual(raw.decode("utf-8"), content)


class RunCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ws"
        self.root.mkdir()
        self.ws = Workspace(self.root)

    def test_run_command_exit_zero_is_completed(self):
        result = execute(self.ws, "run_command",
                         {"command": '{python} -c "print(1 + 1)"', "timeout_s": 2})
        self.assertEqual(result.status, EffectStatus.COMPLETED)
        self.assertEqual(result.detail.get("exit_code"), 0)
        self.assertIn("2", result.output)

    def test_run_command_nonzero_exit_is_failed(self):
        result = execute(self.ws, "run_command",
                         {"command": '{python} -c "import sys; sys.exit(3)"', "timeout_s": 2})
        self.assertEqual(result.status, EffectStatus.FAILED)
        self.assertEqual(result.detail.get("exit_code"), 3)

    def test_run_command_timeout_is_unknown(self):
        result = execute(self.ws, "run_command",
                         {"command": '{python} -c "import time; time.sleep(1.5)"', "timeout_s": 1})
        self.assertEqual(result.status, EffectStatus.UNKNOWN)
        self.assertEqual(result.detail.get("reason"), "TIMEOUT")
        self.assertEqual(result.detail.get("timeout_s"), 1)


if __name__ == "__main__":
    unittest.main()
