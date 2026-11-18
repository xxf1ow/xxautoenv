"""Data shared by deployment nodes, consumers, and run reporting."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum


class ContractError(RuntimeError):
    """A declaration, input, or node result violates the deployment API."""


class Stage(Enum):
    GUIDANCE = "guidance"
    EXECUTION = "execution"
    ACCEPTANCE = "acceptance"


class NodeStatus(Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class CheckVerdict(Enum):
    PASSED = "passed"
    NOT_PASSED = "not_passed"
    INDETERMINATE = "indeterminate"


class RunStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class ActionScope:
    action: str
    target: str


@dataclass(frozen=True)
class NodeResult:
    status: NodeStatus
    value: object
    summary: str


@dataclass(frozen=True)
class CheckResult:
    verdict: CheckVerdict
    summary: str


@dataclass(frozen=True)
class Action:
    """A prepared operation; declared scope checks do not sandbox execution."""

    description: str
    scope: tuple[ActionScope, ...]
    execute: Callable[[], NodeResult]
    changes: bool = False


@dataclass(frozen=True)
class NodeDeclaration:
    id: str
    name: str
    purpose: str
    stage: Stage
    inputs: Mapping[str, object | Ref]
    output: str


class PreparationStatus(Enum):
    """Display-only facts; native task state remains the execution authority."""

    OBSERVED = "observed"
    READY = "ready"
    NO_CHANGE = "no_change"
    PENDING = "pending"
    NOT_SELECTED = "not_selected"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class PreparedStep:
    """Read-only stage row; it contains no executable action or scheduling state."""

    declaration: NodeDeclaration
    status: PreparationStatus
    scope: tuple[ActionScope, ...] = ()


class StepDisposition(Enum):
    """Report-only classification derived after native execution stops."""

    EXECUTED = "executed"
    NOT_SELECTED = "not_selected"
    NOT_EXECUTED = "not_executed"
    PENDING = "pending"


@dataclass(frozen=True)
class Diagnostic:
    node_id: str
    stage: Stage
    error: BaseException
    traceback: str


@dataclass(frozen=True)
class StepReport:
    declaration: NodeDeclaration
    result: NodeResult | None
    diagnostic: Diagnostic | None
    disposition: StepDisposition


@dataclass(frozen=True)
class RunReport:
    status: RunStatus
    steps: tuple[StepReport, ...]
    checks: tuple[CheckResult, ...]

    @property
    def exit_code(self) -> int:
        """Map success, failure, refusal, and keyboard interruption to CLI exits."""
        return {
            RunStatus.SUCCESS: 0,
            RunStatus.FAILED: 1,
            RunStatus.CANCELLED: 2,
            RunStatus.INTERRUPTED: 130,
        }[self.status]
