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
    Stage,
)


class RunServices(Protocol):
    """Per-run interaction capabilities used by deployment nodes."""

    def before(self, declaration: NodeDeclaration, action: Action) -> bool: ...
    def finished(self, declaration: NodeDeclaration, result: NodeResult) -> None: ...
    def failed(self, declaration: NodeDeclaration, error: BaseException) -> None: ...
    def prompt(self, message: str, default: str | None) -> str: ...
    def stdout(self, text: str) -> None: ...
    def stderr(self, text: str) -> None: ...


class _PlanBoundary(BaseException):
    """Pause before execution without SpiffWorkflow marking the task ERROR."""

    def __init__(self, task):
        self.task = task


class _RunCancelled(RuntimeError):
    """Stop native execution after the user refuses a prepared action."""


def _read_only(scope: ActionScope) -> bool:
    return False


class DeploymentNode(Simple):
    """Resolve declared keyword inputs and execute one approved Action.

    Bind services before running a native Workflow. A prepare callable returns
    an Action or a SKIPPED NodeResult. Missing inputs and invalid operations
    raise ContractError; operation exceptions retain their original identity.
    Preparation is read-only and binds once per native task, before the stage
    plan when its route and inputs are known, otherwise at native execution.
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
            raise ContractError(f"节点标识重复：{declaration.id!r}")
        super().__init__(spec, declaration.id)
        self.declaration = declaration
        self.prepare = prepare
        self.allows = allows
        self.services: RunServices | None = None

    def bind(self, services: RunServices) -> None:
        """Bind the current run's services; concurrent shared-spec runs are unsupported."""
        self.services = services

    def _prepare_task(self, task: Task, data: dict) -> Action | NodeResult:
        """Bind once to this native task; data contains only observed ancestor outputs."""
        state = task.internal_data.setdefault("xxautoenv", {})
        if "prepared" in state:
            return state["prepared"]
        if "error" in state:
            raise state["error"]
        operation_running = False
        try:
            inputs = {}
            for name, source in self.declaration.inputs.items():
                if isinstance(source, Ref):
                    if source.name not in data:
                        raise ContractError(
                            f"节点 {self.declaration.id!r} 的输入 {name!r} 缺少产出 {source.name!r}"
                        )
                    inputs[name] = data[source.name]
                else:
                    inputs[name] = source
            state["inputs"] = inputs
            operation_running = True
            prepared = self.prepare(**inputs)
            operation_running = False
            if isinstance(prepared, Action):
                state["action"] = prepared
                self._validate_action(prepared)
            elif not isinstance(prepared, NodeResult) or prepared.status is not NodeStatus.SKIPPED:
                raise ContractError(f"节点 {self.declaration.id!r} 的 prepare 必须返回 Action 或 SKIPPED NodeResult")
            state["prepared"] = prepared
            return prepared
        except BaseException as error:
            state["error"] = error
            if self._check_error(error, operation_running):
                verdict = CheckResult(CheckVerdict.INDETERMINATE, str(error))
                state["prepared"] = NodeResult(NodeStatus.SUCCESS, verdict, verdict.summary)
                return state["prepared"]
            raise

    def _check_error(self, error: BaseException, operation_running: bool) -> bool:
        return (getattr(self, "_is_check", False) and operation_running
                and isinstance(error, Exception) and not isinstance(error, ContractError))

    def _run_hook(self, task: Task) -> bool:
        services = self.services
        if services is None:
            raise ContractError(f"节点 {self.declaration.id!r} 尚未绑定 RunServices")
        # Direct native consumers need only RunServices; the runner owns stage pauses.
        if hasattr(services, "enter"):
            services.enter(task)
        state = task.internal_data.setdefault("xxautoenv", {})
        operation_running = False
        try:
            prepared = self._prepare_task(task, task.data)
            if isinstance(prepared, NodeResult):
                result = prepared
                if "error" in state:
                    services.failed(self.declaration, state["error"])
            else:
                if not services.before(self.declaration, prepared):
                    result = NodeResult(NodeStatus.CANCELLED, None, "已拒绝动作")
                    state["result"] = result
                    services.finished(self.declaration, result)
                    raise _RunCancelled(f"节点 {self.declaration.id!r}：已拒绝动作")
                operation_running = True
                result = prepared.execute()
                operation_running = False
                if not isinstance(result, NodeResult) or result.status not in (NodeStatus.SUCCESS, NodeStatus.SKIPPED):
                    raise ContractError(f"节点 {self.declaration.id!r} 的 Action.execute 必须返回 SUCCESS 或 SKIPPED NodeResult")
            state["result"] = result
            task.data[self.declaration.output] = result.value
            services.finished(self.declaration, result)
            return True
        except _RunCancelled:
            raise
        except BaseException as error:
            state["error"] = error
            if self._check_error(error, operation_running):
                verdict = CheckResult(CheckVerdict.INDETERMINATE, str(error))
                result = NodeResult(NodeStatus.SUCCESS, verdict, verdict.summary)
                state["result"] = result
                task.data[self.declaration.output] = verdict
                services.failed(self.declaration, error)
                services.finished(self.declaration, result)
                return True
            services.failed(self.declaration, error)
            raise

    def _validate_action(self, action: Action) -> None:
        if self.declaration.stage is Stage.ACCEPTANCE and action.changes:
            raise ContractError(
                f"核查阶段节点 {self.declaration.id!r} 不能执行变更 Action"
            )
        if not action.changes and action.scope:
            raise ContractError(f"节点 {self.declaration.id!r} 的只读 Action 必须使用空范围")
        if action.changes and not action.scope:
            raise ContractError(f"节点 {self.declaration.id!r} 的变更 Action 必须声明范围")
        if any(not self.allows(scope) for scope in action.scope):
            raise ContractError(f"节点 {self.declaration.id!r} 的 Action 范围超出 allows 声明")
