"""The governance chain for one task:

submit -> worker loop (G3 on every action) -> G4 freeze -> TEST -> independent AUDIT
       -> verdict (PASS / FAIL / UNKNOWN) -> G6 delivery gate -> G7 post-delivery check

Only PASS is ever delivered. FAIL and UNKNOWN keep the workspace as-is for diagnosis.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import anchor, trust as trustlib
from .adjudicate import (adjudicate, amendment_basis, amendment_impact, approval_needs, outside_changes,
                         owner_requests, preauth_change, successor_denial)
from .contract import (DEFAULT_TOOLS, Budget, ContractError, TaskContract, check_subtasks, child_task_id, in_scope,
                       plan_subtasks)
from .delivery import SUFFIX
from .external import changes, overlaps, watch_snapshot
from .gates import DEFAULT_GATES, Gate, TrustChainGate, model_budget_denial
from .fingerprint import framework_fingerprint
from .ledger import Ledger, LedgerError
from .models import ModelAdapter, ModelError, extract_json
from .obs import ObsError
from .sandbox import NoSandbox
from .tools import SKIP_DIRS, Workspace
from .types import Verdict, WorkerStatus
from .verify import AuditResult, Candidate, CheckResult, audit, combine, freeze, freeze_one, run_test
from .worker import WorkerOutcome, run_worker


PLAN_PROMPT = """You are the planner of a governed task. Split the goal into 1 to 6 subtasks that run one after
another, each in a fresh copy of the workspace, each changing only the files in its "writes" list.
Reply with exactly one JSON object and nothing else:
{"subtasks": [{"id": "a", "goal": "...", "writes": ["path", "folder/"], "acceptance_files": ["owner check file"],
  "budget": {"max_rounds": 4, "max_model_calls": 5, "max_commands": 2}, "max_reassign": 0}]}
Rules: ids are lowercase letters, digits, _ or -; acceptance_files are chosen only from the owner's checks; you never
write acceptance commands; writes never touch an acceptance or protected file; the budgets together must leave room
for the planner and one final review within the total budget; give max_tokens too when the total budget has it."""


class KernelError(RuntimeError):
    pass


@dataclass
class RunReport:
    task_id: str
    verdict: Verdict
    reasons: list[str]
    worker: WorkerOutcome
    candidate: Candidate | None = None
    test: CheckResult | None = None
    audit: AuditResult | None = None
    delivered_to: Path | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    reported_verdict: Verdict = Verdict.UNKNOWN
    awaiting: dict | None = None
    effective: TaskContract | None = None


class Kernel:
    def __init__(self, ledger: Ledger, worker_model: ModelAdapter, reviewer_model: ModelAdapter,
                 gates: tuple[Gate, ...] = DEFAULT_GATES, sandbox=None, trust=None, framework=None, obs=None,
                 anchor_paths=(None, None), release=None, review_mode=None, watch=()):
        if worker_model is reviewer_model:
            raise KernelError("worker and reviewer must be different models")
        self.ledger = ledger
        self.watch = tuple(Path(p).expanduser().absolute() for p in watch)
        self.anchor_paths = anchor_paths
        self.release = release
        self.framework, self.obs, self.obs_failed = framework, obs, None
        self.trust, self.credential = trust, None   # trust: TrustSetup (m2k/trust.py) or None = no chain
        self.child_credentials: dict[str, dict] = {}   # subtask task id -> its own child credential (T22)
        self.failed_scopes: dict[str, set[str]] = {}   # attempt id -> in-scope digests that already failed (T24)
        self.submitted: dict[str, TaskContract] = {}   # parent id -> contract its credential covers, when planned (T27)
        self.effective: dict[str, TaskContract] = {}
        self.clock = lambda: int(time.time())
        self.sandbox = sandbox or NoSandbox()   # how every command runs; see m2k/sandbox.py
        self.worker_model = worker_model
        self.reviewer_model = reviewer_model
        self.review_mode = review_mode
        self.gates = gates

    def run(self, contract: TaskContract, _parent: tuple[TaskContract, Any] | None = None,
            *, amends: str | None = None, approval: dict | None = None) -> RunReport:
        """_parent = (parent contract, subtask, epoch) when the kernel runs an attempt at one of its own subtasks."""
        amendment, source = None, None
        owner = json.loads(json.dumps(contract.data(), default=str))
        needs = None
        def submit_guard():
            nonlocal needs
            if reason := successor_denial(self.ledger, amends):
                return reason
            needs = approval_needs(self.ledger, amends, owner)
            if approval is not None and (approval.get("needs") != needs or approval.get("by") != "m2k answer"):
                return "the situation changed since you confirmed; run m2k answer again"
            if any(needs.values()) and approval is None:
                return f"owner approval required for {needs}; run m2k answer in a terminal"
            if approval is not None:
                amendment["approval"] = approval
            return None
        if amends is not None:
            if _parent is not None:
                raise KernelError("only a top-level task can amend another task")
            contract, amendment, source = self._amend(contract, amends)
            if reason := submit_guard():
                raise KernelError(reason)
        verify = ((lambda: self._verify_chain(self.effective.get(contract.task_id, contract))) if _parent is None
                  else (lambda: self._verify_child(contract, _parent[0])))   # the whole chain this task acts under
        ledger_path, workspace = self.ledger.path.resolve(), contract.workspace.resolve()
        if ledger_path.is_relative_to(workspace):
            raise KernelError(f"ledger is inside the workspace: ledger={ledger_path}, workspace={workspace}")
        if any(overlaps(p, w) for p in self.watch for w in (workspace, workspace.parent / f"{workspace.name}.subtasks")):
            raise KernelError("watched paths overlap the task or subtask workspace")
        if contract.deliver_to is not None:
            delivery = contract.deliver_to.resolve()
            if delivery.is_relative_to(workspace) or workspace.is_relative_to(delivery):
                raise KernelError(f"deliver_to overlaps the workspace: deliver_to={delivery}, workspace={workspace}")
        tid = contract.task_id
        if _parent is not None and contract.preauth is not None: raise KernelError("[preauth] requires a top-level task")
        try:   # the plan's rules hold however the contract was built, not only when it came from a TOML file
            check_subtasks(replace(contract, budget=Budget(**dict.fromkeys(asdict(contract.budget))))
                           if amendment is not None else contract)  # check amendment sums after impact
            if contract.plan and (contract.plan != "model" or contract.subtasks or not contract.acceptance.parts):
                raise ContractError('plan = "model" needs [acceptance].parts and no [[subtasks]]')
        except ContractError as e:
            raise KernelError(str(e)) from e
        if getattr(self.worker_model, "external", False) and (contract.plan or set(contract.tools) != set(DEFAULT_TOOLS)):
            raise KernelError("an external agent cannot plan, and it can do everything a worker can: "
                              "its contract must grant every tool (T37)")
        attempts = [(s, n) for s in contract.subtasks for n in range(1, s.max_reassign + 2)]
        for t in [tid] + [child_task_id(tid, s.id, n) for s, n in attempts]:
            if self.ledger.events(t):
                raise KernelError(f"task id '{t}' already exists in the ledger; use a new id")
        for s, n in attempts:
            d = self._subtask_dir(workspace, s, n)
            if d.exists() and (not d.is_dir() or any(d.iterdir())):
                raise KernelError(f"subtask workspace must be new or empty: {d}")
        if not contract.acceptance.files:
            raise KernelError("acceptance files must be provided by the task owner")
        ws = self._prepare_workspace(contract, source)
        ws.sandbox, ws.network = self.sandbox, contract.network
        acceptance_files = {}
        for rel in contract.acceptance.files:
            p = ws.resolve(rel)
            if not p.is_file():
                raise KernelError(f"acceptance file missing at submit: {rel}")
            acceptance_files[p.relative_to(ws.root).as_posix()] = freeze_one(p)
        if not any(rel in contract.acceptance.command or Path(rel).name in contract.acceptance.command for rel in acceptance_files):
            raise KernelError("acceptance command does not use any acceptance file")
        protected = self._protected_hashes(contract, ws) | acceptance_files
        baseline = freeze(ws)
        if amendment is not None and contract.subtasks:
            impact = amendment_impact(self.ledger, amends, json.loads(json.dumps(contract.data(), default=str)),
                                      baseline.files, **self._adjudication_checks())
            amendment.update(start_files=baseline.files, impact=impact)
            limits = {k: None if v is None else v + sum(getattr(s.budget, k) or 0 for s in contract.subtasks
                                                       if s.id in impact["reused"]) for k, v in asdict(contract.budget).items()}
            try:
                check_subtasks(replace(contract, budget=Budget(**limits)))
            except ContractError as e:
                raise KernelError(str(e)) from e
        gates, trust_rec = self.gates, None
        if self.trust is not None:   # G0–G2: issue the credential now; nothing is in the ledger yet
            try:
                trust_rec = self._issue_credential(contract) if _parent is None else self._issue_child(contract, *_parent)
            except ValueError as e:
                raise KernelError(f"trust: {e}") from e
        if self.trust is not None or _parent is not None:   # a subtask's chain check also carries recall (T24)
            check = verify if self.trust is not None else lambda: self._verify_child(contract, _parent[0], check_framework=False)
            gates = (TrustChainGate(check),) + gates
        extra = (({"parent": _parent[0].task_id} if _parent is not None else {})
                 | ({"contract": json.loads(json.dumps(contract.data(), default=str))} if _parent is None else {})
                 | ({"start_files": baseline.files} if contract.preauth is not None else {})
                 | ({"amends": amendment} if amendment is not None else {})
                 | ({"subtasks": [asdict(s) for s in contract.subtasks]} if contract.subtasks else {})
                 | ({"plan": contract.plan} if contract.plan else {})
                 | ({"context": contract.context} if contract.context else {})
                 | ({"watch": [str(p) for p in self.watch]} if self.watch else {})
                 | ({"release": self.release} if _parent is None and self.release is not None else {})
                 | ({"framework": self.framework} if self.framework is not None else {}))
        self._record(tid, "task.submitted", {
            "goal": contract.goal, "contract_sha256": contract.digest(), "workspace": str(ws.root), "ledger": str(ledger_path),
            "seed": None if contract.seed is None else str(contract.seed), "protected": protected, "acceptance_files": acceptance_files,
            "baseline": baseline.digest, "baseline_files": len(baseline.files),
            "worker_model": self.worker_model.name, "reviewer_model": self.reviewer_model.name,
            "review": self.review_mode or ("same-model" if self.worker_model.name == self.reviewer_model.name else "cross-model"),
            "sandbox": self.sandbox.name, "network": contract.network, "trust": trust_rec,
            "obs": {"notary": self.obs.notary, "strength": self.obs.strength} if self.obs else None, **extra},
            **({"guard": submit_guard} if amends is not None else {}))
        if needs and needs["budget"]:
            self._record(tid, "budget.raised", {"by": "owner", "task": tid, "amends": amends, "fields": needs["budget"]})
        credential = self.credential if _parent is None else self.child_credentials.get(tid)
        if credential is not None:
            self._record(tid, "credential.issued", {"credential": credential})

        planned = contract
        if _parent is None and contract.preauth is not None: self.effective[tid] = contract
        if contract.plan:   # GLOBAL/DECISION: the plan is a proposal; it runs only after passing every plan rule
            self.submitted[tid] = contract
            planned = replace(contract, subtasks=self._plan(contract, ws) or ())
        if contract.plan and not planned.subtasks:
            err = self.ledger.last(tid, "model.error")   # the planner model failed: not a rejected plan (T36)
            worker, failed = (self._stop(tid, WorkerStatus.MODEL_ERROR, f"planner: {err.payload['error'][:300]}") if err
                              else self._stop(tid, WorkerStatus.PLAN_REJECTED, "no proposed plan passed the plan rules")), None
        elif planned.subtasks:
            worker, failed = self._run_subtasks(planned, ws)
        else:
            worker, failed = self._work(replace(contract, protected=tuple(protected)), ws, gates), None

        shadow_dirs = {Path("."), *(Path(rel).parent for rel in acceptance_files)}
        shadow_names = sys.stdlib_module_names | {"sitecustomize", "usercustomize", "conftest"}
        repairs = 0
        while True:
            contract = self.effective.get(tid, contract)
            ws.network = contract.network
            awaiting = worker.awaiting if worker.status is WorkerStatus.AWAITING_OWNER else None
            budget_waiting = (_parent is None and not contract.subtasks and not contract.plan
                              and worker.status is WorkerStatus.BUDGET_EXHAUSTED)
            budget_reason = worker.reason
            cand = freeze(ws)
            self._record(tid, "candidate.frozen", {"digest": cand.digest, "files": cand.files})
            shadows = []
            for rel, digest in cand.files.items():
                p = Path(rel)
                name = p.name.split(".", 1)[0]
                parent, name = (p.parent.parent, p.parent.name) if name == "__init__" else (p.parent, name)
                if (p.suffix in {".py", ".pyw", ".pyc", ".pyo", ".pyd", ".so"} and parent in shadow_dirs and name in shadow_names
                        and baseline.files.get(rel) != digest):
                    shadows.append(rel)
            tampered = [rel for rel, h in protected.items() if cand.files.get(rel) != h]
            outside = [rel for rel in sorted(cand.files.keys() | baseline.files.keys()) if contract.writes
                       and cand.files.get(rel) != baseline.files.get(rel) and not in_scope(rel, contract.writes)]
            repeated = bool(contract.writes) and self._scope_digest(cand, contract.writes) in self.failed_scopes.get(tid, ())
            watched = outside_changes(self.ledger, tid)
            unverified = any("unverified" in e.payload for e in watched)
            paths = sorted({p for e in watched for k in ("added", "changed", "removed", "unverified") for p in e.payload.get(k, [])})
            broken = self._problem(verify)
            if broken and broken.startswith("FRAMEWORK_CHANGED") and worker.status is not WorkerStatus.TRUST_BROKEN:
                worker = self._stop(tid, WorkerStatus.TRUST_BROKEN, broken)
            reasons = ([f"trust chain broken at pre-test: {broken}"] if broken else [])
            reasons += ([f"protected files changed or removed: {tampered}"] if tampered else [])
            reasons += [f"shadowed modules: {shadows}"] if shadows else []
            reasons += [f"files changed outside the workspace in watched paths: {paths}"] if watched else []
            reasons += [f"files changed outside the write scope: {outside}"] if outside else []
            reasons += ["the candidate repeats an earlier attempt that already failed"] if repeated else []
            bad = (f"TRUST_{broken}" if broken else "SHADOWED_MODULE" if shadows else
                   ("OUTSIDE_UNVERIFIED" if unverified else "OUTSIDE_CHANGED") if watched else "OUT_OF_SCOPE" if outside
                   else "REPEATED_FAILURE" if repeated else "PROTECTED_FILE_CHANGED")
            test = (CheckResult(Verdict.UNKNOWN if bad == "OUTSIDE_UNVERIFIED" else Verdict.FAIL, "", None, bad)
                    if broken or tampered or shadows or watched or outside or repeated
                    else run_test(contract, ws))
            self._record(tid, "test.result", {"verdict": test.verdict.value, "exit_code": test.exit_code,
                                                  "reason": test.reason, "output": test.output[-2000:], "candidate": cand.digest})
            if freeze(ws).digest != cand.digest:
                reasons.append("acceptance command changed workspace files; test is not bound to the frozen candidate")
                test = CheckResult(Verdict.UNKNOWN, test.output, test.exit_code, "CANDIDATE_CHANGED")
            review = None
            budget_tail = (not contract.subtasks and not contract.plan and not broken
                           and worker.status is WorkerStatus.BUDGET_EXHAUSTED and test.verdict is Verdict.PASS
                           and not test.reason and not model_budget_denial(contract, self._usage(contract), "reviewer"))
            if awaiting:
                reasons.insert(0, f"awaiting owner: {awaiting['kind']}: {awaiting['text'][:300]}")
                verdict = Verdict.UNKNOWN
            elif (worker.status is not WorkerStatus.FINISHED and not budget_tail) or broken:
                if worker.status is not WorkerStatus.FINISHED and not budget_waiting:
                    reasons.insert(0, f"worker stopped: {worker.status.value} ({worker.reason})")
                verdict = failed or Verdict.FAIL
            else:
                if budget_tail:
                    reasons.insert(0, f"worker stopped: BUDGET_EXHAUSTED ({worker.reason}); candidate checked as usual")
                review = self._audit(contract, ws, cand, test, baseline.files)
                reasons += [f"test: {test.verdict.value}" + (f" ({test.reason})" if test.reason else ""),
                            f"review: {review.decision}"] + [f"  - {r}" for r in review.reasons]
                verdict = combine(test.verdict, review.verdict)
            if budget_waiting and verdict is not Verdict.PASS:
                event = self.ledger.append(tid, "owner.request", {"kind": "budget", "text": budget_reason,
                                                                 "request": None, "by": "framework"})
                awaiting = dict(event.payload, seq=event.seq)
                reasons.insert(0, f"awaiting owner: {awaiting['kind']}: {awaiting['text'][:300]}")
                verdict = Verdict.UNKNOWN
            if (contract.subtasks or contract.plan or _parent is not None or repairs >= contract.max_repairs
                    or worker.status is not WorkerStatus.FINISHED or verdict is not Verdict.FAIL
                    or test.reason or test.verdict is Verdict.UNKNOWN or review is None or review.verdict is Verdict.UNKNOWN
                    or model_budget_denial(contract, self.ledger.usage(tid), "worker")):
                break
            audit_record = self.ledger.last(tid, "audit.result")
            if audit_record is None or audit_record.payload.get("candidate") != cand.digest:
                break
            record = self.ledger.last(tid, "test.result").payload
            briefing = f"test.result: exit_code={record['exit_code']}\noutput:\n{record['output'][-1500:]}"
            record = audit_record.payload
            briefing += f"\naudit.result: decision={record['decision']}\nreasons: {record['reasons']}"
            repairs += 1
            self.ledger.append(tid, "repair.started", {"attempt": repairs, "candidate": cand.digest, "facts": briefing})
            worker = self._work(replace(contract, protected=tuple(protected)), ws, gates, briefing)
        if contract.max_repairs and not contract.subtasks and _parent is None:
            reasons.insert(int(budget_tail and not awaiting), f"repair attempts: {repairs}")
        return self._finish(contract, verify, verdict, reasons, worker, cand, test, review, _parent is None, awaiting)

    def _amend(self, contract: TaskContract, task_id: str):
        try:
            old, frozen, version, prior = amendment_basis(self.ledger, task_id)
            previous = Workspace(Path(old["workspace"]))
            if freeze(previous).files != frozen["files"]:
                raise ValueError("the earlier version's workspace no longer matches its last frozen candidate")
        except (ValueError, KeyError, TypeError, OSError) as e:
            raise KernelError(str(e)) from e
        owner = json.loads(json.dumps(contract.data(), default=str))
        old_owner = dict(old["contract"])
        if old.get("amends"):
            old_owner["budget"] = old["amends"]["owner_budget"]
        changed = sorted(k for k in owner if k not in {"task_id", "workspace", "seed", "deliver_to"}
                         and owner[k] != old_owner.get(k))
        left = {}
        for key, value in asdict(contract.budget).items():
            used = prior[key.removeprefix("max_")]
            left[key] = None if value is None else value - used
            if left[key] is not None and left[key] <= 0:
                raise KernelError(f"budget already used up by earlier versions: {key}: owner {value}, used {used}")
        amendment = {"task": task_id, "version": version, "from_candidate": frozen["digest"], "changed": changed,
                     "prior_usage": prior, "owner_budget": owner["budget"]}
        return replace(contract, budget=Budget(**left)), amendment, (previous.root, frozen["files"])

    # ---- subtasks (stage 3) ------------------------------------------------------
    @staticmethod
    def _subtask_dir(workspace: Path, s, epoch: int = 1) -> Path:
        return workspace.parent / f"{workspace.name}.subtasks" / (s.id + (f".e{epoch}" if epoch > 1 else ""))

    @staticmethod
    def _scope_digest(cand: Candidate, writes) -> str:
        files = {r: h for r, h in cand.files.items() if in_scope(r, writes)}
        return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()

    def _grant(self, tid, request, by):
        current = self.effective.get(tid)
        if current is None or by not in ("worker", "framework"): return None
        try:
            data = json.loads(json.dumps(current.data(), default=str))
            new, clause, fields = preauth_change(self.ledger, tid, data, request)
            if not fields: return current
            candidate = replace(current, **{k: Budget(**v) if k == "budget" else tuple(v) if k in ("writes", "tools") else v
                                            for k, (_, v) in fields.items()})
            credential = self._issue_credential(candidate, activate=False) if self.trust else None
            if self.trust: self._verify_chain(candidate, credential)
            def guard():
                try: return None if preauth_change(self.ledger, tid, data, request) == (new, clause, fields) else "preauth changed"
                except ValueError as e: return str(e)
            self._record(tid, "authority.granted", {"basis": {"by": "preauth", "clause": clause}, "by": by,
                         "request": request, "fields": fields, "contract_sha256": candidate.digest()}, guard=guard)
        except (ValueError, KernelError): return None
        self.effective[tid], self.credential = candidate, credential
        if credential is not None: self._record(tid, "credential.issued", {"credential": credential})
        return candidate

    def _work(self, contract: TaskContract, ws: Workspace, gates, briefing: str = "") -> WorkerOutcome:
        """The model-driven worker loop, or an external agent CLI that does the whole job in one go (T37)."""
        submitted = self.ledger.last(contract.task_id, "task.submitted")
        while submitted and submitted.payload.get("parent"):
            submitted = self.ledger.last(submitted.payload["parent"], "task.submitted")
        if not briefing and submitted and (amendment := submitted.payload.get("amends")):
            old = self.ledger.last(amendment["task"], "verdict").payload
            briefing = (f"this is version {amendment['version']} of the task (amends {amendment['task']}); "
                        f"the owner changed: {', '.join(amendment['changed']) or 'none'}\n"
                        f"Earlier verdict: {old['verdict']}; reasons: {'; '.join(old.get('reasons', []))[:1500]}\n"
                        f"Remaining budget: {submitted.payload['contract']['budget']}")
        before, unread = watch_snapshot(self.watch)
        grant = {"grant": lambda request, by: self._grant(contract.task_id, request, by)} if contract.task_id in self.effective else {}
        try:
            if getattr(self.worker_model, "external", False):
                fixed = set(contract.protected) | set(contract.acceptance.files)
                return self.worker_model.run(contract, self.ledger, ws.root, gates, fixed, briefing, **grant)
            return run_worker(contract, self.ledger, self.worker_model, gates, self.sandbox, briefing=briefing, **grant)
        finally:
            after, bad = watch_snapshot(self.watch)
            bad = sorted(set(unread + bad))[:50]
            if bad or before != after:
                self._record(contract.task_id, "outside.changed", {"unverified": bad} if bad else changes(before, after))

    def _stop(self, tid: str, status: WorkerStatus, reason: str, awaiting=None) -> WorkerOutcome:
        self.ledger.append(tid, "worker.stopped", {"status": status.value, "reason": reason, "summary": ""})
        return WorkerOutcome(status, reason, awaiting=awaiting)

    def _run_subtasks(self, contract: TaskContract, ws: Workspace) -> tuple[WorkerOutcome, Verdict | None]:
        """One after another; each subtask runs the whole chain in its own copy; only a PASS is merged back."""
        tid = contract.task_id
        reused = (self.ledger.last(tid, "task.submitted").payload.get("amends") or {}).get("impact", {}).get("reused", {})
        for s in contract.subtasks:
            if s.id in reused:
                source = reused[s.id]
                self.ledger.append(tid, "subtask.reused", {"id": s.id, "task_id": source,
                                   "candidate": self.ledger.last(source, "candidate.frozen").payload["digest"]})
                continue
            used, problem, unknown = {}, "no budget left for an attempt", False
            for epoch in range(1, s.max_reassign + 2):
                if self.obs_failed is not None:
                    break
                left = {k: None if v is None else max(v - used.get(k.removeprefix("max_"), 0), 0)
                        for k, v in asdict(s.budget).items()}   # the plan's budget is never reset by a reassignment
                if 0 in (left["max_rounds"], left["max_model_calls"], left["max_tokens"]):
                    break
                report, child_id, problem = self._attempt(contract, ws, s, epoch, Budget(**left))
                if report is not None and report.worker.status is WorkerStatus.AWAITING_OWNER:
                    return (self._stop(tid, WorkerStatus.AWAITING_OWNER, f"subtask {s.id}: {report.worker.reason}",
                                       awaiting=report.awaiting), Verdict.UNKNOWN)
                if not problem or report is None:
                    break
                unknown = ((report.verdict is Verdict.UNKNOWN or problem.startswith("adjudication "))
                           and not problem.startswith("trust chain"))
                used = {k: used.get(k, 0) + v for k, v in self.ledger.usage(child_id).items()}
            if problem:
                return (self._stop(tid, WorkerStatus.SUBTASK_FAILED, f"subtask {s.id}: {problem}"),
                        Verdict.UNKNOWN if unknown else Verdict.FAIL)
        return self._stop(tid, WorkerStatus.FINISHED, f"all {len(contract.subtasks)} subtasks passed and merged"), None

    def _attempt(self, contract: TaskContract, ws: Workspace, s, epoch: int, budget: Budget):
        tid, child_id = contract.task_id, child_task_id(contract.task_id, s.id, epoch)
        amounts = {k.removeprefix("max_"): v or 0 for k, v in asdict(budget).items()}
        limits = {k.removeprefix("max_"): v for k, v in asdict(contract.budget).items()}
        if not self.ledger.reserve(tid, child_id, amounts, limits):
            refused = self.ledger.last(tid, "budget.refused")
            short = refused.payload["short"] if refused and refused.payload["child"] == child_id else {}
            return None, child_id, f"budget reservation refused ({short})"
        try:
            child_ws = self._subtask_dir(ws.root, s, epoch)
            shutil.copytree(ws.root, child_ws, ignore=shutil.ignore_patterns(*SKIP_DIRS), dirs_exist_ok=True)
            # The goal is the owner's words only; model-written text (planner goal, ledger facts) goes to context (T32).
            goal = (f"Part '{s.id}' of the task owner's goal: {contract.goal}" if contract.plan
                    else f"{s.goal}\n\n(This is one part of a larger task: {contract.goal})")
            context = [s.goal] if contract.plan else []
            if epoch > 1:
                context.append("Facts from the ledger about earlier attempts (not opinions):\n"
                               + self._facts(tid, [child_task_id(tid, s.id, n) for n in range(1, epoch)]))
            child = TaskContract(child_id, goal, child_ws, s.acceptance, budget, s.tools,
                                 contract.constraints + (f"Only change files inside: {', '.join(s.writes)}",),
                                 protected=contract.protected, network=s.network, writes=s.writes,
                                 context="\n\n".join(context))
            self.ledger.append(tid, "subtask.started", {"id": s.id, "task_id": child_id, "workspace": str(child_ws),
                                                        "epoch": epoch})
            report = self.run(child, (contract, s, epoch))
        except KernelError as e:   # e.g. its child credential could not be issued: the attempt failed
            return None, child_id, str(e)
        finally:
            self.ledger.release(tid, child_id)
        self.ledger.append(tid, "subtask.finished", {"id": s.id, "task_id": child_id, "verdict": report.verdict.value,
                                                     "epoch": epoch})
        broken = self._problem(lambda: self._verify_child(child, contract))
        if report.verdict is Verdict.PASS and not broken:
            adj = self._adjudicate(child_id, check_chain=False)
            if adj.verdict != "PASS":
                return report, child_id, f"adjudication {adj.verdict}: {adj.reasons[0]}"
            return report, child_id, self._merge(tid, s, report.candidate, child_ws, ws)
        test, review = report.test, report.audit
        if test is not None and ((test.verdict is Verdict.FAIL and not test.reason) or (review and review.decision == "NO_GO")):
            for n in range(epoch + 1, s.max_reassign + 2):   # a later attempt may not hand in this failure again
                self.failed_scopes.setdefault(child_task_id(tid, s.id, n), set()).add(self._scope_digest(report.candidate, s.writes))
        return report, child_id, report.verdict.value if report.verdict is not Verdict.PASS else f"trust chain broken: {broken}"

    def _facts(self, parent_id: str, attempts: list[str]) -> str:
        """What earlier attempts did and how they ended, from ledger events only (never thoughts or summaries)."""
        recalled = {e.payload["task_id"]: e.payload["reason"] for e in self.ledger.events(parent_id, "subtask.recalled")}
        out = []
        for t in attempts:
            evs = self.ledger.events(t)
            args = {e.op_id: e.payload.get("args", {}) for e in evs if e.kind == "effect.intent"}
            out.append(f"Attempt {t}:")
            for e in evs:
                p, a, k = e.payload, args.get(e.op_id, {}), e.kind
                code = p.get("detail", {}).get("exit_code") if k == "effect.result" else p.get("exit_code")
                line = {"effect.result": f"- {p.get('tool')} {a.get('command') or a.get('path') or ''!r}: {p.get('status')}",
                        "gate.denied": f"- denied by gate {p.get('gate')}: {p.get('reason')}",
                        "worker.stopped": f"- worker stopped: {p.get('status')} ({p.get('reason')})",
                        "test.result": f"- acceptance: {p.get('verdict')} [{p.get('reason')}]",
                        "audit.result": f"- review: {p.get('decision')}: {'; '.join(p.get('reasons') or [])}"}.get(k)
                if line:
                    out.append(line + (f" (exit {code})" if code is not None else "")
                               + (" — outcome unknown — that workspace was discarded; do not assume it happened"
                                  if k == "effect.result" and p["status"] == "UNKNOWN" else "")
                               + (f"\n  output tail: {p['output'][-600:]}" if k == "test.result" else ""))
            out += [f"- recalled: {recalled[t]}"] if t in recalled else []
        return "\n".join(out)[-3000:]

    def _merge(self, tid: str, s, cand: Candidate, child_ws: Path, ws: Workspace) -> str | None:
        """Merge exactly the subtask's in-scope changes into the parent workspace (an effect: intent, then result)."""
        if freeze(Workspace(child_ws)).digest != cand.digest:
            return "subtask workspace changed after its verdict"
        have = {r: h for r, h in freeze(ws).files.items() if in_scope(r, s.writes)}
        want = {r: h for r, h in cand.files.items() if in_scope(r, s.writes)}
        changed, removed = sorted(r for r in want if have.get(r) != want[r]), sorted(r for r in have if r not in want)
        op = f"merge-{s.id}"
        self.ledger.append(tid, "effect.intent", {"tool": "merge_subtask", "args": {
            "subtask": s.id, "candidate": cand.digest, "changed": changed, "removed": removed}}, op_id=op)
        for rel in changed:
            (ws.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child_ws / rel, ws.root / rel)
        for rel in removed:
            (ws.root / rel).unlink()
        ok = {r: h for r, h in freeze(ws).files.items() if in_scope(r, s.writes)} == want
        self.ledger.append(tid, "effect.result", {"tool": "merge_subtask", "status": "COMPLETED" if ok else "FAILED",
                                                  "output": "", "detail": {"verified": ok}}, op_id=op)
        return None if ok else "merged files do not match the subtask's frozen candidate"

    def _plan(self, contract: TaskContract, ws: Workspace):
        """The worker model proposes the split (at most two proposals); returns the accepted subtasks or None."""
        tid, feedback = contract.task_id, ""
        for attempt in (1, 2):
            if model_budget_denial(contract, self.ledger.usage(tid), "planner"):
                return None
            try:
                reply = self.worker_model.complete(self._plan_prompt(contract, ws, feedback))
            except ModelError as e:
                self.ledger.append(tid, "model.error", {"role": "planner", "error": str(e)[:1000]})
                return None
            self.ledger.append(tid, "model.called", {"role": "planner", "model": reply.model or self.worker_model.name,
                                                     "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                                                     **({"lost_attempts": reply.lost_attempts} if reply.lost_attempts else {})})
            self.ledger.append(tid, "plan.proposed", {"attempt": attempt, "reply_head": reply.text[:2000]})
            try:
                subs = plan_subtasks(extract_json(reply.text), contract)
                self._plan_fits(contract, subs)
            except ValueError as e:   # ContractError or no JSON
                feedback = str(e)
                self.ledger.append(tid, "plan.rejected", {"attempt": attempt, "reason": feedback})
                continue
            plan = [asdict(s) for s in subs]
            self.ledger.append(tid, "plan.accepted", {"subtasks": plan, "plan_sha256": hashlib.sha256(
                json.dumps(plan, sort_keys=True).encode()).hexdigest()})
            return subs
        return None

    def _plan_fits(self, contract: TaskContract, subs) -> None:
        used = self.ledger.usage(contract.task_id)
        for key, limit in asdict(contract.budget).items():   # the subtasks, what the parent used, and its final review
            need = sum(getattr(s.budget, key) or 0 for s in subs) + used[key.removeprefix("max_")] + (key == "max_model_calls")
            if limit is not None and need > limit:
                raise ContractError(f"the plan needs {need} {key} with the parent's own use and review; the limit is {limit}")
        for s in subs:
            for n in range(1, s.max_reassign + 2):
                d = self._subtask_dir(contract.workspace.resolve(), s, n)
                if self.ledger.events(child_task_id(contract.task_id, s.id, n)) or (d.exists() and any(d.iterdir())):
                    raise ContractError(f"subtask {s.id!r}: attempt {n} id or workspace is already in use")

    def _plan_prompt(self, contract: TaskContract, ws: Workspace, feedback: str) -> list[dict[str, str]]:
        files = [f.relative_to(ws.root).as_posix() for f in ws.files()][:200]
        checks = "\n".join(f"- {f}: `{cmd}`\n```\n{(ws.root / f).read_text(encoding='utf-8', errors='replace')[:1500]}\n```"
                           for f, cmd in contract.acceptance.parts)
        budget = {k: v for k, v in asdict(contract.budget).items() if v is not None}
        task = (f"Goal:\n{contract.goal}\n\nConstraints:\n" + "\n".join(f"- {c}" for c in contract.constraints)
                + f"\n\nWorkspace files: {files}\n\nOwner acceptance checks (choose; never write your own):\n{checks}"
                + f"\n\nTools: {list(contract.tools)}; network: {contract.network}; total budget: {budget}"
                + (f"\n\nYour previous plan was rejected: {feedback}" if feedback else ""))
        return [{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": task}]

    def _usage(self, contract: TaskContract) -> dict[str, Any]:
        """A parent's consumption includes every attempt at every subtask."""
        total: dict[str, Any] = {}
        for t in [contract.task_id] + [e.payload["task_id"] for e in self.ledger.events(contract.task_id, "subtask.started")]:
            for k, v in self.ledger.usage(t).items():
                total[k] = total.get(k, 0) + v
        return total

    # ---- trust chain (G0–G2) ---------------------------------------------------
    def _check_framework(self):
        if self.framework is None:
            return
        before = self.framework
        try:
            after = framework_fingerprint(before["code_dir"], before["files"])
        except OSError as e:
            raise trustlib.TrustError("FRAMEWORK_CHANGED", f"cannot hash {e.filename}") from e
        changed = (["code"] if before["code"] != after["code"] else [])
        changed += [name for name in before["files"] if before["files"][name] != after["files"][name]]
        if changed or before["digest"] != after["digest"]:
            raise trustlib.TrustError("FRAMEWORK_CHANGED", ", ".join(changed) or "digest")

    def _issue_credential(self, contract: TaskContract, activate=True) -> dict:
        t, code = self.trust, Path(trustlib.__file__).resolve().parent
        charter, _ = trustlib.verify_charter(t.pin, t.domain, t.charter_env, t.revocations(), code)
        issuer = t.issuer_key()
        if trustlib.public_key_b64(issuer) != charter["issuer_public_key_b64"]:
            raise trustlib.TrustError("ISSUER_KEY", "issuer_key does not match the charter's issuer")
        credential = trustlib.issue_credential(charter, issuer, contract, self.clock(), t.ttl_s,
                                                  self.framework["digest"] if self.framework is not None else None)
        if not activate: return credential
        self.credential = credential
        body = self._verify_chain(contract)
        return {"domain": t.domain, "root_fingerprint": t.pin.fingerprint, "charter_id": charter["charter_id"],
                "credential_id": body["credential_id"],
                "credential_sha256": hashlib.sha256(trustlib.canonical(self.credential)).hexdigest()}

    def _verify_chain(self, contract: TaskContract, credential=None) -> dict | None:
        self._check_framework()
        t = self.trust
        if t is None:
            return None
        return trustlib.verify_chain(t.pin, t.domain, t.charter_env, credential or self.credential, t.revocations(), contract,
                                     self.clock(), Path(trustlib.__file__).resolve().parent)

    def _issue_child(self, contract: TaskContract, parent: TaskContract, sub, epoch: int) -> dict:
        base = self.submitted.get(parent.task_id, parent)   # the contract the parent credential was issued for
        t, body = self.trust, self._verify_chain(base)
        env = trustlib.issue_child_credential(t.issuer_key(), body, base, contract, sub.id, self.clock(), epoch,
                                              parent.subtasks)
        self.child_credentials[contract.task_id] = env
        child = self._verify_child(contract, parent)
        return {"domain": t.domain, "root_fingerprint": t.pin.fingerprint, "charter_id": child["charter_id"],
                "credential_id": child["credential_id"], "parent_credential_id": body["credential_id"],
                "credential_sha256": hashlib.sha256(trustlib.canonical(env)).hexdigest()}

    def _verify_child(self, contract: TaskContract, parent: TaskContract, check_framework=True) -> dict | None:
        if check_framework:
            self._check_framework()
        why = next((e.payload["reason"] for e in self.ledger.events(parent.task_id, "subtask.recalled")
                    if e.payload["task_id"] == contract.task_id), None)
        if why is not None:   # a recall is a ledger fact: it holds whoever wrote it, from whatever process
            raise trustlib.TrustError("RECALLED", why)
        t = self.trust
        if t is None:
            return None
        return trustlib.verify_child_chain(t.pin, t.domain, t.charter_env, self.credential,
                                           self.child_credentials[contract.task_id], t.revocations(),
                                           self.submitted.get(parent.task_id, parent), contract, self.clock(),
                                           Path(trustlib.__file__).resolve().parent, parent.subtasks)

    def _problem(self, verify) -> str | None:
        """The failing chain code, or None when the chain holds (or there is no chain)."""
        try:
            verify()
        except ValueError as e:
            return str(e) if getattr(e, "code", None) == "FRAMEWORK_CHANGED" else getattr(e, "code", "FORMAT")
        return None

    # ---- steps ---------------------------------------------------------------
    def _record(self, tid, kind, payload, guard=None):
        try:
            event = self.ledger.append(tid, kind, payload, guard=guard)
        except LedgerError as e:
            raise KernelError(str(e)) from e
        receipt = None
        if self.obs is not None and self.obs_failed is None:
            try:
                receipt = self.obs.witness(event, self.ledger.ledger_id)
                self.ledger.append(tid, "obs.receipt", {"receipt": receipt})
            except ObsError as e:
                self.obs_failed = str(e)
                self.ledger.append(tid, "obs.failed", {"kind": kind, "reason": str(e)})
        return receipt

    @staticmethod
    def _prepare_workspace(contract: TaskContract, source=None) -> Workspace:
        root = contract.workspace
        if source is not None and root.exists() and (not root.is_dir() or any(root.iterdir())):
            raise KernelError(f"workspace must be new or empty for an amendment: {root}")
        if contract.seed is not None:
            if not contract.seed.is_dir():
                raise KernelError(f"seed folder not found: {contract.seed}")
            if root.exists() and any(root.iterdir()):
                raise KernelError(f"workspace must be empty when a seed is used: {root}")
        if source is not None:
            for rel in source[1]:
                dst = Workspace(root).resolve(rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source[0] / rel, dst)
            if freeze(Workspace(root)).files != source[1]:
                raise KernelError("earlier version's candidate changed while copying")
        if contract.seed is not None:
            shutil.copytree(contract.seed, root, dirs_exist_ok=True)
        root.mkdir(parents=True, exist_ok=True)
        return Workspace(root)

    @staticmethod
    def _protected_hashes(contract: TaskContract, ws: Workspace) -> dict[str, str]:
        out = {}
        for rel in contract.protected:
            p = ws.resolve(rel)
            if not p.is_file():
                raise KernelError(f"protected file does not exist at submit: {rel}")
            out[p.relative_to(ws.root).as_posix()] = freeze_one(p)
        return out

    def _audit(self, contract: TaskContract, ws: Workspace, cand: Candidate, test: CheckResult,
               baseline: dict[str, str], retry: bool = True) -> AuditResult:
        tid = contract.task_id
        denial = model_budget_denial(contract, self._usage(contract), "reviewer")
        if denial:
            result = AuditResult(Verdict.UNKNOWN, "NONE", [f"review not run: {denial}"])
        else:
            submitted = self.ledger.last(tid, "task.submitted")
            while submitted.payload.get("parent"):
                submitted = self.ledger.last(submitted.payload["parent"], "task.submitted")
            amendment = submitted.payload.get("amends")
            reviewed = replace(contract, constraints=contract.constraints + (f"Effective authority: writes={contract.writes}; tools={contract.tools}; network={contract.network}",)) if contract.preauth is not None else contract
            result = audit(reviewed, ws, cand, test, self.reviewer_model, baseline, amendment)
            if result.reply is not None:
                self.ledger.append(tid, "model.called", {
                    "role": "reviewer", "model": result.reply.model or self.reviewer_model.name,
                    "input_tokens": result.reply.input_tokens, "output_tokens": result.reply.output_tokens,
                    **({"lost_attempts": result.reply.lost_attempts} if result.reply.lost_attempts else {})})
        if retry and result.retry:
            self.ledger.append(tid, "audit.retried", {"candidate": cand.digest, "decision": "NONE",
                "reasons": result.reasons, "nonce": result.nonce,
                "reply": result.reply.text[:2000] if result.reply is not None else None})
            if not model_budget_denial(contract, self._usage(contract), "reviewer"):
                return self._audit(contract, ws, cand, test, baseline, retry=False)
        self._record(tid, "audit.result", {"verdict": result.verdict.value, "decision": result.decision,
                                                 "reasons": result.reasons, "candidate": cand.digest,
                                                 "reviewer": self.reviewer_model.name, "nonce": result.nonce,
            **({"reply": result.reply.text[:2000]} if result.decision == "NONE" and result.reply is not None else {})})
        return result

    def _adjudication_checks(self):
        return dict(obs_public_key=self.obs.public_key_b64 if self.obs else None, obs_log=self.obs.log_path if self.obs else None,
                    root_pin=self.trust.pin if self.release is not None and self.trust else None,
                    revocations=self.trust.revocations() if self.release is not None and self.trust else None)

    def _adjudicate(self, tid, check_chain=True):
        adj = adjudicate(self.ledger, tid, check_chain=check_chain, **self._adjudication_checks())
        self.ledger.append(tid, "gate.adjudication", {"verdict": adj.verdict, "candidate": adj.candidate,
                                                      "reasons": adj.reasons[:20]})
        return adj

    def _finish(self, contract, verify, verdict, reasons, worker, cand, test, review, top_level=True, awaiting=None) -> RunReport:
        tid = contract.task_id
        broken = self._problem(verify) if verdict is Verdict.PASS else None
        if broken and broken.startswith("FRAMEWORK_CHANGED"):
            verdict, worker = Verdict.FAIL, self._stop(tid, WorkerStatus.TRUST_BROKEN, broken)
            reasons.append(f"trust chain broken at delivery: {broken}")
        if self.obs_failed is not None:
            verdict = Verdict.UNKNOWN
            reasons.append(f"OBS_UNAVAILABLE: {self.obs_failed}")
        self._record(tid, "verdict", {"verdict": verdict.value, "reasons": reasons, "candidate": cand.digest})
        report = RunReport(tid, verdict, reasons, worker, cand, test, review, reported_verdict=verdict, awaiting=awaiting)
        if contract.preauth is not None: report.effective = contract
        anchor_error, staged = None, None
        if top_level:
            adj = self._adjudicate(tid)
            if not broken and verdict is Verdict.PASS and adj.verdict == "PASS" and contract.deliver_to is not None:
                staged = self._deliver(contract, cand)
            if self.obs is not None:
                receipt = self._record(tid, "anchor.checkpoint", {})
                path, repo = self.anchor_paths
                try:
                    if receipt is None:
                        raise ObsError(self.obs_failed or "checkpoint has no OBS receipt")
                    if path is not None:
                        anchor.append(path, self.ledger, f"{tid} checkpoint", receipt=receipt)
                        if repo is not None:
                            commit = anchor.publish(path, repo, push=True)
                            self.ledger.append(tid, "anchor.published", {"commit": commit, "seq": receipt["body"]["ledger_seq"]})
                except (OSError, ValueError) as e:
                    self.ledger.append(tid, "anchor.failed", {"reason": str(e)})
                    if self.obs_failed or (self.trust is not None and self.trust.domain == "PRODUCTION"):
                        anchor_error = f"ANCHOR_UNAVAILABLE: {e}"
                    else:
                        print(f"\033[31m警告：外部锚点未发布：{e}\033[0m", file=sys.stderr)
        if broken or anchor_error:   # G6 re-checks the chain and requires the external checkpoint before release.
            if staged is not None:
                shutil.rmtree(staged[0])
            self.ledger.append(tid, "delivery.refused", {"reason": anchor_error or f"trust chain broken at delivery: {broken}"})
        elif verdict is Verdict.PASS and top_level:
            # The Gate releases only what the read-only adjudicator, from the ledger alone, also finds PASS (T39).
            if adj.verdict == "PASS":
                if staged is not None:
                    stage, portable = staged
                    target, receipt_path = contract.deliver_to / tid, contract.deliver_to / (tid + SUFFIX)
                    created = []
                    try:
                        if portable is not None:
                            temporary = receipt_path.with_suffix(".tmp")
                            with temporary.open("xb") as stream:
                                created.append(temporary)
                                stream.write(trustlib.canonical(portable))
                            temporary.rename(receipt_path)
                            created[-1] = receipt_path
                        stage.rename(target)
                        report.delivered_to = target
                    except OSError as e:
                        shutil.rmtree(stage)
                        for path in created:
                            path.unlink(missing_ok=True)
                        self.ledger.append(tid, "delivery.refused", {"reason": str(e)})
            else:
                self.ledger.append(tid, "delivery.refused", {"reason": "adjudication: " + "; ".join(adj.reasons)[:500]})
        if top_level and verdict is Verdict.PASS and (anchor_error or broken or adj.verdict != "PASS"
                or (contract.deliver_to is not None and report.delivered_to is None)):
            report.reported_verdict = Verdict.UNKNOWN
            refused = self.ledger.last(tid, "delivery.refused")
            reason = adj.reasons[0] if adj.reasons else "delivery post-verification failed"
            if refused and not adj.reasons:
                reason = refused.payload["reason"]
            report.reasons.append(f"裁决器 {adj.verdict}：{reason}")
        report.usage = self._usage(contract)
        return report

    def _deliver(self, contract: TaskContract, cand: Candidate):
        """G6/G7: stage the exact PASS candidate and witness it before anchoring and release."""
        tid = contract.task_id
        ws = Workspace(contract.workspace)
        verdict_ev = self.ledger.last(tid, "verdict")
        current = freeze(ws)
        target = contract.deliver_to / tid
        stage, receipt_path = contract.deliver_to / (".m2k-staging-" + tid), contract.deliver_to / (tid + SUFFIX)
        problem = None
        if verdict_ev is None or verdict_ev.payload.get("verdict") != "PASS":
            problem = "no PASS verdict in ledger"
        elif verdict_ev.payload.get("candidate") != cand.digest or current.digest != cand.digest:
            problem = "workspace changed after the verdict; delivery refused"
        elif any(p.exists() or p.is_symlink() for p in (target, stage, receipt_path)):
            problem = f"delivery target already exists: {target}"
        if problem:
            self.ledger.append(tid, "delivery.refused", {"reason": problem})
            return None

        self.ledger.append(tid, "delivery.intent", {"target": str(target), "candidate": cand.digest}, op_id="deliver")
        try:
            stage.mkdir(parents=True)
            for rel in cand.files:
                dst = stage / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ws.root / rel, dst)
            landed = freeze(Workspace(stage))
            if landed != cand:
                raise ValueError("delivery post-verification failed")
            receipt = self._record(tid, "delivery.result", {"target": str(target), "candidate": cand.digest,
                                   "files": cand.files, "landed": landed.digest, "post_verified": True})
            if self.obs is not None and receipt is None:
                raise ValueError(f"OBS_UNAVAILABLE: {self.obs_failed}")
            portable = {"schema": "m2k.delivery/1", "ledger_id": self.ledger.ledger_id,
                        "event": asdict(self.ledger.last(tid, "delivery.result")), "receipt": receipt} if receipt else None
            return stage, portable
        except (OSError, ValueError) as e:
            if stage.exists():
                shutil.rmtree(stage)
            self.ledger.append(tid, "delivery.refused", {"reason": str(e)})
            return None


def recall(ledger: Ledger, task_id: str, reason: str) -> str | None:
    """Withdraw a running subtask's authority. Returns why it was refused, or None when recorded."""
    if not ledger.events(task_id):
        return f"unknown task '{task_id}'"
    sub = ledger.events(task_id, "task.submitted")
    parent = sub[0].payload.get("parent") if sub else None
    if parent is None:
        return f"'{task_id}' is not a subtask"
    if ledger.events(task_id, "verdict"):
        return f"'{task_id}' already has a verdict"
    if any(e.payload["task_id"] == task_id for e in ledger.events(parent, "subtask.recalled")):
        return f"'{task_id}' was already recalled"
    ledger.append(parent, "subtask.recalled", {"task_id": task_id, "reason": reason})
    return None


def status(ledger: Ledger, task_id: str) -> dict[str, Any]:
    evs = ledger.events(task_id)
    last = {e.kind: e for e in evs}
    if "task.submitted" not in last:
        raise KernelError(f"unknown task '{task_id}'")
    trust_rec = last["task.submitted"].payload.get("trust")
    out: dict[str, Any] = {"task_id": task_id, "goal": last["task.submitted"].payload.get("goal"),
                           "trust": trust_rec and {k: trust_rec[k] for k in ("domain", "charter_id", "credential_id")},
                           "events": len(evs), "usage": ledger.usage(task_id),
                           "sandbox": last["task.submitted"].payload.get("sandbox"),
                           "network": last["task.submitted"].payload.get("network")}
    out["review"] = {"same-model": "同源审查", "cross-model": "跨模型审查"}.get(last["task.submitted"].payload.get("review"))
    out["awaiting"] = [dict(seq=e.seq, task=e.task_id, **{k: e.payload[k] for k in ("kind", "text")})
                       for e in owner_requests(ledger, task_id)]
    if grants := [dict(seq=e.seq, by=e.payload["by"], fields=e.payload["fields"]) for e in evs if e.kind == "authority.granted"]: out["granted"] = grants
    successor = next((e.task_id for e in ledger.events(kind="task.submitted")
                      if (e.payload.get("amends") or {}).get("task") == task_id), None)
    if successor:
        out["answered_by"] = successor
    if "plan" in last["task.submitted"].payload:
        out["plan"] = last["task.submitted"].payload["plan"]
        out["planned"] = [s["id"] for e in evs if e.kind == "plan.accepted" for s in e.payload["subtasks"]]
    if "framework" in last["task.submitted"].payload:
        out["framework_sha256"] = last["task.submitted"].payload["framework"]["digest"]
    out.update(obs_receipts=sum(e.kind == "obs.receipt" for e in evs), obs_failed=[e.payload for e in evs if e.kind == "obs.failed"])
    out["adjudication"] = last["gate.adjudication"].payload["verdict"] if "gate.adjudication" in last else None
    if "subtasks" in last["task.submitted"].payload or "plan" in last["task.submitted"].payload:
        out["subtasks"] = [{k: e.payload[k] for k in ("id", "task_id", "verdict")} for e in evs if e.kind == "subtask.finished"]
    if "subtask.reused" in last:
        out["reused"] = [e.payload["id"] for e in evs if e.kind == "subtask.reused"]
    if "worker.stopped" in last:
        out["worker"] = last["worker.stopped"].payload
    if "verdict" in last:
        out["verdict"] = last["verdict"].payload["verdict"]
        out["reasons"] = last["verdict"].payload["reasons"]
    else:
        out["verdict"] = None
        out["note"] = "no verdict recorded (still running, or the process was interrupted)"
    for e in evs:
        if e.kind == "obs.receipt" and e.payload["receipt"]["body"]["kind"] == "delivery.result":
            out["delivery_receipt"] = e.payload["receipt"]["body"]["obs_seq"]
    if "delivery.result" in last and ("delivery.refused" not in last or last["delivery.result"].seq > last["delivery.refused"].seq):
        out["delivered_to"] = last["delivery.result"].payload["target"]
        out["post_verified"] = last["delivery.result"].payload["post_verified"]
    elif "delivery.refused" in last:
        out["delivery_refused"] = last["delivery.refused"].payload["reason"]
    return out
