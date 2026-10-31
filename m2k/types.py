"""Core value types shared by every module. Keep this file small and boring."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """Final flow state of a task. FAIL = proven failure, UNKNOWN = cannot be decided."""
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class EffectStatus(str, Enum):
    COMPLETED = "COMPLETED"   # effect happened and we have its result
    FAILED = "FAILED"         # effect attempted and definitely failed
    UNKNOWN = "UNKNOWN"       # we cannot tell whether / how the effect happened (e.g. timeout)


class WorkerStatus(str, Enum):
    FINISHED = "FINISHED"                  # worker called `finish`
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # a hard budget limit was hit
    AWAITING_OWNER = "AWAITING_OWNER"      # a proposal needs the owner's answer or approval
    STALLED = "STALLED"                    # no progress (repeated identical failures / bad output)
    MODEL_ERROR = "MODEL_ERROR"            # model adapter raised
    TRUST_BROKEN = "TRUST_BROKEN"          # the Root → charter → credential chain no longer holds
    SUBTASK_FAILED = "SUBTASK_FAILED"      # a subtask did not PASS; later subtasks did not run
    PLAN_REJECTED = "PLAN_REJECTED"        # no planner proposal passed the plan rules (T27)


@dataclass(frozen=True)
class Proposal:
    """What the model wants to do next. A proposal is data, never an effect."""
    tool: str
    args: dict[str, Any]
    thought: str = ""

    def key(self) -> str:
        import json
        return json.dumps({"tool": self.tool, "args": self.args}, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class GateDecision:
    allow: bool
    gate: str
    reason: str = ""


@dataclass(frozen=True)
class Effect:
    status: EffectStatus
    output: str
    detail: dict[str, Any] = field(default_factory=dict)
