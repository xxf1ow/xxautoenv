"""Adapt deployment operations to native SpiffWorkflow task execution."""

from collections.abc import Callable
from typing import Protocol

from SpiffWorkflow.specs import Simple, WorkflowSpec
from SpiffWorkflow.task import Task

from .contracts import (
    Action,
    ActionScope,
    CheckResult,
    CheckVerdict,
    ContractError,
    NodeDeclaration,
    NodeResult,
    NodeStatus,
    Ref,
)


class RunServices(Protocol):
    """Per-run interaction capabilities used by deployment nodes."""

    def before(self, declaration: NodeDeclaration, action: Action) -> bool: ...
    def finished(self, declaration: NodeDeclaration, result: NodeResult) -> None: ...
    def failed(self, declaration: NodeDeclaration, error: BaseException) -> None: ...
    def prompt(self, message: str, default: str | None) -> str: ...
    def stdout(self, text: str) -> None: ...
    def stderr(self, text: str) -> None: ...


class _RunCancelled(RuntimeError):
    """Stop native execution after the user refuses a prepared action."""


def _read_only(scope: ActionScope) -> bool:
    return False


class DeploymentNode(Simple):
    """Resolve declared keyword inputs and execute one approved Action.

    Bind services before running a native Workflow. A prepare callable returns
    an Action or a SKIPPED NodeResult. Missing inputs and invalid operations
    raise ContractError; operation exceptions retain their original identity.
    Each task stores its inputs, action, result, and error in internal_data.
    Acceptance checks convert ordinary prepare/execute errors to INDETERMINATE
    results and complete natively; ContractError and interruptions still stop.
    """

    def __init__(
        self,
        spec: WorkflowSpec,
        declaration: NodeDeclaration,
        prepare: Callable[..., Action | NodeResult],
        *,
        allows: Callable[[ActionScope], bool] = _read_only,
    ):
        if declaration.id in spec.task_specs:
            raise ContractError(f"Duplicate node identifier {declaration.id!r}")
        super().__init__(spec, declaration.id)
        self.declaration = declaration
        self.prepare = prepare
        self.allows = allows
        self.services: RunServices | None = None

    def bind(self, services: RunServices) -> None:
        """Bind the current run's services; concurrent shared-spec runs are unsupported."""
        self.services = services

    def _run_hook(self, task: Task) -> bool:
        services = self.services
        if services is None:
            raise ContractError(f"Node {self.declaration.id!r} has no bound RunServices")
        state = task.internal_data.setdefault("xxautoenv", {})
        operation_running = False
        try:
            inputs = {}
            for name, source in self.declaration.inputs.items():
                if isinstance(source, Ref):
                    if source.name not in task.data:
                        raise ContractError(
                            f"Node {self.declaration.id!r} input {name!r} requires "
                            f"missing output {source.name!r}"
                        )
                    inputs[name] = task.data[source.name]
                else:
                    inputs[name] = source
            state["inputs"] = inputs
            operation_running = True
            prepared = self.prepare(**inputs)
            operation_running = False
            if isinstance(prepared, NodeResult):
                if prepared.status is not NodeStatus.SKIPPED:
                    raise ContractError(
                        f"Node {self.declaration.id!r} prepare may only return "
                        "an Action or a SKIPPED NodeResult"
                    )
                result = prepared
            elif isinstance(prepared, Action):
                state["action"] = prepared
                self._validate_action(prepared)
                if not services.before(self.declaration, prepared):
                    result = NodeResult(NodeStatus.CANCELLED, None, "Action declined")
                    state["result"] = result
                    services.finished(self.declaration, result)
                    raise _RunCancelled(f"Node {self.declaration.id!r}: action declined")
                operation_running = True
                result = prepared.execute()
                operation_running = False
                if not isinstance(result, NodeResult):
                    raise ContractError(
                        f"Node {self.declaration.id!r} Action.execute must return NodeResult"
                    )
                if result.status not in (NodeStatus.SUCCESS, NodeStatus.SKIPPED):
                    raise ContractError(
                        f"Node {self.declaration.id!r} Action.execute must return "
                        "a SUCCESS or SKIPPED NodeResult"
                    )
            else:
                raise ContractError(
                    f"Node {self.declaration.id!r} prepare must return Action or SKIPPED NodeResult"
                )
            state["result"] = result
            task.data[self.declaration.output] = result.value
            services.finished(self.declaration, result)
            return True
        except _RunCancelled:
            raise
        except BaseException as error:
            state["error"] = error
            services.failed(self.declaration, error)
            if (getattr(self, "_is_check", False) and operation_running
                    and isinstance(error, Exception) and not isinstance(error, ContractError)):
                verdict = CheckResult(CheckVerdict.INDETERMINATE, str(error))
                result = NodeResult(NodeStatus.SUCCESS, verdict, verdict.summary)
                state["result"] = result
                task.data[self.declaration.output] = verdict
                services.finished(self.declaration, result)
                return True
            raise

    def _validate_action(self, action: Action) -> None:
        if getattr(self, "_is_check", False) and action.changes:
            raise ContractError(f"Check {self.declaration.id!r} cannot execute a changing Action")
        if not action.changes and action.scope:
            raise ContractError(f"Node {self.declaration.id!r} read-only Action must have empty scope")
        if action.changes and not action.scope:
            raise ContractError(f"Node {self.declaration.id!r} changing Action requires scope")
        if any(not self.allows(scope) for scope in action.scope):
            raise ContractError(f"Node {self.declaration.id!r} Action scope exceeds declared allows")
