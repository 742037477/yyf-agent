"""Owner check for the whole T29 task: every part passes, and the tool uses only the standard library."""
import ast
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parents[1] / "examples" / "expenses"
for part in ("check_records.py", "check_summary.py", "check_cli.py"):
    r = subprocess.run([sys.executable, str(HERE / part)], capture_output=True, text=True, encoding="utf-8")
    print(r.stdout.strip())
    if r.returncode != 0:
        print(f"CHECK FAILED: {part} failed")
        raise SystemExit(1)
allowed = set(sys.stdlib_module_names) | {"records", "summary"}
for f in sorted(TOOL.glob("*.py")):
    for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
            [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
        for name in names:
            if name.split(".")[0] not in allowed:
                print(f"CHECK FAILED: {f.name} imports {name!r}; standard library only")
                raise SystemExit(1)
print("T29 ALL OK")
