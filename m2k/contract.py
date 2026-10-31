"""Task contract: what the user wants, what is allowed, and how success is checked.

Loaded from a TOML file. Relative paths are resolved against the TOML file's folder.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import InitVar, asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath

DEFAULT_TOOLS = ("list_files", "read_file", "write_file", "edit_file", "run_command")
SUBTASK_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Budget:
    max_rounds: int = 20          # worker loop iterations (one model call each)
    max_model_calls: int = 30     # all model calls for this task, worker + reviewer
    max_tokens: int | None = None  # input+output tokens, when the adapter reports them
    max_commands: int = 30        # run_command executions


@dataclass(frozen=True)
class Acceptance:
    command: str                  # run in the workspace; exit 0 = pass. "{python}" -> current interpreter
    timeout_s: int = 300
    files: tuple[str, ...] = ()
    parts: tuple[tuple[str, str], ...] = ()   # owner file -> the owner's command that checks it (for model plans)


@dataclass(frozen=True)
class Subtask:
    id: str
    goal: str
    writes: tuple[str, ...]               # exact files, or folder prefixes ending in "/"
    acceptance: Acceptance                # only the task owner's acceptance files
    budget: Budget
    tools: tuple[str, ...]
    network: bool = False
    max_reassign: int = 0                 # successor attempts after a failed or recalled one (T24)


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    goal: str
    workspace: Path
    acceptance: Acceptance
    budget: Budget = field(default_factory=Budget)
    tools: tuple[str, ...] = DEFAULT_TOOLS
    constraints: tuple[str, ...] = ()
    deliver_to: Path | None = None
    seed: Path | None = None              # folder copied into the (new, empty) workspace at submit
    protected: tuple[str, ...] = ()       # workspace files the worker may not change (e.g. the acceptance test)
    network: bool = False                 # commands may reach the network only if the task owner grants it
    writes: tuple[str, ...] = ()          # write scope (empty = the whole workspace); set for subtasks
    subtasks: tuple[Subtask, ...] = ()    # run one after another; each merged back only if it PASSes
    max_repairs: int = 0                 # extra attempts for single tasks; the budget is shared
    plan: str = ""                        # "model": a planner model proposes the subtasks (T27)
    context: str = ""                     # text written by a model (planner, earlier attempts): data, never the goal (T32)
    preauth: InitVar[dict | None] = None

    def __post_init__(self, preauth):
        object.__setattr__(self, "preauth", preauth)  # keep legacy asdict() and replacement semantics

    def data(self) -> dict:
        return asdict(self) | ({"preauth": self.preauth} if self.preauth is not None else {})

    def digest(self) -> str:
        data = self.data()
        data["workspace"] = str(self.workspace)
        data["deliver_to"] = None if self.deliver_to is None else str(self.deliver_to)
        data["seed"] = None if self.seed is None else str(self.seed)
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def child_task_id(parent_id: str, sub_id: str, epoch: int = 1) -> str:
    return f"{parent_id}.{sub_id}" + (f".e{epoch}" if epoch > 1 else "")


def in_scope(rel: str, writes) -> bool:
    return any(rel == w or (w.endswith("/") and rel.startswith(w)) for w in writes)


def _norm(rel: str) -> str:
    rel = rel.replace("\\", "/")
    return PurePosixPath(rel).as_posix() + ("/" if rel.endswith("/") else "")


def check_subtasks(parent: TaskContract) -> None:
    """Every rule a subtask plan must meet. Run when the contract is parsed and again by the kernel before submit."""
    if parent.preauth is not None and (parent.subtasks or parent.plan):
        raise ContractError("[preauth] requires a top-level task without subtasks or plan")
    owned, fixed, seen = set(parent.acceptance.files), set(parent.acceptance.files) | set(parent.protected), set()
    for s in parent.subtasks:
        def check(ok, msg):
            if not ok:
                raise ContractError(f"subtask {s.id!r}: {msg}")
        check(isinstance(s.id, str) and SUBTASK_ID.fullmatch(s.id) and s.id not in seen,
              f"id must be unique and match {SUBTASK_ID.pattern}")
        seen.add(s.id)
        check(isinstance(s.goal, str) and s.goal.strip(), "goal must be a non-empty string")
        check(s.writes and all(isinstance(w, str) and w.strip() and w == _norm(w) for w in s.writes),
              "writes must be a non-empty list of normalized paths")
        for w in s.writes:
            path = PurePosixPath(w)
            check(path.parts and not (path.is_absolute() or PureWindowsPath(w).is_absolute() or ".." in path.parts),
                  f"write path must stay inside the workspace: {w}")
            check(not any(in_scope(_norm(f), (w,)) for f in fixed), f"write path covers a protected or acceptance file: {w}")
        files, cmd = s.acceptance.files, s.acceptance.command
        check(files and set(files) <= owned, "acceptance_files must be a non-empty subset of the parent's [acceptance].files")
        check(isinstance(cmd, str) and any(f in cmd or Path(f).name in cmd for f in files),
              "acceptance must be a command that uses one of its acceptance_files")
        for key in Budget.__dataclass_fields__:
            v = getattr(s.budget, key)
            check((v is None and getattr(parent.budget, key) is None) or (type(v) is int and v >= 0),
                  f"budget {key} must be a non-negative integer")
        check(set(s.tools) <= set(parent.tools), "tools must be a subset of the parent's tools")
        check(isinstance(s.network, bool) and (parent.network or not s.network), "network may be true only if the parent allows it")
        check(type(s.max_reassign) is int and 0 <= s.max_reassign <= 3, "max_reassign must be an integer from 0 to 3")
    for key in Budget.__dataclass_fields__:
        limit = getattr(parent.budget, key)
        if limit is not None and sum(getattr(s.budget, key) or 0 for s in parent.subtasks) > limit:
            raise ContractError(f"subtask budgets {[s.id for s in parent.subtasks]} add up to more than the parent's {key} ({limit})")


def _subtasks(raw, parent: TaskContract) -> tuple[Subtask, ...]:
    if not isinstance(raw, list) or not raw or not all(isinstance(s, dict) for s in raw):
        raise ContractError("[[subtasks]] must be a non-empty list of tables")
    need = {"max_rounds", "max_model_calls", "max_commands"} | ({"max_tokens"} if parent.budget.max_tokens is not None else set())
    out = []
    for s in raw:
        writes, files, budget, tools = s.get("writes"), s.get("acceptance_files"), s.get("budget"), s.get("tools", list(parent.tools))
        if not (isinstance(writes, list) and all(isinstance(w, str) for w in writes) and isinstance(files, list)
                and isinstance(tools, list) and isinstance(budget, dict) and need <= set(budget) <= set(Budget.__dataclass_fields__)):
            raise ContractError(f"subtask {s.get('id')!r}: needs writes, acceptance, acceptance_files and a budget with {sorted(need)}")
        out.append(Subtask(s.get("id"), s.get("goal"), tuple(_norm(w) for w in writes),
                           Acceptance(s.get("acceptance"), parent.acceptance.timeout_s, tuple(files)), Budget(**budget),
                           tuple(tools), s.get("network", False), s.get("max_reassign", 0)))
    check_subtasks(replace(parent, subtasks=tuple(out)))
    return tuple(out)


PLAN_KEYS = {"id", "goal", "writes", "acceptance_files", "budget", "tools", "network", "max_reassign"}


def plan_subtasks(reply, parent: TaskContract) -> tuple[Subtask, ...]:
    """A planner's proposal -> subtasks, or ContractError. The planner picks owner checks; it never writes one."""
    subs, parts = reply.get("subtasks") if isinstance(reply, dict) else None, dict(parent.acceptance.parts)
    if not isinstance(subs, list) or not 1 <= len(subs) <= 6 or not all(isinstance(s, dict) for s in subs):
        raise ContractError('a plan must be {"subtasks": [...]} with 1 to 6 subtasks')
    for s in subs:
        files = s.get("acceptance_files")
        if set(s) - PLAN_KEYS:
            raise ContractError(f"subtask {s.get('id')!r}: keys not allowed in a plan: {sorted(set(s) - PLAN_KEYS)}")
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and f in parts for f in files):
            raise ContractError(f"subtask {s.get('id')!r}: acceptance_files must be owner checks from [acceptance].parts")
    return _subtasks([dict(s, acceptance=" && ".join(parts[f] for f in s["acceptance_files"])) for s in subs], parent)


def _path(base: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    p = Path(value)
    return (p if p.is_absolute() else base / p).resolve()


def parse_contract(data: dict, base_dir: Path) -> TaskContract:
    task = data.get("task")
    if not isinstance(task, dict):
        raise ContractError("missing [task] table")
    for key in ("id", "goal", "workspace"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ContractError(f"[task].{key} must be a non-empty string")
    acc = data.get("acceptance")
    if not isinstance(acc, dict) or not isinstance(acc.get("command"), str):
        raise ContractError("missing [acceptance].command")
    files = acc.get("files")
    if not isinstance(files, list) or not files or any(not isinstance(f, str) or not f.strip() for f in files):
        raise ContractError("[acceptance].files must list the task owner's acceptance files")
    if not isinstance(task.get("network", False), bool):
        raise ContractError("[task].network must be true or false")
    max_repairs = task.get("max_repairs", 0)
    if type(max_repairs) is not int or not 0 <= max_repairs <= 5:
        raise ContractError("[task].max_repairs must be an integer from 0 to 5")
    parts, plan = acc.get("parts", {}), task.get("plan", "")
    if not isinstance(parts, dict) or any(not isinstance(c, str) or f not in files or (f not in c and Path(f).name not in c)
                                          for f, c in parts.items()):
        raise ContractError("[acceptance].parts must map owner acceptance files to commands that use them")
    if plan not in ("", "model") or (plan and (not parts or "subtasks" in data)):
        raise ContractError('[task].plan can only be "model", needs [acceptance].parts and excludes [[subtasks]]')
    budget_raw = data.get("budget", {})
    known = set(Budget.__dataclass_fields__)
    unknown = set(budget_raw) - known
    if unknown:
        raise ContractError(f"unknown [budget] keys: {sorted(unknown)}")
    task_id = task["id"].strip()
    tools = tuple(task.get("tools", DEFAULT_TOOLS))
    bad_tools = [t for t in tools if t not in DEFAULT_TOOLS]
    if bad_tools:
        raise ContractError(f"unknown tools: {bad_tools}")
    preauth = data.get("preauth")
    if "preauth" in data:
        if (not isinstance(preauth, dict) or set(preauth) - {"writes", "tools", "network", "budget_percent"}
                or any(not isinstance(preauth.get(k, []), list) or any(not isinstance(v, str) or not v.strip()
                       for v in preauth.get(k, [])) for k in ("writes", "tools"))
                or set(preauth.get("tools", [])) - set(DEFAULT_TOOLS) or type(preauth.get("network", False)) is not bool
                or ("budget_percent" in preauth and (type(preauth["budget_percent"]) is not int
                    or not 1 <= preauth["budget_percent"] <= 100)) or "subtasks" in data or plan):
            raise ContractError("invalid [preauth]: needs writes/tools lists, network bool, budget_percent 1–100; no subtasks/plan")
        preauth = dict(preauth, **({"writes": [_norm(w) for w in preauth["writes"]]} if "writes" in preauth else {}))
        if not isinstance(task.get("writes", []), list) or any(not isinstance(w, str) or not w.strip() for w in task.get("writes", [])):
            raise ContractError("[task].writes must be a list of paths")
        for w in preauth.get("writes", []) + [_norm(w) for w in task.get("writes", [])]:
            if not isinstance(w, str) or not PurePosixPath(w).parts or PurePosixPath(w).is_absolute() or PureWindowsPath(w).is_absolute() or ".." in PurePosixPath(w).parts:
                raise ContractError("[preauth] write paths must stay inside the workspace")
    contract = TaskContract(
        task_id=task_id,
        goal=task["goal"].strip(),
        workspace=_path(base_dir, task["workspace"].replace("{task_id}", task_id)),
        acceptance=Acceptance(command=acc["command"], timeout_s=int(acc.get("timeout_s", 300)), files=tuple(files),
                              parts=tuple(sorted(parts.items()))),
        budget=Budget(**budget_raw),
        tools=tools,
        constraints=tuple(task.get("constraints", ())),
        deliver_to=_path(base_dir, task.get("deliver_to")),
        seed=_path(base_dir, task.get("seed")),
        protected=tuple(task.get("protected", ())),
        network=task.get("network", False),
        max_repairs=max_repairs,
        plan=plan,
        preauth=preauth,
        writes=tuple(_norm(w) for w in task.get("writes", ())) if preauth is not None else (),
    )
    return replace(contract, subtasks=_subtasks(data["subtasks"], contract)) if "subtasks" in data else contract


def load_contract(path: str | Path) -> TaskContract:
    path = Path(path).resolve()
    with path.open("rb") as f:
        data = tomllib.load(f)
    return parse_contract(data, path.parent)
