"""Bind prepared local commands to native deployment nodes."""

import shlex
from collections.abc import Callable, Mapping

from SpiffWorkflow.specs import WorkflowSpec

from ..contracts import Action, ActionScope, NodeDeclaration, NodeResult, NodeStatus, Ref, Stage
from ..node import DeploymentNode
from ..operations.command import CommandRequest, execute_command


def command_node(
    spec: WorkflowSpec,
    id: str,
    *,
    name: str,
    purpose: str,
    stage: Stage,
    inputs: Mapping[str, object | Ref],
    output: str,
    request: Callable[..., CommandRequest],
    changes: bool,
    scope: tuple[ActionScope, ...],
) -> DeploymentNode:
    """Prepare one command before approval and publish its CommandResult.

    The request callable receives declared inputs once per execution. Failures
    retain CommandError and its request/result in the node diagnostic.
    """
    declaration = NodeDeclaration(id, name, purpose, stage, dict(inputs), output)

    def prepare(**values: object) -> Action:
        bound = request(**values)

        def execute() -> NodeResult:
            result = execute_command(bound, stdout=node.services.stdout, stderr=node.services.stderr)
            return NodeResult(NodeStatus.SUCCESS, result, f'Command exited with {result.returncode}')

        description = f'{shlex.join(bound.argv)} (cwd: {bound.cwd}, env: {dict(bound.env)!r})'
        return Action(description, scope, execute, changes)

    node = DeploymentNode(spec, declaration, prepare, allows=lambda item: item in scope)
    return node
