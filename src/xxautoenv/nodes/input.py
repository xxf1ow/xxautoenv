"""Resolve consumer configuration from a supplied value or interactive input."""

from collections.abc import Callable, Mapping

from SpiffWorkflow.specs import WorkflowSpec

from ..contracts import Action, ContractError, NodeDeclaration, NodeResult, NodeStatus, Ref, Stage
from ..node import DeploymentNode


def input_node(
    spec: WorkflowSpec,
    id: str,
    *,
    name: str,
    purpose: str,
    stage: Stage,
    inputs: Mapping[str, object | Ref],
    output: str,
    prompt: str,
    default: str | None,
    validate: Callable[[str], object],
) -> DeploymentNode:
    """Validate inputs['value'], or ask when the value is omitted or None.

    An empty string is supplied configuration. The validator's return value,
    including None, is the output. ValueError displays a correction and asks
    again; unavailable interactive input and EOF fail the step. Auto approval
    never supplies configuration answers.
    """
    if set(inputs) - {'value'}:
        raise ContractError(f'Input node {id!r} accepts only the value input')
    declaration = NodeDeclaration(id, name, purpose, stage, dict(inputs), output)

    def prepare(value: object = None) -> Action:
        if value is not None and not isinstance(value, str):
            raise ContractError(f'Input node {id!r} requires a string or None')

        def execute() -> NodeResult:
            services = node.services
            if services is None:
                raise ContractError(f'Input node {id!r} has no bound RunServices')
            answer = value
            while True:
                if answer is None:
                    answer = services.prompt(prompt, default)
                try:
                    normalized = validate(answer)
                except ValueError as error:
                    services.stderr(f'{error}\n')
                    answer = None
                    continue
                return NodeResult(NodeStatus.SUCCESS, normalized, 'Configuration accepted')

        return Action(purpose, (), execute)

    node = DeploymentNode(spec, declaration, prepare)
    return node
