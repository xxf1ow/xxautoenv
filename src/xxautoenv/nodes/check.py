"""Construct independent, read-only acceptance checks."""

from collections.abc import Callable, Mapping

from SpiffWorkflow.specs import WorkflowSpec

from ..contracts import (
    Action, CheckResult, CheckVerdict, ContractError, NodeDeclaration, NodeResult,
    NodeStatus, Ref, Stage,
)
from ..node import DeploymentNode


def check_node(
    spec: WorkflowSpec,
    id: str,
    *,
    name: str,
    purpose: str,
    stage: Stage,
    inputs: Mapping[str, object | Ref],
    output: str,
    check: Callable[..., CheckResult],
) -> DeploymentNode:
    """Run a check in ACCEPTANCE and publish its CheckResult.

    NOT_PASSED and ordinary preparation/execution exceptions complete the native
    task so independent checks continue. Exceptions retain diagnostics and yield
    INDETERMINATE. ContractError and interruptions stop the run. Check actions
    cannot declare changes; consumers remain responsible for read-only functions.
    """
    if stage is not Stage.ACCEPTANCE:
        raise ContractError(f'核查 {id!r} 必须位于 acceptance 阶段')
    declaration = NodeDeclaration(id, name, purpose, stage, dict(inputs), output)

    def prepare(**values: object) -> Action:
        def execute() -> NodeResult:
            result = check(**values)
            if not isinstance(result, CheckResult) or not isinstance(result.verdict, CheckVerdict):
                raise ContractError(f'核查 {id!r} 必须返回包含 CheckVerdict 的 CheckResult')
            return NodeResult(NodeStatus.SUCCESS, result, result.summary)

        return Action(purpose, (), execute)

    node = DeploymentNode(spec, declaration, prepare)
    node._is_check = True
    return node
