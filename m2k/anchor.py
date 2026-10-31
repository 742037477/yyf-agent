"""Append-only snapshots of a ledger head; publish them outside worker access."""
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .ledger import GENESIS
from . import obs, trust


def head(ledger) -> dict:
    events = ledger.events()
    if not events:
        raise ValueError("empty ledger has no head")
    return {"seq": events[-1].seq, "hash": events[-1].hash,
            "events": len(events), "ledger_id": events[0].hash}


def append(path, ledger, note, receipt=None) -> dict:
    record = {"v": 1, "ts": datetime.now(timezone.utc).isoformat(), **head(ledger), "note": note}
    if receipt is not None:
        body = receipt["body"]
        record = dict(v=2, ts=body["ts"], ledger_id=body["ledger_id"], seq=body["ledger_seq"],
                      hash=body["ledger_hash"], note=note, receipt=receipt)
    path = Path(path)
    lines = path.read_bytes().splitlines(keepends=True) if path.exists() else []
    if lines and not lines[-1].endswith(b"\n"):
        raise ValueError("anchor file has an incomplete last line")
    record["prev"] = hashlib.sha256(lines[-1]).hexdigest() if lines else GENESIS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def check(path, ledger=None, obs_public_key=None, obs_log=None) -> list[str]:
    lines = (path if isinstance(path, bytes) else Path(path).read_bytes()).splitlines(keepends=True)
    events = ledger.events() if ledger is not None else []
    hashes = {e.seq: e.hash for e in events}
    ledger_id = events[0].hash if events else None
    problems, prev, foreign = [], GENESIS, False
    rows, errors = (obs.log_snapshot(obs_log) if obs_log is not None and not isinstance(obs_log, dict) else (obs_log, []))
    problems.extend(errors)
    for number, line in enumerate(lines, 1):
        try:
            record = json.loads(line.decode("utf-8"))
            if (type(record["v"]) is not int or record["v"] not in (1, 2) or record["prev"] != prev or not line.endswith(b"\n")
                    or type(record["seq"]) is not int or record["seq"] < 1
                    or (record["v"] == 2 and not isinstance(record.get("receipt"), dict))
                    or not all(isinstance(record[k], str) and re.fullmatch(r"[0-9a-f]{64}", record[k]) for k in ("ledger_id", "hash"))):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            problems.append(f"anchor file chain broken at line {number}")
            prev = hashlib.sha256(line).hexdigest()
            continue
        prev = hashlib.sha256(line).hexdigest()
        if record["v"] == 2 and obs_public_key is not None:
            try:
                body = obs.receipt_body(record["receipt"], obs_public_key, dict(ledger_id=record["ledger_id"],
                                        ledger_seq=record["seq"], ledger_hash=record["hash"], kind="anchor.checkpoint"))
                if (type(body["obs_seq"]) is not int or body["obs_seq"] < 1 or (rows is not None and
                        (rows.get(body["obs_seq"], {}).get("receipt") != record["receipt"]
                         or rows[body["obs_seq"]]["prev"] != body["obs_prev"]))):
                    raise ValueError("OBS log position differs")
            except (ValueError, KeyError, TypeError) as e:
                problems.append(f"anchor receipt at line {number}: {e}")
        if ledger is None:
            continue
        if ledger_id is not None and record["ledger_id"] != ledger_id:
            if not foreign:
                problems.append("anchors for a different ledger_id skipped")
                foreign = True
        elif not events or record["seq"] > events[-1].seq:
            problems.append(f"ledger truncated at anchored seq {record['seq']}")
        elif hashes.get(record["seq"]) != record["hash"]:
            problems.append(f"ledger rewritten at anchored seq {record['seq']}")
    return problems if lines else ["anchor file is empty"]


def git(repo, *args, raw=False):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            env=os.environ | {"LC_ALL": "C", "LANGUAGE": "C"})
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace")
        error = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/]+@", r"\1***@", error)
        error = re.sub(r"(?im)((?:authorization|extraheader)\s*[:=]\s*)[^\r\n]*", r"\1***", error)
        error = " | ".join(error[-300:].strip().splitlines())
        raise ValueError(f"git {args[0]} failed (exit {result.returncode}): {error}")
    return result.stdout if raw else result.stdout.decode("utf-8").strip()


def _integrate(repo):
    git(repo, "fetch", "origin", "+refs/heads/ledger-anchors:refs/remotes/origin/ledger-anchors")
    try:
        git(repo, "rebase", "refs/remotes/origin/ledger-anchors")
    except ValueError:
        try:
            git(repo, "rebase", "--abort")
        except ValueError:
            pass
        raise


def publish(anchors, repo, push=False):
    if git(repo, "symbolic-ref", "--short", "HEAD") != "ledger-anchors":
        raise ValueError("repo-dir must be on branch ledger-anchors")
    if git(repo, "status", "--porcelain"):
        raise ValueError("anchors clone must have a clean working tree")
    data = Path(anchors).read_bytes()
    if problems := check(data):
        raise ValueError("; ".join(problems))
    records = [json.loads(line) for line in data.splitlines()]
    ledger_id = records[-1]["ledger_id"]
    if any(r["ledger_id"] != ledger_id for r in records):
        raise ValueError("anchor file contains different ledger_ids")
    if push and git(repo, "ls-remote", "--heads", "origin", "ledger-anchors"):
        _integrate(repo)
    relative = f"anchors/{ledger_id[:12]}.jsonl"
    target = Path(repo) / relative
    existing = target.read_bytes() if target.exists() else b""
    if not data.startswith(existing):
        raise ValueError("anchor file is not an extension of the published one")
    if data != existing:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        git(repo, "add", "--", relative)
        git(repo, "commit", "-m", f"anchors: {ledger_id[:12]} up to seq {records[-1]['seq']}", "--", relative)
    if push:
        try:
            git(repo, "push", "origin", "ledger-anchors")
        except ValueError as e:
            if not any(s in str(e) for s in ("non-fast-forward", "fetch first", "fast-forwards")):
                raise
            _integrate(repo)
            git(repo, "push", "origin", "ledger-anchors")
    return git(repo, "rev-parse", "HEAD")


def activity(repo):
    origin = git(repo, "remote", "get-url", "origin")
    match = re.fullmatch(r"(?:https://(?:[^/@]*@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?", origin)
    if not match:
        raise ValueError("origin is not a GitHub repository URL")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        result = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                                capture_output=True, text=True, env=os.environ | {"GIT_TERMINAL_PROMPT": "0"})
        token = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line).get("password")
    if not token:
        raise ValueError("GitHub credential unavailable")
    entries = []
    try:
        for kind in ("force_push", "branch_deletion"):
            url = f"https://api.github.com/repos/{match[1]}/activity?ref=ledger-anchors&activity_type={kind}&per_page=100"
            while url:
                request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    page = json.load(response)
                    if not isinstance(page, list):
                        raise ValueError()
                    entries.extend(page)
                    link = re.search(r'<(https://api\.github\.com/[^>]+)>; rel="next"', response.headers.get("Link", ""))
                    url = link[1] if link else None
    except (OSError, ValueError, TypeError):
        raise ValueError("GitHub activity API unavailable") from None
    return entries


def remote_check(path, ledger, repo, obs_public_key, obs_log, this_ledger_only=False):
    problems = []
    try:
        if obs_public_key is None or obs_log is None:
            raise ValueError("remote check requires an OBS public key and log")
        notary = trust.fingerprint(trust._public(obs_public_key))
        git(repo, "fetch", "origin", "+refs/heads/ledger-anchors:refs/remotes/origin/ledger-anchors")
        # Check every parent diff; summaries include deletions of empty files.
        commit = None
        for line in git(repo, "log", "--numstat", "--no-renames", "--summary", "--format=%H",
                        "--diff-merges=separate", "refs/remotes/origin/ledger-anchors").splitlines():
            if re.fullmatch(r"[0-9a-f]{40,64}", line):
                commit = line
            elif line.startswith(" delete mode ") or ("\t" in line and line.split("\t", 2)[1] != "0"):
                problems.append(f"anchor history is not append-only: commit {commit}: {line.strip()}")
        files = git(repo, "ls-tree", "-r", "--name-only", "refs/remotes/origin/ledger-anchors", "--", "anchors/").splitlines()
        rows, errors = obs.log_snapshot(obs_log) if obs_log and Path(obs_log).exists() else ({}, [])
        problems.extend(errors)
        own = f"anchors/{ledger.ledger_id[:12]}.jsonl" if ledger.ledger_id else None
        if own not in files and ledger.events(kind="anchor.published"):
            problems.append(f"{own}: published anchor file missing")
        for file in files:
            if this_ledger_only and file != own:
                continue
            data = git(repo, "show", f"refs/remotes/origin/ledger-anchors:{file}", raw=True)
            if file == own:
                if path is None or not Path(path).exists() or not Path(path).read_bytes().startswith(data):
                    problems.append(f"{file}: published anchors are not a prefix of the local anchor file")
                problems.extend(f"{file}: {p}" for p in check(data, ledger, obs_public_key, rows))
            notaries = set()
            for number, line in enumerate(data.splitlines(), 1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError()
                    version = record["v"]
                    if version == 2:
                        previous = record["receipt"]["body"]["notary"]
                        if not isinstance(previous, str):
                            raise ValueError()
                except (ValueError, KeyError, TypeError):
                    problems.append(f"{file}: malformed anchor record at line {number}")
                    continue
                if version != 2:
                    continue
                if file == own:
                    if previous != notary and previous not in notaries:
                        notaries.add(previous)
                        problems.append(f"notary changed: {file} has anchors signed by {previous}, current OBS is {notary}")
                if this_ledger_only:
                    continue
                try:
                    body = obs.receipt_body(record["receipt"], obs_public_key)
                except (ValueError, KeyError, TypeError):
                    continue
                if rows.get(body["obs_seq"], {}).get("receipt") != record["receipt"]:
                    problems.append(f"OBS log replaced or truncated: obs_seq {body['obs_seq']} in {file}")
    except (OSError, ValueError, KeyError, TypeError) as e:
        problems.append(str(e))
    try:
        problems.extend(f"{r['activity_type']} by {r['actor']['login']} at {r['timestamp']}" for r in activity(repo)
                        if r["activity_type"] in ("force_push", "branch_deletion"))
    except (OSError, ValueError, KeyError, TypeError) as e:
        problems.append(str(e))
    return problems
