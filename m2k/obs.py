"""Local independent witness: signed receipts and an append-only log; no authority is taken from the ledger."""
import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from . import trust
from .ledger import GENESIS

DECISIVE = ("task.submitted", "credential.issued", "candidate.frozen", "test.result", "audit.result", "verdict", "delivery.result")
FIELDS, SCHEMA = ("task_id", "kind", "ledger_id", "ledger_seq", "ledger_hash"), "m2k.obs.receipt/1"

class ObsError(ValueError):
    pass

class ObsRefused(ObsError):
    pass

def request(event, ledger_id):
    return dict(zip(FIELDS, (event.task_id, event.kind, ledger_id, event.seq, event.hash))) | (
        {"release": event.payload["release"]} if event.kind == "release.checked" else {})

def _shape(req):
    if (not isinstance(req, dict) or set(req) != set(FIELDS) | ({"release"} if req.get("kind") == "release.checked" else set())
            or type(req["ledger_seq"]) is not int or req["ledger_seq"] < 1
            or not all(isinstance(req[k], str) and req[k] for k in ("task_id", "kind", "ledger_id", "ledger_hash"))
            or not all(re.fullmatch(r"[0-9a-fA-F]{64}", req[k]) for k in ("ledger_id", "ledger_hash"))):
        raise ObsError("malformed OBS request")
    if req["kind"] == "release.checked":
        release = req["release"]
        if (not isinstance(release, dict) or set(release) != {"root", "domain", "slot", "version", "manifest_id", "revocations_version"}
                or not all(isinstance(release[k], str) and release[k] for k in ("root", "slot", "manifest_id"))
                or release["domain"] not in trust.DOMAINS or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", release["slot"])
                or type(release["version"]) is not int or release["version"] < 1
                or (release["revocations_version"] is not None and
                    (type(release["revocations_version"]) is not int or release["revocations_version"] < 0))):
            raise ObsError("malformed release.checked request")


def _release_versions(req, versions, revocations, roots, remember=False, rebuilding=False):
    if req["kind"] != "release.checked":
        return
    _shape(req)
    release = req["release"]
    domain, fingerprint = release["domain"], release["root"]
    if domain in roots and roots[domain] != fingerprint:
        if rebuilding:
            return   # Pre-T65 logs may contain other Roots; the first one wins.
        raise ObsRefused(f"root changed: {domain} first seen {roots[domain]}, now {fingerprint}")
    root = release["root"], release["domain"]
    slot, version, manifest = (*root, release["slot"]), release["version"], release["manifest_id"]
    highest, previous = versions.get(slot, (0, None))
    if version < highest:
        raise ObsRefused("release rollback")
    if version == highest and manifest != previous:
        raise ObsRefused(f"two manifests share slot {release['slot']} version {version}")
    revoked = release["revocations_version"]
    if revoked is not None and revoked < revocations.get(root, 0):
        raise ObsRefused("revocations rollback")
    if remember:
        roots.setdefault(domain, fingerprint)
        versions[slot] = version, manifest
        if revoked is not None:
            revocations[root] = revoked

def receipt_body(envelope, public_key_b64, req=None):
    key = trust._public(public_key_b64)
    body = trust.open_envelope(envelope, key)
    if (body.get("schema") != SCHEMA or body.get("notary") != trust.fingerprint(key)
            or (req is not None and any(body.get(k) != v for k, v in req.items()))):
        raise ObsError("OBS receipt does not match the notary or request")
    return body

def check_log(log_path, public_key_b64=None, *, rows=None):
    problems, prev, seq = [], GENESIS, 0
    try:
        lines = Path(log_path).read_bytes().splitlines(keepends=True)
    except OSError as e:
        return [str(e)]
    for number, raw in enumerate(lines, 1):
        try:
            row = trust.loads_strict(raw.decode("utf-8"))
            if (set(row) != {"v", "seq", "ts", "prev", "request", "receipt"} or type(row["v"]) is not int or row["v"] != 1
                    or type(row["seq"]) is not int or row["seq"] <= seq or row["prev"] != prev
                    or not isinstance(row["ts"], str) or not raw.endswith(b"\n")
                    or not isinstance(row["receipt"], dict) or set(row["receipt"]) != {"schema", "body", "signature_b64"}
                    or not isinstance(row["receipt"]["body"], dict) or not isinstance(row["receipt"]["signature_b64"], str)):
                raise ObsError("malformed line, broken prev chain or non-increasing seq")
            _shape(row["request"])
            if public_key_b64 is not None:
                body = receipt_body(row["receipt"], public_key_b64, row["request"])
                if body.get("obs_seq") != row["seq"] or body.get("obs_prev") != row["prev"]:
                    raise ObsError("receipt does not match the OBS log position")
            seq = row["seq"]
            if rows is not None:
                rows[seq] = row
        except (ValueError, KeyError, TypeError) as e:
            problems.append(f"OBS log line {number}: {e}")
        prev = hashlib.sha256(raw).hexdigest()
    return problems

def log_snapshot(log_path):
    """Read and parse once; share this snapshot only within one Gate decision."""
    rows = {}
    return rows, check_log(log_path, rows=rows)


def root_memory(log_path, public_key_b64):
    """Return the first Root per domain only after verifying the entire log."""
    trust._public(public_key_b64)
    rows, roots = {}, {}
    if not Path(log_path).exists():
        return roots
    if problems := check_log(log_path, public_key_b64, rows=rows):
        raise ObsError("; ".join(problems))
    for row in rows.values():
        if row["request"]["kind"] == "release.checked":
            release = row["request"]["release"]
            roots.setdefault(release["domain"], release["root"])
    return roots


def reconcile(ledger, task_id, log_path, public_key_b64, *, snapshot=None):
    """Check the chain and format; authenticate its tail, this task's rows and receipt references."""
    rows, errors = snapshot if snapshot is not None else log_snapshot(log_path)
    problems = list(errors)
    events = {e.seq: e for e in ledger.events(task_id)}
    receipts = [e.payload.get("receipt") for e in events.values() if e.kind == "obs.receipt"]
    related = {seq: row for seq, row in rows.items() if row["request"]["task_id"] == task_id}
    if rows:
        last = next(reversed(rows))
        related[last] = rows[last]
    for envelope in receipts:
        try:
            seq = envelope["body"]["obs_seq"]
            row = rows.get(seq)
            if row is not None:
                related[seq] = row
            receipt_body(envelope, public_key_b64)
            if row is None or row["receipt"] != envelope:
                problems.append(f"OBS receipt {seq} missing or different in log")
        except (ValueError, KeyError, TypeError):
            problems.append("malformed ledger OBS receipt")
    for seq, row in related.items():
        req = row["request"]
        try:
            body = receipt_body(row["receipt"], public_key_b64, req)
            if body.get("obs_seq") != seq or body.get("obs_prev") != row["prev"]:
                raise ObsError("receipt does not match the OBS log position")
        except (ValueError, KeyError, TypeError) as e:
            problems.append(f"OBS 日志被改动：OBS log seq {seq}: {e}")
        if req["task_id"] != task_id:
            continue
        if req["ledger_id"] != ledger.ledger_id:
            problems.append(f"OBS witnessed {task_id} in another ledger {req['ledger_id'][:12]}")
        event = events.get(req["ledger_seq"])
        if event is None or event.hash != req["ledger_hash"] or event.kind != req["kind"]:
            problems.append(f"OBS ledger event {req['ledger_seq']} missing or changed for {task_id}")
        if row["receipt"] not in receipts:
            problems.append(f"no ledger OBS receipt for log seq {seq} of {task_id}")
    return problems


def serve(key_path, log_path, stdin, stdout):
    key, path = trust.load_private_key(key_path), Path(log_path)
    lines = path.read_bytes().splitlines(keepends=True) if path.exists() else []
    # The tail's signed obs_prev authenticates every earlier raw line through this chain.
    seq, prev, last = 0, GENESIS, {}
    versions, revocations, roots = {}, {}, {}
    for raw in lines:
        row = json.loads(raw)
        if (row["prev"] != prev or type(row["seq"]) is not int or row["seq"] <= seq or not raw.endswith(b"\n")):
            raise ObsError("broken OBS log chain or non-increasing seq")
        seq, prev = row["seq"], hashlib.sha256(raw).hexdigest()
        last[row["request"]["ledger_id"]] = row["request"]["ledger_seq"]
        _release_versions(row["request"], versions, revocations, roots, remember=True, rebuilding=True)
    if lines:
        _shape(row["request"])
        body = receipt_body(row["receipt"], trust.public_key_b64(key), row["request"])
        if body.get("obs_seq") != seq or body.get("obs_prev") != row["prev"]:
            raise ObsError("receipt does not match the OBS log position")
    path.parent.mkdir(parents=True, exist_ok=True)
    for line in stdin:
        try:
            req = trust.loads_strict(line)
            _shape(req)
            if req["ledger_seq"] <= last.get(req["ledger_id"], 0):
                raise ObsError("ledger_seq replay or rollback")
            _release_versions(req, versions, revocations, roots)
            ts = datetime.now(timezone.utc).isoformat()
            response = trust.sign(dict(schema=SCHEMA, notary=trust.fingerprint(key.public_key()), strength="local",
                                      obs_seq=seq + 1, obs_prev=prev, ts=ts, **req), key)
            raw = trust.canonical(dict(v=1, seq=seq + 1, ts=ts, prev=prev, request=req, receipt=response)) + b"\n"
            with path.open("ab") as log:
                log.write(raw)
                log.flush()
                os.fsync(log.fileno())
            seq, prev, last[req["ledger_id"]] = seq + 1, hashlib.sha256(raw).hexdigest(), req["ledger_seq"]
            _release_versions(req, versions, revocations, roots, remember=True)
        except (OSError, ValueError, TypeError, KeyError) as e:
            response = {"error": str(e)}
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()

class ObsClient:
    def __init__(self, command, public_key_b64, timeout_s=10, log_path=None):
        self.log_path = log_path
        self.public_key_b64, self.timeout_s = public_key_b64, timeout_s
        self.notary, self.strength = trust.fingerprint(trust._public(public_key_b64)), "local"
        env = {k: v for k, v in os.environ.items() if k.upper() in "PATH SYSTEMROOT WINDIR TEMP TMP LOCALAPPDATA USERPROFILE".split()}
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env=env, close_fds=True, bufsize=0,
                                            cwd=Path(__file__).resolve().parent.parent)
        except (OSError, ValueError) as e:
            raise ObsError(str(e)) from e
        self.reader = ThreadPoolExecutor(1)

    def witness(self, event, ledger_id):
        try:
            req = request(event, ledger_id)
            self.process.stdin.write(trust.canonical(req) + b"\n")
            self.process.stdin.flush()
            raw = self.reader.submit(self.process.stdout.readline).result(self.timeout_s)
            envelope = trust.loads_strict(raw.decode("utf-8"))
            if req["kind"] == "release.checked" and isinstance(envelope, dict) and "error" in envelope:
                raise ObsRefused(envelope["error"])
            receipt_body(envelope, self.public_key_b64, req)
            return envelope
        except ObsRefused:
            raise
        except (OSError, ValueError, TypeError, RuntimeError, TimeoutError) as e:
            self.close()
            raise ObsError(f"witness unavailable: {e or type(e).__name__}") from e

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.reader.shutdown()
        self.process.stdin.close()
        self.process.stdout.close()
