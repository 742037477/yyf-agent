"""G3 action gates. Every effectful proposal passes the whole chain before it may run.

A gate only reads facts (contract, workspace, ledger-derived usage) and returns allow/deny.
Gates never execute anything. Add a gate = add a class with `name` and `check`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .contract import TaskContract, in_scope
from .tools import TOOLS, ScopeError, Workspace
from .types import GateDecision, Proposal


@dataclass(frozen=True)
class GateContext:
    contract: TaskContract
    workspace: Workspace
    usage: dict[str, Any]


class Gate(Protocol):
    name: str

    def check(self, ctx: GateContext, p: Proposal) -> GateDecision: ...


def _allow(name: str) -> GateDecision:
    return GateDecision(True, name)


def _deny(name: str, reason: str) -> GateDecision:
    return GateDecision(False, name, reason)


class ToolAllowlistGate:
    name = "tool_allowlist"

    def check(self, ctx, p):
        if p.tool not in TOOLS:
            return _deny(self.name, f"unknown tool '{p.tool}'")
        if p.tool not in ctx.contract.tools:
            return _deny(self.name, f"tool '{p.tool}' is not allowed by this task contract")
        return _allow(self.name)


class ArgsGate:
    name = "args"

    def check(self, ctx, p):
        spec = TOOLS[p.tool]
        missing = [k for k in spec.required if k not in p.args]
        if missing:
            return _deny(self.name, f"missing required args {missing}")
        extra = [k for k in p.args if k not in spec.params]
        if extra:
            return _deny(self.name, f"unknown args {extra}")
        for k, v in p.args.items():
            want_int = spec.params[k].startswith("integer")
            if want_int and not (isinstance(v, int) and not isinstance(v, bool)):
                return _deny(self.name, f"arg '{k}' must be an integer")
            if not want_int and not isinstance(v, str):
                return _deny(self.name, f"arg '{k}' must be a string")
        return _allow(self.name)


class PathScopeGate:
    name = "path_scope"

    def check(self, ctx, p):
        for k in TOOLS[p.tool].path_params:
            if k in p.args:
                try:
                    ctx.workspace.resolve(p.args[k])
                except ScopeError as e:
                    return _deny(self.name, str(e))
        return _allow(self.name)


class ProtectedPathGate:
    """Files named in contract.protected (e.g. the acceptance test) cannot be written by the worker."""
    name = "protected_path"

    def check(self, ctx, p):
        if p.tool not in ("write_file", "edit_file") or not ctx.contract.protected:
            return _allow(self.name)
        target = ctx.workspace.resolve(p.args["path"])
        for rel in ctx.contract.protected:
            if target == ctx.workspace.resolve(rel):
                return _deny(self.name, f"'{rel}' is protected by the task contract")
        return _allow(self.name)


class WriteScopeGate:
    """A subtask writes only inside its declared scope (contract.writes); empty scope = the whole workspace."""
    name = "write_scope"

    def check(self, ctx, p):
        if p.tool not in ("write_file", "edit_file") or not ctx.contract.writes:
            return _allow(self.name)
        rel = ctx.workspace.resolve(p.args["path"]).relative_to(ctx.workspace.root).as_posix()
        if in_scope(rel, ctx.contract.writes):
            return _allow(self.name)
        return _deny(self.name, f"'{rel}' is outside this task's write scope {list(ctx.contract.writes)}")


class CommandBudgetGate:
    name = "command_budget"

    def check(self, ctx, p):
        if p.tool == "run_command" and ctx.usage["commands"] >= ctx.contract.budget.max_commands:
            return _deny(self.name, f"command budget exhausted ({ctx.contract.budget.max_commands})")
        return _allow(self.name)


class TrustChainGate:
    """G0–G2: before every action the whole chain Root → charter → credential → contract is verified again."""
    name = "trust_chain"

    def __init__(self, verify):
        self.verify = verify          # () -> None, raises TrustError

    def check(self, ctx, p):
        try:
            self.verify()
        except ValueError as e:       # TrustError
            return _deny(self.name, str(e))
        return _allow(self.name)


DEFAULT_GATES: tuple[Gate, ...] = (ToolAllowlistGate(), ArgsGate(), PathScopeGate(), ProtectedPathGate(),
                                   WriteScopeGate(), CommandBudgetGate())


def check_all(gates: tuple[Gate, ...], ctx: GateContext, p: Proposal) -> GateDecision:
    """First deny wins. Order matters: later gates may assume earlier ones passed."""
    for g in gates:
        d = g.check(ctx, p)
        if not d.allow:
            return d
    return GateDecision(True, "all")


def model_budget_denial(contract: TaskContract, usage: dict[str, Any], role: str) -> str | None:
    """Gate for model calls (they cost money too). Returns a reason if the next call is not allowed."""
    b = contract.budget
    if role == "worker" and usage["rounds"] >= b.max_rounds:
        return f"round budget exhausted ({b.max_rounds})"
    if usage["model_calls"] >= b.max_model_calls:
        return f"model call budget exhausted ({b.max_model_calls})"
    if b.max_tokens is not None and usage["tokens"] >= b.max_tokens:
        return f"token budget exhausted ({usage['tokens']}/{b.max_tokens})"
    return None
