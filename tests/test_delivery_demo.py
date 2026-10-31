"""T87：真实本地 OBS 旁证的离线演示及其拒绝覆盖行为。"""
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from m2k import trust
from m2k.delivery import check_delivery


def load_demo():
    path = Path(__file__).resolve().parents[1] / "examples" / "delivery_check" / "make_demo.py"
    spec = importlib.util.spec_from_file_location("delivery_check_demo", path)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    return demo


class DeliveryDemoTest(unittest.TestCase):
    def test_demo_checks_real_receipts_in_both_modes(self):
        demo = load_demo()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "演示 空目录"
            root.mkdir()
            output = io.StringIO()
            with redirect_stdout(output):
                code = demo.main([str(root)])
            text = output.getvalue()
            self.assertEqual(code, 0, text)
            for name in ("report-ok", "report-edited", "report-swapped", "report-forged"):
                self.assertIn(f"{name}: PASS\n", text)
            pristine = text.split("第 3 步：", 1)[1].split("第 4 步：", 1)[0]
            for name in ("report-edited", "report-forged", "report-ok", "report-swapped"):
                self.assertIn(f"OK {name} (2 files)\n", pristine)
            self.assertIn("合计 4 份：通过 4 份，未经核验 0 份\n退出码：0\n", pristine)
            after = text.split("第 5 步：", 1)[1].split("第 6 步：", 1)[0]
            expected = ("未经核验 hand-made: missing receipt file\n"
                        "未经核验 report-edited: changed file: calc.py\n"
                        "未经核验 report-forged: receipt event hash mismatch\n"
                        "OK report-ok (2 files)\n"
                        "未经核验 report-swapped: missing file: check.py; extra file: notes.txt\n"
                        "合计 5 份：通过 1 份，未经核验 4 份\n退出码：1\n")
            self.assertEqual(after.split("独立模式：\n", 1)[1], expected + "完整模式：\n" + expected)
            public = (root / "public-key.txt").read_text(encoding="utf-8").strip()
            self.assertEqual(public, trust.public_key_b64(trust.load_private_key(root / "obs.pem")))
            self.assertTrue((root / "ledger.sqlite").is_file())
            self.assertTrue((root / "obs.jsonl").is_file())
            results = check_delivery(root / "delivered", public)
            self.assertEqual(results["report-ok"]["problems"], [])
            self.assertIn("Windows（PowerShell）：python -m m2k check-delivery", text)
            self.assertIn("bash：python -m m2k check-delivery", text)

    def test_nonempty_directory_and_existing_file_are_preserved(self):
        demo = load_demo()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keep = root / "keep.txt"
            keep.write_bytes(b"keep exactly\x00\xff")
            before = list(root.iterdir())
            for target in (root, keep):
                with self.subTest(target=target), redirect_stdout(io.StringIO()):
                    self.assertEqual(demo.main([str(target)]), 2)
                self.assertEqual(list(root.iterdir()), before)
                self.assertEqual(keep.read_bytes(), b"keep exactly\x00\xff")

    def test_self_check_rejects_wrong_exit_code_or_output(self):
        demo = load_demo()
        for code, output in ((0, "OK example\n"), (1, "unexpected\n")):
            with self.subTest(code=code), patch.object(demo, "cli_main", return_value=code) as main:
                main.side_effect = lambda args, value=code, text=output: (print(text, end=""), value)[1]
                with redirect_stdout(io.StringIO()):
                    self.assertFalse(demo.check([], ["OK example"], 1))


if __name__ == "__main__":
    unittest.main()
