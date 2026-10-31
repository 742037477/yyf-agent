"""离线生成带 OBS 回执的交付，再演示交付后改动的核验。"""
import argparse
import hashlib
import io
import json
import shlex
import sys
from contextlib import closing, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from m2k import obs, trust
from m2k.cli import main as cli_main
from m2k.contract import Acceptance, TaskContract
from m2k.kernel import Kernel
from m2k.ledger import Ledger
from m2k.models import ScriptedModel
from m2k.verify import scripted_review

TASKS = ("report-ok", "report-edited", "report-swapped", "report-forged")
CODE = "def add(a, b):\n    return a + b\n"
CHECK = "from calc import add\nassert add(2, 3) == 5\nprint('ok')\n"


def check(args, expected, expected_code):
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli_main(args)
    print(output.getvalue(), end="")
    print(f"退出码：{code}")
    return code == expected_code and output.getvalue().splitlines() == expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default="delivery-check-demo")
    root = Path(parser.parse_args(argv).directory).resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        print(f"拒绝：演示目录必须不存在或为空：{root}")
        return 2
    root.mkdir(parents=True, exist_ok=True)
    print("第 1 步：生成临时 OBS 密钥并启动本地旁证；账本、日志、私钥均在任务工作区之外。")
    key, public = trust.generate_keypair()
    key_path, log = root / "obs.pem", root / "obs.jsonl"
    key_path.write_bytes(key.private_bytes(trust.serialization.Encoding.PEM, trust.serialization.PrivateFormat.PKCS8,
                                           trust.serialization.NoEncryption()))
    command = [sys.executable, "-m", "m2k", "obs", "serve", "--key", str(key_path), "--log", str(log)]
    delivered, passed = root / "delivered", True
    with closing(obs.ObsClient(command, public, log_path=log)) as observer, Ledger(root / "ledger.sqlite") as ledger:
        print("第 2 步：脚本工人写 calc.py，运行所有者 check.py，并由独立脚本审查者给出 GO。")
        for name in TASKS:
            workspace = root / "workspaces" / name
            workspace.mkdir(parents=True)
            (workspace / "check.py").write_text(CHECK, encoding="utf-8")
            contract = TaskContract(name, "编写 calc.py 的 add(a, b)，通过所有者的 check.py。", workspace,
                                    Acceptance("{python} check.py", files=("check.py",)), deliver_to=delivered)
            steps = [json.dumps({"tool": "write_file", "args": {"path": "calc.py", "content": CODE}}),
                     json.dumps({"tool": "finish", "args": {}})]
            report = Kernel(ledger, ScriptedModel(steps, "demo-worker"),
                            ScriptedModel([scripted_review("GO")], "demo-reviewer"), obs=observer).run(contract)
            print(f"{name}: {report.reported_verdict.value}")
            passed &= report.reported_verdict.value == "PASS"
    if not passed:
        print("演示自检失败：任务未全部交付。")
        return 1
    standalone = ["check-delivery", str(delivered), "--standalone", "--public-key", public]
    print("第 3 步：刚交付时独立核验，预期 4 份全部通过。")
    pristine = [f"OK {name} (2 files)" for name in sorted(TASKS)] + ["合计 4 份：通过 4 份，未经核验 0 份"]
    passed &= check(standalone, pristine, 0)
    print("第 4 步：模拟交付之后的改动。")
    (delivered / "report-edited" / "calc.py").write_text("# 交付后被改写\n", encoding="utf-8")
    print("report-edited：改写 calc.py 的内容。")
    (delivered / "report-swapped" / "check.py").unlink()
    (delivered / "report-swapped" / "notes.txt").write_text("交付后新增的说明。\n", encoding="utf-8")
    print("report-swapped：删除 check.py，新增 notes.txt。")
    forged = delivered / "report-forged"
    (forged / "calc.py").write_text("# 改文件并试图补回执\n", encoding="utf-8")
    receipt_path = delivered / "report-forged.m2k-receipt.json"
    doc = json.loads(receipt_path.read_text(encoding="utf-8"))
    doc["event"]["payload"]["files"]["calc.py"] = hashlib.sha256((forged / "calc.py").read_bytes()).hexdigest()
    receipt_path.write_bytes(trust.canonical(doc))
    print("report-forged：改写 calc.py，并把回执中该文件的哈希改成新哈希。")
    (delivered / "hand-made").mkdir()
    (delivered / "hand-made" / "notes.txt").write_text("手工放入，没有回执。\n", encoding="utf-8")
    print("hand-made：手工新增文件夹和文件，没有回执。")
    print("report-ok：保持原样。")
    config = root / "check.toml"
    config.write_text('ledger = "ledger.sqlite"\n[obs]\nlog = "obs.jsonl"\npublic_key_b64 = '
                      + json.dumps(public) + '\n', encoding="utf-8")
    expected = ["未经核验 hand-made: missing receipt file",
                "未经核验 report-edited: changed file: calc.py",
                "未经核验 report-forged: receipt event hash mismatch",
                "OK report-ok (2 files)",
                "未经核验 report-swapped: missing file: check.py; extra file: notes.txt",
                "合计 5 份：通过 1 份，未经核验 4 份"]
    print("第 5 步：交付后再核验，分别打印独立模式和完整模式的结果。")
    print("独立模式：")
    passed &= check(standalone, expected, 1)
    print("完整模式：")
    passed &= check(["--config", str(config), "check-delivery", str(delivered)], expected, 1)
    print("第 6 步：公钥已写入 public-key.txt，可在仓库根目录自行再次核验。")
    (root / "public-key.txt").write_text(public + "\n", encoding="utf-8")
    ps_path = str(delivered).replace("'", "''")
    print(f"Windows（PowerShell）：python -m m2k check-delivery '{ps_path}' --standalone --public-key '{public}'")
    print(f"bash：python -m m2k check-delivery {shlex.quote(str(delivered))} --standalone --public-key {shlex.quote(public)}")
    print("演示自检通过。" if passed else "演示自检失败：核验结果与预期不符。")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
