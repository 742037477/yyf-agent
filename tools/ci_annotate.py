"""Re-emit unittest failures from a log file as GitHub annotations (readable via the checks API)."""
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
blocks = re.split(r"^={50,}$", text, flags=re.M)[1:]
for b in blocks[:20]:
    body = b.strip()
    head = body.splitlines()[0] if body else "failure"
    msg = body[-3500:].replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title={head[:120]}::{msg}")
if not blocks:
    tail = text[-3500:].replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title=step failed (no unittest failure blocks)::{tail}")
