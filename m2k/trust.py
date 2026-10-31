"""Trust chain: Root (offline) → framework charter → task credential → contract. Old project's format:
{schema, body, signature_b64}, Ed25519 over canonical JSON of body (ints only; no floats, duplicate keys or BOM)."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .contract import child_task_id, in_scope

DOMAINS = ("TEST_ONLY", "PRODUCTION")
CHARTER, REVOCATIONS, CREDENTIAL = "m2k.charter/1", "m2k.revocations/1", "m2k.credential/1"
RELEASE = "m2k.release/2"
BUDGET_KEYS = ("max_rounds", "max_model_calls", "max_tokens", "max_commands")


class TrustError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _fail(why: str):
    raise TrustError("FORMAT", why)


def loads_strict(text: str) -> Any:
    """Like json.loads, but a BOM, duplicate keys, floats and NaN/Infinity are errors."""
    if text.startswith("\ufeff"):
        _fail("BOM is not allowed")
    unique = lambda pairs: dict(pairs) if len({k for k, _ in pairs}) == len(pairs) else _fail("duplicate keys")
    return json.loads(text, object_pairs_hook=unique, parse_float=lambda t: _fail(f"float {t}"),
                      parse_constant=lambda t: _fail(f"constant {t}"))


def canonical(obj: Any) -> bytes:
    loads_strict(json.dumps(obj, ensure_ascii=False))   # same rules on the way out: no floats, no NaN
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _public(b64: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(b64, validate=True)
        if len(raw) != 32:
            raise ValueError("not 32 bytes")
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as e:
        raise TrustError("KEY", f"bad Ed25519 public key: {e}") from e


def _raw(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(key: Ed25519PublicKey) -> str:
    return hashlib.sha256(_raw(key)).hexdigest()[:16]


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    return _b64(_raw(private_key.public_key()))


def generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    return key, public_key_b64(key)


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as e:
        raise TrustError("KEY", f"cannot load private key: {path}") from e
    if not isinstance(key, Ed25519PrivateKey):
        raise TrustError("KEY", f"{path} is not an Ed25519 private key")
    return key


def sign(body: dict, private_key: Ed25519PrivateKey) -> dict:
    return {"schema": body["schema"], "body": body, "signature_b64": _b64(private_key.sign(canonical(body)))}


def open_envelope(env: Any, public_key: Ed25519PublicKey, code: str = "SIGNATURE") -> dict:
    """The body if the envelope is well-formed and signed by public_key; otherwise TrustError(code)."""
    if not isinstance(env, dict) or set(env) != {"schema", "body", "signature_b64"} or not isinstance(env["body"], dict):
        raise TrustError(code, "envelope must be exactly {schema, body, signature_b64}")
    if env["schema"] != env["body"].get("schema"):
        raise TrustError(code, "envelope schema differs from body schema")
    try:
        public_key.verify(base64.b64decode(env["signature_b64"], validate=True), canonical(env["body"]))
    except (InvalidSignature, ValueError, TypeError) as e:
        raise TrustError(code, "signature does not verify") from e
    return env["body"]


@dataclass(frozen=True)
class Pin:
    public_key: Ed25519PublicKey
    trust_domain: str
    fingerprint: str

def read_json(path: str | Path):
    try:
        data = loads_strict(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict): _fail("expected a JSON object")
        return data
    except (OSError, ValueError) as e:
        raise TrustError("FORMAT", f"cannot read JSON: {path}") from e


_read_json = read_json


def load_pin(path: str | Path) -> Pin:
    data = _read_json(path)
    key = _public(data.get("public_key_b64", ""))
    if data.get("trust_domain") not in DOMAINS:
        raise TrustError("DOMAIN", f"pin trust_domain must be one of {DOMAINS}")
    fp = fingerprint(key)
    full = hashlib.sha256(_raw(key)).hexdigest()   # the old project's pins carry the full sha256 as "fingerprint"
    for field, allowed in (("fingerprint", (fp, full)), ("public_key_sha256", (full,))):
        if field in data and data[field] not in allowed:
            raise TrustError("ROOT_PIN", f"pin {field} does not match the key")
    return Pin(key, data["trust_domain"], fp)

@dataclass(frozen=True)
class TrustSetup:
    domain: str
    pin: Pin
    charter_env: dict
    revocations_path: Path | None
    issuer_key_path: Path
    ttl_s: int
    def revocations(self): return _read_json(self.revocations_path) if self.revocations_path is not None else None
    def issuer_key(self): return load_private_key(self.issuer_key_path)

def dev_setup(folder: str | Path, caps: dict, ttl_s: int = 600) -> tuple[TrustSetup, Ed25519PrivateKey]:
    """Fresh TEST_ONLY Root, issuer, charter and an empty revocation list in folder, for tests and accept drills."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    root, root_b64 = generate_keypair()
    issuer, issuer_b64 = generate_keypair()
    (folder / "issuer.pem").write_bytes(issuer.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                             serialization.NoEncryption()))
    now = int(time.time())
    (folder / "revocations.json").write_bytes(canonical(sign({"schema": REVOCATIONS, "trust_domain": "TEST_ONLY", "version": 0,
        "revoked_charters": [], "revoked_credentials": [], "issued_at": now}, root)))
    charter = sign({"schema": CHARTER, "trust_domain": "TEST_ONLY", "charter_id": secrets.token_hex(8), "caps": caps,
                    "issuer_public_key_b64": issuer_b64, "release": "dev", "issued_at": now}, root)
    pin = Pin(root.public_key(), "TEST_ONLY", fingerprint(root.public_key()))
    return TrustSetup("TEST_ONLY", pin, charter, folder / "revocations.json", folder / "issuer.pem", ttl_s), root


def load_setup(section: dict, base_dir: Path) -> TrustSetup:
    try:
        domain, ttl = section["domain"], section.get("credential_ttl_s", 3600)
        if domain not in DOMAINS:
            raise TrustError("DOMAIN", f"unknown trust domain: {domain}")
        if type(ttl) is not int or ttl <= 0: _fail("credential_ttl_s must be a positive integer")
        pin, charter, issuer = (base_dir / section[k] for k in ("root_pin", "charter", "issuer_key"))
        load_private_key(issuer)
        return TrustSetup(domain, load_pin(pin), _read_json(charter),
                          base_dir / section["revocations"] if "revocations" in section else None, issuer, ttl)
    except (KeyError, TypeError, AttributeError) as e:
        raise TrustError("FORMAT", f"invalid [trust] configuration: {e}") from e

def release_digest(code_dir: str | Path) -> str:
    root = Path(code_dir)
    lines = sorted(f"{p.relative_to(root).as_posix()}:{hashlib.sha256(p.read_bytes()).hexdigest()}"
                   for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def config_digest(config_bytes: bytes) -> str:
    return hashlib.sha256(config_bytes.replace(b"\r\n", b"\n")).hexdigest()


def verify_release(pin, domain, release_env, code_dir=None, config_bytes=None, revocations=None):
    body = open_envelope(release_env, pin.public_key, "RELEASE_SIGNATURE")
    if (set(body) != {"schema", "trust_domain", "manifest_id", "slot", "version", "code", "config", "issued_at"}
            or body.get("schema") != RELEASE or domain not in DOMAINS
            or pin.trust_domain != domain or body.get("trust_domain") != domain
            or not isinstance(body.get("manifest_id"), str) or not body["manifest_id"]
            or not isinstance(body.get("slot"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", body["slot"])
            or type(body.get("version")) is not int or body["version"] < 1
            or not all(isinstance(body.get(k), str) for k in ("code", "config"))
            or not re.fullmatch(r"[0-9a-f]{64}", body["config"])
            or (body["code"] != "dev" and not re.fullmatch(r"[0-9a-f]{64}", body["code"]))
            or type(body.get("issued_at")) is not int):
        raise TrustError("RELEASE_SIGNATURE", "release schema or trust domain differs")
    if body["code"] == "dev" and domain != "TEST_ONLY":
        raise TrustError("RELEASE_CODE", "PRODUCTION cannot use a dev release")
    if revocations is not None:
        revoked = open_envelope(revocations, pin.public_key, "REVOCATIONS_SIGNATURE")
        if (revoked.get("schema") != REVOCATIONS or revoked.get("trust_domain") != domain
                or type(revoked.get("version")) is not int or revoked["version"] < 0
                or not isinstance(revoked.get("revoked_releases", []), list)):
            raise TrustError("REVOCATIONS_SIGNATURE", "invalid revocation list")
        if body["manifest_id"] in revoked.get("revoked_releases", []):
            raise TrustError("RELEASE_REVOKED", f"manifest {body['manifest_id']} is revoked")
    if code_dir is not None and not ((body.get("code") == "dev" and domain == "TEST_ONLY")
            or body.get("code") == release_digest(code_dir)):
        raise TrustError("RELEASE_CODE", "the manifest does not cover this framework code")
    if config_bytes is not None and body.get("config") != config_digest(config_bytes):
        raise TrustError("RELEASE_CONFIG", "the manifest does not cover this config")
    return body


def check_release(section, base_dir, code_dir, config_bytes):
    try:
        path = (base_dir / section["release"]).absolute()
        raw = path.read_bytes()
        env = loads_strict(raw.decode("utf-8"))
        pin, domain = load_pin(base_dir / section["root_pin"]), section["domain"]
    except (KeyError, OSError, ValueError, TypeError) as e:
        raise TrustError("RELEASE_SIGNATURE", "missing or unreadable [trust] release manifest") from e
    if domain == "PRODUCTION" and not section.get("revocations"):
        raise TrustError("REVOCATIONS_REQUIRED", "PRODUCTION requires [trust] revocations")
    revocations = _read_json(base_dir / section["revocations"]) if section.get("revocations") else None
    verify_release(pin, domain, env, code_dir, config_bytes, revocations)
    return {path: raw}


def _grant_fits(tools, budget: dict, network: bool, limit: dict) -> bool:
    if (not isinstance(limit["tools"], list) or not isinstance(limit["network"], bool)   # fully explicit limits:
            or any(type(limit[k]) is not int for k in BUDGET_KEYS)):                     # never 'unlimited'
        raise TypeError("caps must list tools, a bool network and an int for every budget key")
    return (set(tools) <= set(limit["tools"]) and (limit["network"] or not network)
            and all(type(budget.get(k)) is int and budget[k] <= limit[k] for k in BUDGET_KEYS))


def _contract_grant(contract) -> tuple[list[str], dict, bool]:
    return list(contract.tools), {k: getattr(contract.budget, k) for k in BUDGET_KEYS}, bool(contract.network)


def issue_credential(charter: dict, issuer_private_key: Ed25519PrivateKey, contract, now: int, ttl_s: int,
                     framework_sha256: str | None = None) -> dict:
    try:
        fits = _grant_fits(*_contract_grant(contract), charter["caps"])
    except (KeyError, TypeError) as e:
        raise TrustError("FORMAT", f"malformed charter caps: {e}") from e
    if not fits:
        raise TrustError("EXCEEDS_CHARTER", "the contract asks for more than the charter allows")
    tools, budget, network = _contract_grant(contract)
    return sign({"schema": CREDENTIAL, "trust_domain": charter["trust_domain"], "charter_id": charter["charter_id"],
                 "credential_id": secrets.token_hex(16), "task_id": contract.task_id,
                 "contract_sha256": contract.digest(), "tools": tools, "budget": budget, "network": network,
                 "not_before": now, "expires_at": now + ttl_s,
                 **({"framework_sha256": framework_sha256} if framework_sha256 is not None else {})}, issuer_private_key)


def verify_chain(pin: Pin, domain: str, charter_env: dict, credential_env: dict, revocations_env: dict | None,
                 contract, now: int, code_dir: str | Path) -> dict:
    """Root → charter → (revocations) → credential → contract. Returns the credential body or raises TrustError."""
    try:
        return _verify_chain(pin, domain, charter_env, credential_env, revocations_env, contract, now, code_dir)
    except (KeyError, TypeError, AttributeError) as e:   # a signed document with missing or mistyped fields
        raise TrustError("FORMAT", f"malformed signed document: {type(e).__name__}: {e}") from e

def verify_charter(pin, domain, charter_env, revocations_env, code_dir) -> tuple[dict, dict]:
    try: return _verify_charter(pin, domain, charter_env, revocations_env, code_dir)
    except (KeyError, TypeError, AttributeError) as e:
        raise TrustError("FORMAT", f"malformed signed document: {e}") from e

def _verify_charter(pin, domain, charter_env, revocations_env, code_dir):
    domains = [pin.trust_domain] + [e.get("body", {}).get("trust_domain") if isinstance(e, dict) else None
                                    for e in (charter_env, revocations_env) if e is not None]
    if domain not in DOMAINS or any(d != domain for d in domains):
        raise TrustError("DOMAIN", f"everything must belong to {domain}; got {domains}")
    charter = open_envelope(charter_env, pin.public_key, "CHARTER_SIGNATURE")
    revoked = {"revoked_charters": [], "revoked_credentials": []}
    if revocations_env is not None:
        revoked = open_envelope(revocations_env, pin.public_key, "REVOCATIONS_SIGNATURE")
    if charter.get("schema") != CHARTER or charter["charter_id"] in revoked["revoked_charters"]:
        raise TrustError("CHARTER_REVOKED", f"charter {charter.get('charter_id')} is revoked or not a charter")
    release = charter.get("release")
    if not ((release == "dev" and domain == "TEST_ONLY") or release == release_digest(code_dir)):
        raise TrustError("RELEASE", "the charter does not cover this framework code")
    try:
        _public(charter["issuer_public_key_b64"])
    except TrustError as e:
        raise TrustError("FORMAT", "malformed charter issuer public key") from e
    return charter, {k: revoked[k] for k in ("revoked_charters", "revoked_credentials")}

def _verify_chain(pin, domain, charter_env, credential_env, revocations_env, contract, now, code_dir) -> dict:
    if not isinstance(credential_env, dict) or credential_env.get("body", {}).get("trust_domain") != domain:
        raise TrustError("DOMAIN", f"credential must belong to {domain}")
    charter, revoked = verify_charter(pin, domain, charter_env, revocations_env, code_dir)
    cred = open_envelope(credential_env, _public(charter["issuer_public_key_b64"]), "CREDENTIAL_SIGNATURE")
    if cred.get("schema") != CREDENTIAL:
        raise TrustError("CREDENTIAL_SIGNATURE", "not a credential")
    checks = [("CREDENTIAL_REVOKED", lambda: cred["credential_id"] not in revoked["revoked_credentials"]),
              ("WRONG_PARENT", lambda: "parent_credential_id" not in cred),   # a child credential never stands alone
              ("WRONG_CHARTER", lambda: cred["charter_id"] == charter["charter_id"]),
              ("NOT_YET_VALID", lambda: now >= cred["not_before"]), ("EXPIRED", lambda: now < cred["expires_at"]),
              ("WRONG_TASK", lambda: cred["task_id"] == contract.task_id),
              ("CONTRACT_MISMATCH", lambda: cred["contract_sha256"] == contract.digest()),
              ("EXCEEDS_CHARTER", lambda: _grant_fits(cred["tools"], cred["budget"], cred["network"], charter["caps"])),
              ("EXCEEDS_CREDENTIAL", lambda: _grant_fits(*_contract_grant(contract), cred["budget"] | {
                  "tools": cred["tools"], "network": cred["network"]}))]
    _check(checks, cred["credential_id"])
    return cred


def _check(checks, credential_id) -> None:
    for code, ok in checks:   # in order: the first failing check names the reason
        if not ok():
            raise TrustError(code, f"credential {credential_id} failed {code}")


# ---- child credentials (stage 3): Root → charter → parent credential → child credential → child contract ----
def _child_checks(parent: dict, parent_contract, cred: dict, contract, now: int, revoked, plan=None) -> list:
    sub = next((s for s in (parent_contract.subtasks if plan is None else plan) if s.id == cred["subtask"]), None)
    if not isinstance(cred["writes"], list) or not all(isinstance(w, str) for w in cred["writes"]):
        _fail("child writes must be a list of strings")
    mine, writes = (cred["tools"], cred["budget"], cred["network"]), tuple(cred["writes"])
    top = dict(parent["budget"], tools=parent["tools"], network=parent["network"])
    plan = sub and dict({k: getattr(sub.budget, k) for k in BUDGET_KEYS}, tools=list(sub.tools), network=sub.network)
    return [("CREDENTIAL_REVOKED", lambda: cred["credential_id"] not in revoked),
            ("FRAMEWORK_CHANGED", lambda: cred.get("framework_sha256") == parent.get("framework_sha256")),
            ("WRONG_PARENT", lambda: sub is not None and cred["parent_credential_id"] == parent["credential_id"]
                                     and cred["charter_id"] == parent["charter_id"]),
            ("NOT_YET_VALID", lambda: now >= cred["not_before"]), ("EXPIRED", lambda: now < cred["expires_at"]),
            ("WRONG_TASK", lambda: type(cred["epoch"]) is int and cred["epoch"] >= 1
                                   and cred["task_id"] == contract.task_id == child_task_id(parent_contract.task_id, sub.id, cred["epoch"])),
            ("CONTRACT_MISMATCH", lambda: cred["contract_sha256"] == contract.digest()),
            ("EXCEEDS_PARENT", lambda: _grant_fits(*mine, top) and cred["expires_at"] <= parent["expires_at"] and bool(writes)
                                       and (not parent_contract.writes or all(in_scope(w, parent_contract.writes) for w in writes))),
            ("EXCEEDS_PLAN", lambda: _grant_fits(*mine, plan) and writes == tuple(sub.writes)),
            ("EXCEEDS_CREDENTIAL", lambda: _grant_fits(*_contract_grant(contract), dict(cred["budget"], tools=cred["tools"],
                                                       network=cred["network"])) and tuple(contract.writes) == writes)]


def issue_child_credential(issuer_private_key: Ed25519PrivateKey, parent: dict, parent_contract, contract,
                           subtask_id: str, now: int, epoch: int = 1, plan=None) -> dict:
    """The framework issues a subtask its own credential; it refuses anything wider than the parent and its plan."""
    tools, budget, network = _contract_grant(contract)
    body = {"schema": CREDENTIAL, "trust_domain": parent["trust_domain"], "charter_id": parent["charter_id"],
            "credential_id": secrets.token_hex(16), "parent_credential_id": parent["credential_id"], "subtask": subtask_id, "epoch": epoch,
            "task_id": contract.task_id, "contract_sha256": contract.digest(), "tools": tools, "budget": budget,
            "network": network, "writes": list(contract.writes), "not_before": now, "expires_at": parent["expires_at"]}
    if "framework_sha256" in parent:
        body["framework_sha256"] = parent["framework_sha256"]
    try:
        _check(_child_checks(parent, parent_contract, body, contract, now, (), plan), body["credential_id"])
    except (KeyError, TypeError, AttributeError) as e:
        raise TrustError("FORMAT", f"cannot issue a child credential: {type(e).__name__}: {e}") from e
    return sign(body, issuer_private_key)


def verify_child_chain(pin: Pin, domain: str, charter_env: dict, parent_env: dict, child_env: dict,
                       revocations_env: dict | None, parent_contract, contract, now: int, code_dir: str | Path,
                       plan=None) -> dict:
    """The parent chain first (failures re-raised as PARENT_<code>), then the child. Returns the child body."""
    try:
        parent = verify_chain(pin, domain, charter_env, parent_env, revocations_env, parent_contract, now, code_dir)
    except TrustError as e:
        raise TrustError("PARENT_" + e.code, str(e)) from e
    try:
        charter, revoked = verify_charter(pin, domain, charter_env, revocations_env, code_dir)
        cred = open_envelope(child_env, _public(charter["issuer_public_key_b64"]), "CREDENTIAL_SIGNATURE")
        if cred.get("schema") != CREDENTIAL or cred.get("trust_domain") != domain:
            raise TrustError("CREDENTIAL_SIGNATURE", "not a credential of this trust domain")
        _check(_child_checks(parent, parent_contract, cred, contract, now, revoked["revoked_credentials"], plan),
               cred["credential_id"])
        return cred
    except (KeyError, TypeError, AttributeError) as e:
        raise TrustError("FORMAT", f"malformed child credential: {type(e).__name__}: {e}") from e
