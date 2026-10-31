"""Owner acceptance for card T46. The worker cannot change this file (it is a protected acceptance file).

Run from the repository root:  python tasks/T46_check.py --part N   (N = 1..6; the card is done in six runs)
Each part requires the attacks, defences and scenarios of all earlier parts too; part 6 is the whole card.

Defences are switched off by editing a temporary copy of the repository and running the attack tests there in a
separate process: the OBS witness is its own process, so an in-process mock would not reach it.
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULE = "tests.test_stage5_attack"
PARTS = {1: [1, 2, 3, 4, 5], 2: [6, 7, 8, 9, 17, 18], 3: [10, 11, 12, 21], 4: [13, 14, 15, 16, 19, 20], 5: [], 6: []}
MAY_BE_WEAK = set()
SCENARIOS = {
    5: [("绕过 Gate 往交付目录放文件", "check-delivery 列为未经核验，真交付仍 OK"),
        ("伪造账本尾巴冒充 PASS", "没有 OBS 回执，裁决 FAIL，不交付")],
    6: [("回滚到旧配置和旧清单", "旁证拒绝回退到旧版本（release rollback）"),
        ("删掉账本和 OBS 日志重来", "远端核对报 OBS log replaced or truncated"),
        ("整套换 Root（沿用同一 OBS）", "旁证拒绝换 Root（root changed）")],
}
BEFORE = "Worker 伪造字节码缓存劫持验收"

# label -> (part, attacks that must fail, [(file, exact text, replacement), ...])
DEFENCES = {
    "framework re-check off": (1, ["a01"], [
        ("m2k/kernel.py", 'if changed or before["digest"] != after["digest"]:', "if False:")]),
    "manifest config check off": (1, ["a02"], [
        ("m2k/trust.py", 'if config_bytes is not None and body.get("config") != config_digest(config_bytes):', "if False:")]),
    "witness rollback memory off": (1, ["a04"], [
        ("m2k/obs.py", "if version < highest:", "if False:"),
        ("m2k/obs.py", "if revoked is not None and revoked < revocations.get(root, 0):", "if False:")]),
    "manifest revocation off": (1, ["a05"], [
        ("m2k/trust.py", 'if body["manifest_id"] in revoked.get("revoked_releases", []):', "if False:")]),
    "required OBS receipts off": (2, ["a06"], [
        ("m2k/adjudicate.py",
         "if (event := last.get(kind)) and tuple(request(event, ledger.ledger_id).values()) not in witnessed:", "if False:")]),
    "ledger/OBS log reconciliation off": (2, ["a08"], [
        ("m2k/adjudicate.py", "problems.extend(reconcile(ledger, task_id, obs_log, obs_public_key, snapshot=_obs_snapshot))",
         "pass")]),
    "witness replay refusal off": (2, ["a09"], [
        ("m2k/obs.py", 'if req["ledger_seq"] <= last.get(req["ledger_id"], 0):', "if False:")]),
    "OBS failure verdict off": (2, ["a17"], [
        ("m2k/kernel.py", '            verdict = Verdict.UNKNOWN\n            reasons.append(f"OBS_UNAVAILABLE: {self.obs_failed}")',
         "            pass")]),
    "manifest config check off (notary swap)": (2, ["a18"], [
        ("m2k/trust.py", 'if config_bytes is not None and body.get("config") != config_digest(config_bytes):', "if False:")]),
    "anchor cross-checks off": (3, ["a10", "a11"], [
        ("m2k/anchor.py", 'if rows.get(body["obs_seq"], {}).get("receipt") != record["receipt"]:', "if False:"),
        ("m2k/anchor.py", 'problems.extend(f"{file}: {p}" for p in check(data, ledger, obs_public_key, rows))', "pass")]),
    "anchor history checks off": (3, ["a12"], [
        ("m2k/anchor.py", 'elif line.startswith(" delete mode ") or ("\\t" in line and line.split("\\t", 2)[1] != "0"):',
         "elif False:"),
        ("m2k/anchor.py", 'if r["activity_type"] in ("force_push", "branch_deletion"))', "if False)")]),
    "Root memory off": (3, ["a21"], [
        ("m2k/obs.py", "if domain in roots and roots[domain] != fingerprint:", "if False:")]),
    "delivery file comparison off": (4, ["a13", "a19"], [
        ("m2k/delivery.py", "if rel not in actual or rel not in expected or actual[rel] != expected[rel]:", "if False:")]),
    "receipt file required off": (4, ["a20"], [
        ("m2k/delivery.py", '            problems.append("missing receipt file")', "            pass"),
        ("m2k/delivery.py", "if not structural:", "if not structural and receipt_path.is_file():")]),
    "receipt event hash check off": (4, ["a14"], [
        ("m2k/delivery.py", 'problems.append("receipt event hash mismatch")', "pass")]),
    "deliver_to overlap check off": (4, ["a15"], [
        ("m2k/kernel.py", "if delivery.is_relative_to(workspace) or workspace.is_relative_to(delivery):", "if False:")]),
    "CI release adjudication off": (4, ["a16"], [
        ("tools/task_result.py", 'problems = [] if adj.verdict == "PASS" else', "problems = [] if True else"),
        ("tools/task_result.py",
         'if gate is None or gate.payload.get("verdict") != "PASS" or gate.payload.get("candidate") != frozen.payload.get("digest"):',
         "if False:")]),
}


class Recorder(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.passed = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed.append(test.id())


def fail(msg: str) -> None:
    print(f"T46 CHECK FAILED: {msg}")
    raise SystemExit(1)


def run_plain() -> Recorder:
    suite = unittest.defaultTestLoader.loadTestsFromName(MODULE)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0, resultclass=Recorder).run(suite)
    print(f"as written: {result.testsRun} tests, {len(result.failures)} failures, {len(result.errors)} errors, "
          f"{len(result.expectedFailures)} expected failures, {len(result.unexpectedSuccesses)} unexpected successes")
    for test, trace in result.failures + result.errors:
        print(f"--- {test.id()}\n{trace[-1200:]}")
    return result


def copy_repo(dest: Path) -> None:
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".m2k")
    for name in ("m2k", "tests", "tools", "trust", "tasks", "examples", "docs"):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, dest / name, ignore=skip)
    for name in ("pyproject.toml", "m2k.example.toml", "README.md", "AGENTS.md"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, dest / name)


def run_in_copy(edits, attacks) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "repo"
        copy_repo(copy)
        for rel, old, new in edits:
            path = copy / rel
            text = path.read_text(encoding="utf-8")
            if text.count(old) != 1:
                fail(f"defence edit does not apply exactly once in {rel}: {old[:70]!r} "
                     "(the defended code changed; the owner must update tasks/T46_check.py)")
            path.write_text(text.replace(old, new), encoding="utf-8", newline="")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        command = [sys.executable, "-m", "unittest"]
        for a in attacks:
            command += ["-k", f"test_{a}_"]
        return subprocess.run(command + [MODULE], cwd=copy, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)


def scenarios(part: int) -> None:
    wanted = [s for p in sorted(SCENARIOS) if p <= part for s in SCENARIOS[p]]
    # A relative --dir, like CI's default ".m2k": scenarios must not depend on an absolute base folder. Run from a
    # writable temporary folder (the task workspace is read-only inside the sandbox), with this repository's code.
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"} | {"PYTHONPATH": str(ROOT)}
        out = subprocess.run([sys.executable, "-m", "m2k", "accept", "--offline", "--dir", ".m2k"], cwd=tmp, env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(out.stdout[-3000:])
    lines = out.stdout.splitlines()
    before = next((i for i, line in enumerate(lines) if BEFORE in line), None)
    last = -1
    for name, expected in wanted:
        idx = next((i for i, line in enumerate(lines) if name in line), None)
        if idx is None or not lines[idx].lstrip().startswith("✅"):
            fail(f"offline scenario '{name}' missing or not ✅")
        if idx + 1 >= len(lines) or expected not in lines[idx + 1]:
            fail(f"expected text of '{name}' must be exactly: {expected}")
        if idx < last or before is None or idx > before:
            fail(f"'{name}' must come in the card's order, before '{BEFORE}' (in OFFLINE, before s_bytecode)")
        last = idx
    if out.returncode != 0:
        fail("m2k accept --offline: not every scenario passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--part", type=int, choices=sorted(PARTS), default=6)
    part = parser.parse_args().part
    attacks = [f"a{n:02d}" for p in sorted(PARTS) if p <= part for n in PARTS[p]]
    print(f"part {part}: attacks {', '.join(attacks)}")

    plain = run_plain()
    if plain.failures or plain.errors or plain.unexpectedSuccesses:
        fail("the attack tests do not pass")
    method = lambda test_id: test_id.rsplit(".", 1)[-1]
    passed = [method(t) for t in plain.passed]
    weak = [method(t.id()) for t, _ in plain.expectedFailures]
    for a in attacks:
        if not any(m.startswith(f"test_{a}_") for m in passed + weak):
            fail(f"no test for attack {a} (a test method named test_{a}_<name>)")
        if any(m.startswith(f"test_{a}_") for m in weak) and a not in MAY_BE_WEAK:
            fail(f"attack {a} is marked expectedFailure; only {sorted(MAY_BE_WEAK)} may be")
    if weak:
        print("FINDINGS (expectedFailure; the owner decides):", ", ".join(weak))

    for label, (p, targets, edits) in DEFENCES.items():
        if p > part:
            continue
        result = run_in_copy(edits, targets)
        ran = "Ran 0 tests" not in result.stderr
        print(f"{label}: {'tests fail as required' if result.returncode != 0 and ran else 'tests still pass'}")
        if not ran:
            fail(f"no test ran for {targets} in the copy with the {label}")
        if result.returncode == 0:
            fail(f"{', '.join(targets)} still pass with the {label}: they do not exercise that defence")

    if part >= 5:
        scenarios(part)
    full = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(full.stderr[-1500:])
    if full.returncode != 0:
        fail("full test suite fails")
    print(f"T46 PART {part} CHECK OK" if part < 6 else "T46 CHECK OK")


if __name__ == "__main__":
    main()
