"""After `m2k run` on GitHub: decide what may leave the task workspace. Standard library + m2k.

Exits 1 (and copies nothing) unless:
  1. adjudication with the config's OBS/Root inputs is PASS, and the Gate passed that same candidate;
  2. every file that differs from the pristine checkout matches the card's "Files you may change";
  3. each of those files in the workspace still has exactly its frozen-candidate hash.
With --apply it then copies those files into the pristine checkout and re-checks what landed.
Never runs git or any code inside the task workspace: run it with the pristine framework code.
Prints `branch=...`, `title=...` and `files=...` for the workflow.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import shutil
import sys
from pathlib import Path

from m2k.adjudicate import adjudicate
from m2k.cli import _load_config, adjudication_inputs
from m2k.ledger import Ledger
from m2k.tools import Workspace
from m2k.verify import freeze, freeze_one


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--workspace", required=True, help="folder the task ran in")
    ap.add_argument("--pristine", required=True, help="untouched checkout the workspace was copied from")
    ap.add_argument("--card", required=True, help="tasks/Txx-*.md, read from the pristine checkout")
    ap.add_argument("--apply", action="store_true", help="copy the approved files into the pristine checkout")
    a = ap.parse_args()
    card = Path(a.card).read_text(encoding="utf-8")
    patterns = re.findall(r"`([^`]+)`", re.search(r"\*\*Files you may change:\*\*(.+)", card).group(1))
    branch = re.search(r"\*\*Branch:\*\*\s*`([^`]+)`", card).group(1)
    title = card.splitlines()[0].lstrip("# ").strip()

    try:
        cfg, config_base, _ = _load_config(a.config)
        if "trust" not in cfg or "obs" not in cfg:
            raise ValueError("release requires config [trust] and [obs]")
        inputs = adjudication_inputs(cfg, config_base)
        with Ledger(Path(a.ledger)) as ledger:
            frozen = ledger.last(a.task_id, "candidate.frozen")
            gate = ledger.last(a.task_id, "gate.adjudication")
            adj = adjudicate(ledger, a.task_id, **inputs)
    except (OSError, ValueError, KeyError, SystemExit) as e:
        print(f"::error::{e}")
        return 1
    if frozen is None:
        sys.exit(f"::error::no verdict / candidate for {a.task_id} in the ledger")
    problems = [] if adj.verdict == "PASS" else [f"adjudication {adj.verdict}: {r}" for r in adj.reasons] or [
        f"adjudication {adj.verdict}"]
    if gate is None or gate.payload.get("verdict") != "PASS" or gate.payload.get("candidate") != frozen.payload.get("digest"):
        problems.append("no Gate PASS for the last frozen candidate")

    ws, pristine = Path(a.workspace), Path(a.pristine)
    cand, base = frozen.payload.get("files", {}), freeze(Workspace(pristine)).files
    changed = sorted(r for r in set(cand) | set(base) if cand.get(r) != base.get(r))
    for rel in changed:
        if not any(fnmatch.fnmatch(rel, p) for p in patterns):
            problems.append(f"changed outside the card: {rel}")
        elif rel not in cand:
            problems.append(f"deletions are not applied automatically: {rel}")
        elif not (ws / rel).is_file() or freeze_one(ws / rel) != cand[rel]:
            problems.append(f"{rel} in the workspace differs from the frozen candidate")
    if not changed:
        problems.append("the task changed no files")
    if not problems and a.apply:
        for rel in changed:
            (pristine / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ws / rel, pristine / rel)
            if freeze_one(pristine / rel) != cand[rel]:
                problems.append(f"{rel} did not land intact")
    print(f"branch={branch}\ntitle={title}\nfiles={' '.join(changed)}")
    for p in problems:
        print(f"::error::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
