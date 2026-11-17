"""Bind local file delivery to native deployment approval and outputs."""

from collections.abc import Mapping

from SpiffWorkflow.specs import WorkflowSpec

from ..contracts import Action, ActionScope, NodeDeclaration, NodeResult, Ref, Stage
from ..node import DeploymentNode
from ..operations.files import prepare_file


def file_node(
    spec: WorkflowSpec,
    id: str,
    *,
    name: str,
    purpose: str,
    stage: Stage,
    inputs: Mapping[str, object | Ref],
    output: str,
    mode: int = 0o644,
) -> DeploymentNode:
    """Publish a FileArtifact after approved delivery or a matching-file skip.

    Inputs supply source and destination Paths. Each preparation permits only
    that destination write and the explicitly missing parent directories.
    Source or destination changes during approval fail the bound operation.
    """
    declaration = NodeDeclaration(id, name, purpose, stage, dict(inputs), output)
    allowed: tuple[ActionScope, ...] = ()

    def prepare(*, source, destination) -> Action | NodeResult:
        nonlocal allowed
        prepared = prepare_file(source, destination, mode)
        allowed = prepared.scope if isinstance(prepared, Action) else ()
        return prepared

    return DeploymentNode(spec, declaration, prepare, allows=lambda item: item in allowed)
