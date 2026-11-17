"""Wrap ordinary functions and consumer-defined prepared operations."""

from collections.abc import Callable, Mapping

from SpiffWorkflow.specs import WorkflowSpec

from ..contracts import Action, ActionScope, NodeDeclaration, NodeResult, NodeStatus, Ref, Stage
from ..node import DeploymentNode


def function_node(
    spec: WorkflowSpec,
    id: str,
    *,
    name: str,
    purpose: str,
    stage: Stage,
    inputs: Mapping[str, object | Ref],
    output: str,
    function: Callable[..., object],
    summary: Callable[[object], str],
) -> DeploymentNode:
    """Run a read-only probe once when native execution reaches it, including stage preparation."""
    declaration = NodeDeclaration(id, name, purpose, stage, dict(inputs), output)

    def prepare(**values: object) -> Action:
        def execute() -> NodeResult:
            value = function(**values)
            return NodeResult(NodeStatus.SUCCESS, value, summary(value))

        return Action(purpose, (), execute)

    node = DeploymentNode(spec, declaration, prepare)
    node._is_probe = True
    return node


def action_node(
    spec: WorkflowSpec,
    id: str,
    *,
    name: str,
    purpose: str,
    stage: Stage,
    inputs: Mapping[str, object | Ref],
    output: str,
    prepare: Callable[..., Action | NodeResult],
    allows: Callable[[ActionScope], bool],
) -> DeploymentNode:
    """Bind a consumer's prepared Action or SKIPPED result to a native node."""
    declaration = NodeDeclaration(id, name, purpose, stage, dict(inputs), output)
    return DeploymentNode(spec, declaration, prepare, allows=allows)
