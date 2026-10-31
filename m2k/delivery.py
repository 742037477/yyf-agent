"""Portable OBS delivery receipts and read-only checks of every delivery-root entry."""
import stat
from pathlib import Path

from . import obs, trust
from .adjudicate import adjudicate
from .ledger import Event, event_hash
from .verify import freeze_one

SUFFIX = ".m2k-receipt.json"


def delivery_entries(root):
    """Structural scan only: no file contents or ledger reads, and no links followed."""
    root = Path(root)
    if root.is_symlink() or getattr(root.lstat(), "st_reparse_tag", 0):
        raise ValueError("symlink delivery root")
    for entry in sorted(root.iterdir()):
        mode = entry.lstat().st_mode
        name = entry.name if stat.S_ISDIR(mode) else entry.name.removesuffix(SUFFIX)
        folder, receipt = entry.parent / name, entry.parent / (name + SUFFIX)
        problems = []
        if entry.name.startswith(".m2k-staging-"):
            problems.append("staging leftover")
        elif any(p.is_symlink() or (p.exists() and getattr(p.lstat(), "st_reparse_tag", 0)) for p in (folder, receipt)):
            problems.append("symlink in delivery pair")
        elif not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            problems.append("special file")
        elif not folder.is_dir():
            problems.append("missing delivery folder" if entry.name.endswith(SUFFIX) else "not a delivery folder")
        elif not receipt.is_file():
            problems.append("missing receipt file")
        yield entry.name, folder, receipt, problems


def _files(folder, problems):
    files = {}

    def walk(directory):
        for path in sorted(directory.iterdir()):
            rel, info = path.relative_to(folder).as_posix(), path.lstat()
            mode = info.st_mode
            if stat.S_ISDIR(mode) and not getattr(info, "st_reparse_tag", 0):
                walk(path)
            elif stat.S_ISREG(mode) and not getattr(info, "st_reparse_tag", 0):
                files[rel] = freeze_one(path)
            else:
                problems.append(f"symlink or special file: {rel}")
    walk(folder)
    return files


def check_delivery(root, obs_public_key, ledger=None, obs_log=None, **adjudication_inputs):
    """Return {entry: {files: count, problems: [...]}}; validate each pair only once."""
    if (ledger is None) != (obs_log is None):
        raise ValueError("full delivery checks require both the ledger and OBS log")
    common, snapshot = [], obs.log_snapshot(obs_log) if obs_log is not None else None
    if ledger is not None:
        ok, bad = ledger.verify_chain()
        if not ok:
            common.append(f"ledger hash chain broken at seq {bad}")
    results, checked = {}, {}
    for entry, folder, receipt_path, structural in delivery_entries(root):
        if folder not in checked:
            problems, count = list(common) + structural, 0
            if not structural:
                try:
                    doc = trust.loads_strict(receipt_path.read_text(encoding="utf-8"))
                    if set(doc) != {"schema", "ledger_id", "event", "receipt"} or doc["schema"] != "m2k.delivery/1":
                        raise ValueError("wrong delivery receipt schema")
                    event = Event(**doc["event"])
                    payload, name = event.payload, folder.name
                    if event_hash(event.prev_hash, event.task_id, event.kind, event.op_id,
                                  trust.canonical(payload).decode("utf-8"), event.ts) != event.hash:
                        problems.append("receipt event hash mismatch")
                    if event.kind != "delivery.result" or event.task_id != name:
                        problems.append("receipt event kind or task name mismatch")
                    if str(payload["target"]).replace("\\", "/").rstrip("/").split("/")[-1] != name:
                        problems.append("receipt target name mismatch")
                    if payload["post_verified"] is not True:
                        problems.append("delivery was not post-verified")
                    body = obs.receipt_body(doc["receipt"], obs_public_key, obs.request(event, doc["ledger_id"]))
                    actual, expected = _files(folder, problems), payload["files"]
                    count = len(actual)
                    for rel in sorted(set(actual) | set(expected)):
                        if rel not in actual or rel not in expected or actual[rel] != expected[rel]:
                            problem = "missing" if rel not in actual else "extra" if rel not in expected else "changed"
                            problems.append(f"{problem} file: {rel}")
                    if ledger is not None:
                        events = ledger.events(name)
                        if doc["ledger_id"] != ledger.ledger_id or not any(e.seq == event.seq and e.hash == event.hash for e in events):
                            problems.append("receipt ledger id or event mismatch")
                        if snapshot[0].get(body["obs_seq"], {}).get("receipt") != doc["receipt"]:
                            problems.append("receipt missing or different in OBS log")
                        gate = next((e for e in reversed(events) if e.kind == "gate.adjudication"), None)
                        if gate is None or gate.payload.get("verdict") != "PASS" or gate.payload.get("candidate") != payload["candidate"]:
                            problems.append("no Gate PASS for the delivery candidate")
                        adj = adjudicate(ledger, name, check_chain=False, obs_public_key=obs_public_key,
                                         obs_log=obs_log, _obs_snapshot=snapshot, **adjudication_inputs)
                        if adj.verdict != "PASS" or adj.candidate != payload["candidate"]:
                            problems.append(f"adjudication {adj.verdict}: {'; '.join(adj.reasons) or 'candidate mismatch'}")
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                    problems.append(f"invalid delivery receipt: {e}")
            checked[folder] = {"files": count, "problems": problems}
        results[entry] = checked[folder]
    return results
