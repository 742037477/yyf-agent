"""Publish an append-only anchor file to a dedicated ledger-anchors clone."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from m2k.anchor import publish


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchors", type=Path, required=True)
    parser.add_argument("--repo-dir", type=Path, required=True)
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args(argv)
    try:
        commit = publish(args.anchors, args.repo_dir, args.push)
        print(f"copied {args.anchors}; committed {commit}")
        if args.push:
            print("pushed ledger-anchors to origin")
    except (OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
