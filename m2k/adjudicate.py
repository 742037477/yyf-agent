"""Read-only adjudication (T39, stage 5): recompute a task's verdict from the ledger alone.

The kernel's own PASS is only a claim. This module never writes: it reads the hash-chained ledger and checks that the
recorded facts support PASS — the chain is intact, the last frozen candidate is the one the verdict, the acceptance
and the review name; the owner's protected and acceptance files are unchanged in it; the acceptance passed; an
independent review said GO with its nonce; the credential covers this contract; every merged subtask adjudicates
PASS itself. Only a Gate releases anything, and a Gate releases only on PASS from here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import trust as trustlib
from .contract import in_scope
from .ledger import Ledger
from .obs import DECISIVE, FIELDS, log_snapshot, receipt_body, reconcile, request


@dataclass
class Adjudication:
    task_id: str
    verdict: str                      # PASS | FAIL | UNKNOWN
    candidate: str | None = None      # the frozen candidate digest the verdict is about
    reasons: list[str] = field(default_factory=list)


def descendants(ledger: Ledger, task_id: str):
    """Include every descendant attempt via task.submitted parent links, even failed epochs."""
    children = {}
    for tid in ledger.task_ids():
        for e in ledger.events(tid, "task.submitted"):
            children.setdefault(e.payload.get("parent"), set()).add(e.task_id)
    tasks, pending = set(), [task_id]
    while pending:
        tid = pending.pop()
        if tid not in tasks:
            tasks.add(tid)
            pending.extend(children.get(tid, ()))
    return tasks


def outside_changes(ledger: Ledger, task_id: str):
    return [e for tid in sorted(descendants(ledger, task_id)) for e in ledger.events(tid, "outside.changed")]


def successor_denial(ledger: Ledger, task_id: str, successor: str | None = None) -> str | None:
    if any(e.task_id != successor and (e.payload.get("amends") or {}).get("task") == task_id
           for e in ledger.events(kind="task.submitted")):
        return f"task already amended by another successor: {task_id}"
    return None


def amendment_basis(ledger: Ledger, task_id: str, successor: str | None = None):
    """The predecessor, continuation point and cumulative use, derived only from ledger facts."""
    old = ledger.last(task_id, "task.submitted")
    if old is None:
        raise ValueError(f"amended task missing: {task_id}")
    if old.payload.get("parent") is not None:
        raise ValueError(f"amended task has a parent: {task_id}")
    if "contract" not in old.payload:
        raise ValueError(f"amended task has no contract (predates T48): {task_id}")
    if ledger.last(task_id, "verdict") is None:
        raise ValueError(f"amended task has no verdict: {task_id}")
    if reason := successor_denial(ledger, task_id, successor):
        raise ValueError(reason)
    frozen = ledger.last(task_id, "candidate.frozen")
    if frozen is None:
        raise ValueError(f"amended task has no frozen candidate: {task_id}")
    tasks, seen, earlier = set(), set(), old
    while earlier is not None:
        if earlier.task_id in seen:
            raise ValueError("amends chain contains a cycle")
        seen.add(earlier.task_id)
        tasks.update(descendants(ledger, earlier.task_id))
        link = earlier.payload.get("amends")
        if not link:
            break
        earlier = ledger.last(link["task"], "task.submitted")
        if earlier is None:
            raise ValueError(f"amended task missing: {link['task']}")
    prior = {key: 0 for key in ledger.usage(task_id)}
    for tid in sorted(tasks):
        for key, value in ledger.usage(tid).items():
            prior[key] += value
    return old.payload, frozen.payload, (old.payload.get("amends") or {}).get("version", 1) + 1, prior


def owner_requests(ledger: Ledger, task_id: str):
    tasks = descendants(ledger, task_id)
    return [e for e in ledger.events(kind="owner.request") if e.task_id in tasks]


def approval_needs(ledger: Ledger, task_id: str, owner: dict) -> dict:
    """Owner approval concerns requests and increases against the previous owner contract."""
    previous = ledger.last(task_id, "task.submitted").payload
    old = previous["contract"]
    scope = {}
    for key, wider in (("tools", bool(set(owner["tools"]) - set(old["tools"]))),
                       ("network", owner["network"] and not old["network"]),
                       ("writes", bool(old["writes"]) and (not owner["writes"] or
                                   any(not in_scope(p, old["writes"]) for p in owner["writes"])))):
        if wider:
            scope[key] = [old[key], owner[key]]
    budget = (previous.get("amends") or {}).get("owner_budget", old["budget"])
    raises = {k: [v, owner["budget"][k]] for k, v in budget.items()
              if v is not None and (owner["budget"][k] is None or owner["budget"][k] > v)}
    oldp, newp = old.get("preauth") or {}, owner.get("preauth") or {}
    if (any(not in_scope(p, oldp.get("writes", [])) for p in newp.get("writes", []))
            or set(newp.get("tools", [])) - set(oldp.get("tools", [])) or newp.get("network", False) > oldp.get("network", False)
            or newp.get("budget_percent", 0) > oldp.get("budget_percent", 0)):
        scope["preauth"] = [old.get("preauth"), owner.get("preauth")]
    return {"requests": [e.seq for e in owner_requests(ledger, task_id)], "scope": scope, "budget": raises}


def preauth_limits(ledger, tid, current, before=float("inf")):
    chain, first = [], ledger.last(tid, "task.submitted").payload
    percent = (first["contract"].get("preauth") or {}).get("budget_percent", 0)
    while True:
        if first["contract"]["task_id"] in chain: raise ValueError("preauth amends cycle")
        chain.append(first["contract"]["task_id"])
        if not first.get("amends"): break
        first = ledger.last(first["amends"]["task"], "task.submitted").payload
    used = dict.fromkeys(current, 0)
    for e in (e for t in chain for e in ledger.events(t, "authority.granted") if e.seq < before):
        fields = e.payload.get("fields") if isinstance(e.payload, dict) else None
        pair = fields.get("budget", [{}, {}]) if isinstance(fields, dict) else None
        if (not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(b, dict) or
                any(v is not None and type(v) is not int for v in b.values()) for b in pair)):
            raise ValueError(f"authority.granted at seq {e.seq}: invalid fields.budget")
        for k in used: used[k] += max(0, (pair[1].get(k) or 0) - (pair[0].get(k) or 0))
    return {k: v + max(0, (first["contract"]["budget"][k] or 0) * percent // 100 - used[k]) for k, v in current.items() if v is not None}


def preauth_change(ledger, tid, current, request, before=float("inf")):
    from .worker import owner_request
    owner_request({k: v for k, v in request.items() if k != "by"})
    submitted = ledger.last(tid, "task.submitted").payload
    preauth = submitted["contract"].get("preauth")
    if preauth is None or submitted.get("parent") or current.get("subtasks") or current.get("plan"):
        raise ValueError("preauth unavailable")
    new, clause, req = dict(current), {}, request.get("request") or {}
    if request["kind"] == "scope":
        for k, v in req.items():
            if k == "writes" and (not current[k] or all(in_scope(p, current[k]) for p in v)): continue
            if ((k == "writes" and any(not in_scope(p, current[k]) and not in_scope(p, preauth.get(k, [])) for p in v))
                    or (k == "tools" and not set(v) <= set(preauth.get(k, [])))
                    or (k == "network" and v and not preauth.get(k, False))): raise ValueError("outside preauth scope")
            new[k] = current[k] or v if k == "network" else [*current[k], *(x for x in v if x not in current[k])]
            if new[k] != current[k]: clause[k] = preauth[k]
    elif request["kind"] == "budget":
        limits, budget = preauth_limits(ledger, tid, current["budget"], before), dict(current["budget"])
        for k, v in req.get("budget", {}).items():
            if budget[k] is None or v <= budget[k]: continue
            if v > limits[k]: raise ValueError("preauth budget allowance exceeded")
            budget[k], clause["budget_percent"] = v, preauth["budget_percent"]
        new["budget"] = budget
    else: raise ValueError("question has no preauth")
    return new, clause, {k: [current[k], v] for k, v in new.items() if v != current[k]}


def amendment_impact(ledger: Ledger, task_id: str, contract: dict, start_files: dict, **checks):
    """Reuse only an unchanged, verified prefix; never inspect earlier workspaces."""
    def resolve(version, sid, seen):
        if version in seen:
            return None
        seen.add(version)
        submitted = ledger.last(version, "task.submitted")
        if submitted is None:
            return None
        inherited = [e.payload for e in ledger.events(version, "subtask.reused") if e.payload.get("id") == sid]
        if inherited:
            link = submitted.payload.get("amends") or {}
            source = inherited[0].get("task_id")
            origin = resolve(link["task"], sid, seen) if link.get("task") else None
            if (len(inherited) == 1 and origin and origin[0] == source
                    and link.get("impact", {}).get("reused", {}).get(sid) == source):
                return origin
            return None
        finished = [e.payload for e in ledger.events(version, "subtask.finished")
                    if e.payload.get("id") == sid and e.payload.get("verdict") == "PASS"]
        source = finished[-1].get("task_id") if finished else None
        child = ledger.last(source, "task.submitted") if source else None
        frozen = ledger.last(source, "candidate.frozen") if source else None
        if child is None or child.payload.get("parent") != version or frozen is None:
            return None
        started = any(e.payload.get("id") == sid and e.payload.get("task_id") == source
                      for e in ledger.events(version, "subtask.started"))
        results = {e.op_id for e in ledger.events(version, "effect.result")
                   if e.payload.get("tool") == "merge_subtask" and e.payload.get("status") == "COMPLETED"
                   and e.payload.get("detail", {}).get("verified") is True}
        if started and any(e.op_id == f"merge-{sid}" and e.op_id in results
                           and e.payload.get("tool") == "merge_subtask" and e.payload.get("args", {}).get("subtask") == sid
                           and e.payload["args"].get("candidate") == frozen.payload["digest"]
                           for e in ledger.events(version, "effect.intent")):
            return source, submitted.payload["contract"], frozen
        return None

    old = ledger.last(task_id, "task.submitted").payload
    previous = old["contract"]
    old_files = ledger.last(task_id, "candidate.frozen").payload["files"]
    reused, rerun = {}, {}
    for s in contract.get("subtasks", []):
        sid, reason = s["id"], ""
        source, original, frozen = resolve(task_id, sid, set()) or (None, previous, None)
        subs = {s["id"]: s for s in original.get("subtasks", [])}
        changed_parent = [k for k in ("goal", "constraints", "protected") if original.get(k) != contract.get(k)]
        if original["acceptance"]["timeout_s"] != contract["acceptance"]["timeout_s"]:
            changed_parent.append("acceptance.timeout_s")
        if previous.get("plan") or contract.get("plan"):
            reason = "planned by a model"
        elif not previous.get("subtasks"):
            reason = "no owner-written subtasks in the earlier version"
        elif rerun:
            reason = "an earlier subtask reruns"
        elif {k: v for k, v in s.items() if k not in {"budget", "max_reassign"}} != {
                k: v for k, v in subs.get(sid, {}).items() if k not in {"budget", "max_reassign"}}:
            reason = "subtask changed"
        elif changed_parent:
            reason = "parent changed: " + ", ".join(changed_parent)
        elif frozen is None:
            reason = "not merged in the earlier version"
        elif adjudicate(ledger, source, check_chain=False, **checks).verdict != "PASS":
            reason = "earlier result does not adjudicate PASS"
        else:
            acceptance = ledger.last(source, "task.submitted").payload["acceptance_files"]
            allowed = set(contract["acceptance"]["files"]) - set(s["acceptance"]["files"])
            changed = sorted(p for p in start_files.keys() | old_files.keys()
                             if start_files.get(p) != old_files.get(p) and p not in allowed)
            if any(start_files.get(p) != h for p, h in acceptance.items()):
                reason = "acceptance file changed"
            elif {p: h for p, h in start_files.items() if in_scope(p, s["writes"])} != {
                    p: h for p, h in frozen.payload["files"].items() if in_scope(p, s["writes"])}:
                reason = "files in its write scope changed"
            elif changed:
                reason = "owner files changed: " + ", ".join(changed[:10])
        if reason:
            rerun[sid] = reason
        else:
            reused[sid] = source
    return {"reused": reused, "rerun": rerun}


def adjudicate(ledger: Ledger, task_id: str, check_chain: bool = True, obs_public_key=None,
               obs_log=None, _obs_snapshot=None, root_pin=None, revocations=None) -> Adjudication:
    out, unknown = Adjudication(task_id, "FAIL"), []
    problems = out.reasons
    if check_chain:
        ok, bad = ledger.verify_chain()
        if not ok:
            problems.append(f"ledger hash chain broken at seq {bad}")
    if obs_public_key is not None and obs_log is not None:
        if _obs_snapshot is None:
            _obs_snapshot = log_snapshot(obs_log)
        problems.extend(reconcile(ledger, task_id, obs_log, obs_public_key, snapshot=_obs_snapshot))
    last = {}
    for e in ledger.events(task_id):
        last[e.kind] = e
    for e in outside_changes(ledger, task_id):
        paths = sorted({p for k in ("added", "changed", "removed", "unverified") for p in e.payload.get(k, [])})
        problems.append(f"files changed outside the workspace in watched paths: {paths}")
    submitted, frozen, verdict = last.get("task.submitted"), last.get("candidate.frozen"), last.get("verdict")
    if submitted is None or frozen is None or verdict is None:
        problems.append("no submitted task, frozen candidate and verdict in the ledger")
        return out
    if "amends" in submitted.payload:
        try:
            amendment = submitted.payload["amends"]
            _, previous, version, prior = amendment_basis(ledger, amendment["task"], task_id)
            for key, value in {"version": version, "from_candidate": previous["digest"], "prior_usage": prior}.items():
                if amendment.get(key) != value:
                    problems.append(f"amends.{key} differs from the ledger: expected {value}")
            contract = submitted.payload["contract"]
            needs = approval_needs(ledger, amendment["task"], dict(contract, budget=amendment["owner_budget"]))
            approval = amendment.get("approval") or {}
            if any(needs.values()) and (approval.get("by") != "m2k answer" or approval.get("needs") != needs):
                problems.append("amends.approval missing or differs from owner approval needs")
            raised = ledger.events(task_id, "budget.raised")
            if ((needs["budget"] and (len(raised) != 1 or raised[0].payload.get("fields") != needs["budget"]))
                    or (raised and not needs["budget"])):
                problems.append("budget.raised missing, extra or fields differ from owner approval needs")
            if contract.get("subtasks") and "impact" not in amendment:
                problems.append("amends.impact missing for owner-written subtasks")
            if "impact" in amendment:
                start, impact = amendment["start_files"], amendment["impact"]
                if hashlib.sha256(json.dumps(start, sort_keys=True).encode()).hexdigest() != submitted.payload["baseline"]:
                    problems.append("amends.start_files differs from the baseline")
                expected = amendment_impact(ledger, amendment["task"], contract, start, obs_public_key=obs_public_key,
                                            obs_log=obs_log, _obs_snapshot=_obs_snapshot, root_pin=root_pin, revocations=revocations)
                if impact != expected:
                    problems.append("amends.impact differs from the ledger")
                reused = impact["reused"]
                events = [(e.payload["id"], e.payload["task_id"], e.payload["candidate"])
                          for e in ledger.events(task_id, "subtask.reused")]
                wanted = [(s["id"], reused[s["id"]], ledger.last(reused[s["id"]], "candidate.frozen").payload["digest"])
                          for s in contract["subtasks"] if s["id"] in reused]
                if events != wanted:
                    problems.append("subtask.reused events differ from amends.impact or contract order")
                if any(e.payload.get("id") in reused if e.kind == "subtask.started" else
                       e.payload.get("tool") == "merge_subtask" and
                       (e.payload.get("args", {}).get("subtask") or (e.op_id or "").removeprefix("merge-")) in reused
                       for e in ledger.events(task_id) if e.kind in {"subtask.started", "effect.intent", "effect.result"}):
                    problems.append("subtask.reused id also started or merged")
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            problems.append(f"amends: {e}")
    elif ledger.events(task_id, "budget.raised"):
        problems.append("budget.raised without an amendment budget raise")
    if "owner.request" in last:
        problems.append("awaiting owner")
    grants = ledger.events(task_id, "authority.granted")
    effective, effective_digest = submitted.payload.get("contract"), submitted.payload.get("contract_sha256")
    credentials = ledger.events(task_id, "credential.issued") if grants else []
    for event in grants:
        try:
            p = event.payload
            effective, clause, fields = preauth_change(ledger, task_id, effective, p["request"], event.seq)
            effective_digest = hashlib.sha256(json.dumps(effective, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
            if not fields or p["fields"] != fields or p["basis"] != {"by": "preauth", "clause": clause} or p["by"] not in ("worker", "framework") or p["contract_sha256"] != effective_digest:
                raise ValueError("basis, fields or contract_sha256 differs")
            if submitted.payload.get("trust") is not None:
                following = next((c for c in credentials if c.seq > event.seq), None)
                if following is None or following.payload["credential"]["body"]["contract_sha256"] != effective_digest:
                    raise ValueError("no credential.issued after grant covering its contract")
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
            problems.append(f"authority.granted at seq {event.seq}: {e}")
    if root_pin is not None and submitted.payload.get("parent") is None:
        release = submitted.payload.get("release")
        if release is None:
            problems.append("no release manifest recorded (run predates T64 or bypassed the CLI)")
        else:
            try:
                domain = submitted.payload["trust"]["domain"]
                body = trustlib.verify_release(root_pin, domain, release["envelope"], revocations=revocations)
                if body["config"] != release["config_sha256"]:
                    raise ValueError("RELEASE_CONFIG: recorded config digest differs from manifest")
                if not ((body["code"] == "dev" and domain == "TEST_ONLY") or
                        body["code"] == (submitted.payload.get("framework") or {}).get("code")):
                    raise ValueError("RELEASE_CODE: recorded framework code differs from manifest")
                checked_seq = release["checked_seq"]
                checked = (ledger.event(checked_seq) if isinstance(checked_seq, (int, float))
                           and 0 < checked_seq < 2**63 else None)
                if (checked is None or checked.task_id != "m2k.startup" or checked.kind != "release.checked"
                        or checked.seq >= submitted.seq or checked.payload.get("task") != task_id
                        or checked.payload["release"]["manifest_id"] != body["manifest_id"]):
                    raise ValueError("release.checked missing or names a different manifest or run")
                if obs_public_key is not None:
                    receipt = next((e.payload.get("receipt") for e in ledger.events("m2k.startup", "obs.receipt")
                                    if e.payload.get("receipt", {}).get("body", {}).get("ledger_seq") == checked.seq), None)
                    if receipt is None:
                        raise ValueError("no valid OBS receipt for release.checked")
                    signed = receipt_body(receipt, obs_public_key, request(checked, ledger.ledger_id))
                    if _obs_snapshot is not None:
                        row = _obs_snapshot[0].get(signed.get("obs_seq"), {})
                        if row.get("receipt") != receipt or row.get("prev") != signed.get("obs_prev"):
                            raise ValueError("release.checked OBS receipt missing or different in log")
            except (ValueError, KeyError, TypeError) as e:
                problems.append(f"release manifest: {e}")
    if obs_public_key is not None:
        witnessed = set()
        for event in ledger.events(task_id, "obs.receipt"):
            try:
                body = receipt_body(event.payload.get("receipt"), obs_public_key); witnessed.add(tuple(body[k] for k in FIELDS))
            except (ValueError, KeyError, TypeError): continue
        for kind in (*DECISIVE, "budget.raised"):
            if kind == "credential.issued" and submitted.payload.get("trust") is None: continue
            if (event := last.get(kind)) and tuple(request(event, ledger.ledger_id).values()) not in witnessed:
                problems.append(f"no valid OBS receipt for {kind} at seq {event.seq}")
        for event in grants + credentials:
            if tuple(request(event, ledger.ledger_id).values()) not in witnessed:
                problems.append(f"no valid OBS receipt for {event.kind} at seq {event.seq}")
    out.candidate = cand = frozen.payload["digest"]
    files = frozen.payload["files"]
    if grants and effective is not None:
        try:
            start = submitted.payload["start_files"]
            if not isinstance(start, dict) or hashlib.sha256(json.dumps(start, sort_keys=True).encode()).hexdigest() != submitted.payload["baseline"]:
                problems.append("authority.granted: initial files do not match baseline")
            elif effective["writes"] and any(files.get(p) != start.get(p) and not in_scope(p, effective["writes"]) for p in files.keys() | start.keys()):
                problems.append("files changed outside the final effective write scope")
            if any(v is not None and ledger.usage(task_id)[k.removeprefix("max_")] > v for k, v in effective["budget"].items()):
                problems.append("final effective budget exceeded")
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as e: problems.append(f"authority.granted at seq {grants[-1].seq}: invalid final authority or start_files: {e}")
    if verdict.payload.get("verdict") != "PASS":
        (unknown if verdict.payload.get("verdict") == "UNKNOWN" else problems).append(
            f"the kernel's verdict is {verdict.payload.get('verdict')}")
    if verdict.payload.get("candidate") != cand:
        problems.append("the verdict is not about the last frozen candidate")
    for rel, digest in (submitted.payload.get("protected") or {}).items():
        if files.get(rel) != digest:
            problems.append(f"protected or acceptance file changed in the candidate: {rel}")
    stopped = last.get("worker.stopped")
    if stopped is None or not (stopped.payload.get("status") == "FINISHED" or (
            stopped.payload.get("status") == "BUDGET_EXHAUSTED" and verdict.payload.get("verdict") == "PASS")):
        problems.append(f"the worker did not finish ({stopped and stopped.payload.get('status')})")
    test, review = last.get("test.result"), last.get("audit.result")
    if test is None or test.payload.get("candidate") != cand:
        problems.append("no acceptance result for the frozen candidate")
    elif test.payload.get("verdict") != "PASS" or test.payload.get("exit_code") != 0 or test.payload.get("reason"):
        (unknown if test.payload.get("verdict") == "UNKNOWN" else problems).append(
            f"acceptance {test.payload.get('verdict')} (exit {test.payload.get('exit_code')}) {test.payload.get('reason') or ''}".strip())
    if review is None or review.payload.get("candidate") != cand:
        problems.append("no independent review of the frozen candidate")
    elif review.payload.get("decision") != "GO" or not review.payload.get("nonce"):
        (unknown if review.payload.get("decision") not in ("GO", "NO_GO") else problems).append(
            f"review {review.payload.get('decision')}")
    trust = submitted.payload.get("trust")
    if trust is not None:
        cred = last.get("credential.issued")
        try: body = {**cred.payload["credential"]["body"]} if cred else {}
        except (KeyError, TypeError, IndexError): body = {}
        if (not grants and body.get("credential_id") != trust.get("credential_id")) or body.get("contract_sha256") != effective_digest:
            problems.append("the issued credential does not cover the submitted contract")
        if grants:
            try: fits = trustlib._grant_fits(effective["tools"], effective["budget"], effective["network"], dict(body.get("budget", {}), tools=body.get("tools"), network=body.get("network")))
            except (ValueError, KeyError, TypeError, AttributeError, IndexError): fits = False
            if not fits: problems.append("the issued credential does not cover final effective authority")
        if body.get("framework_sha256") != (submitted.payload.get("framework") or {}).get("digest"):
            problems.append("the credential framework_sha256 differs from the submitted framework digest")
    finished = {e.payload["id"]: e.payload for e in ledger.events(task_id, "subtask.finished")}
    for e in ledger.events(task_id, "effect.result"):
        if e.payload.get("tool") == "merge_subtask":
            sid = e.op_id.removeprefix("merge-")
            child = finished.get(sid, {})
            if not e.payload.get("detail", {}).get("verified") or child.get("verdict") != "PASS":
                problems.append(f"subtask {sid} was merged without a verified PASS")
            elif (sub := adjudicate(ledger, child["task_id"], check_chain=False, obs_public_key=obs_public_key,
                                   obs_log=obs_log, _obs_snapshot=_obs_snapshot,
                                   root_pin=root_pin, revocations=revocations)).verdict != "PASS":
                problems.append(f"subtask {sid} does not adjudicate PASS: {'; '.join(sub.reasons)}")
    out.verdict = "FAIL" if problems else "UNKNOWN" if unknown else "PASS"
    problems.extend(unknown)
    return out
