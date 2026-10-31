"""Owner acceptance for the Codex demo (T37). Exit code 0 = pass."""
import subprocess
import sys
from pathlib import Path

from wordstats import top_words

text = "The cat and the hat. THE end, and the END!"
assert top_words(text, 2) == [("the", 4), ("and", 2)], top_words(text, 2)
assert top_words(text, 3) == [("the", 4), ("and", 2), ("end", 2)], "ties are sorted alphabetically"
assert top_words("", 5) == []
assert top_words("don't stop", 5) == [("don't", 1), ("stop", 1)], "apostrophes stay inside words"
try:
    top_words("a", 0)
except ValueError:
    pass
else:
    raise AssertionError("n < 1 must raise ValueError")
sample = Path("sample.txt")
sample.write_text("b a b\nc b a\n", encoding="utf-8")
out = subprocess.run([sys.executable, "wordstats.py", str(sample), "2"], capture_output=True, text=True, encoding="utf-8")
assert out.returncode == 0 and out.stdout.replace("\r\n", "\n") == "b 3\na 2\n", (out.returncode, out.stdout, out.stderr)
sample.unlink()
print("all checks passed")
