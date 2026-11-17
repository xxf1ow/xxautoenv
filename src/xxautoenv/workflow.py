"""Inspect native sequential and exclusive-choice graphs without scheduling them."""

from SpiffWorkflow.specs import ExclusiveChoice, Simple, StartTask, WorkflowSpec

from .contracts import ContractError, NodeDeclaration, Ref, Stage
from .node import DeploymentNode


def describe_workflow(spec: WorkflowSpec) -> tuple[NodeDeclaration, ...]:
    """Return each reachable declaration once, with predecessors before consumers.

    This reads native connections and never prepares or executes an operation.
    Cycles are rejected with ContractError.
    """
    seen = set()
    declarations = []

    def visit(node, ancestors):
        if node in ancestors:
            raise ContractError(f"Node {node.name!r} forms an unsupported cycle")
        if node in seen:
            return
        seen.add(node)
        for child in reversed(node.outputs):
            visit(child, ancestors | {node})
        if isinstance(node, DeploymentNode):
            declarations.append(node.declaration)

    if spec.start is None:
        raise ContractError("Workflow requires a native start task")
    visit(spec.start, set())
    return tuple(reversed(declarations))


def validate_workflow(spec: WorkflowSpec) -> None:
    """Validate every native route's inputs, stages, outputs, and final checks.

    Only automatic tasks with sequential connections or native ExclusiveChoice
    branches are supported.
    A reference must have a producer on every route reaching its consumer. Final
    check nodes carry the internal ``_is_check`` marker set by their factory.
    ContractError rejects unsupported or invalid graphs before execution.
    """
    if spec.start is None:
        raise ContractError("Workflow requires a native start task")
    seen = set()
    ids = {}
    stages = {stage: index for index, stage in enumerate(Stage)}

    def visit(node, ancestors, outputs, stage, checked, check_outputs):
        if node in ancestors:
            raise ContractError(f"Node {node.name!r} forms an unsupported cycle")
        if not isinstance(node, DeploymentNode) and type(node) not in (
            StartTask, Simple, ExclusiveChoice,
        ):
            raise ContractError(f"Node {node.name!r} has unsupported native type {type(node).__name__}")
        if node.manual:
            raise ContractError(f"Node {node.name!r} requires unsupported manual execution")
        if len(node.outputs) > 1 and not isinstance(node, ExclusiveChoice):
            raise ContractError(f"Node {node.name!r} has an unsupported parallel split")
        if isinstance(node, ExclusiveChoice) and node.default_task_spec is None:
            raise ContractError(f"Choice {node.name!r} requires a default branch")
        seen.add(node)
        if isinstance(node, DeploymentNode):
            declaration = node.declaration
            previous = ids.setdefault(declaration.id, node)
            if previous is not node or declaration.id != node.name:
                raise ContractError(f"Duplicate or mismatched node identifier {declaration.id!r}")
            if declaration.stage not in stages:
                raise ContractError(f"Node {declaration.id!r} declares an invalid stage")
            current_stage = stages[declaration.stage]
            if current_stage < stage:
                raise ContractError(f"Node {declaration.id!r} moves backward to {declaration.stage.value}")
            stage = current_stage
            is_check = getattr(node, "_is_check", False)
            if is_check and declaration.stage is not Stage.ACCEPTANCE:
                raise ContractError(f"Check {declaration.id!r} must use the acceptance stage")
            for source in declaration.inputs.values():
                if isinstance(source, Ref):
                    if source.name not in outputs:
                        raise ContractError(
                            f"Node {declaration.id!r} requires output {source.name!r} "
                            "without a producer on every incoming route"
                        )
                    if is_check and source.name in check_outputs:
                        raise ContractError(f"Check {declaration.id!r} depends on another check")
            if declaration.output in outputs:
                raise ContractError(f"Node {declaration.id!r} overwrites output {declaration.output!r}")
            outputs = outputs | {declaration.output}
            if is_check:
                checked = True
                check_outputs = check_outputs | {declaration.output}
        if not node.outputs and not checked:
            raise ContractError(f"Route ending at {node.name!r} requires a final acceptance check")
        for child in node.outputs:
            visit(child, ancestors | {node}, outputs, stage, checked, check_outputs)

    visit(spec.start, set(), set(), -1, False, set())
    disconnected = set(spec.task_specs.values()) - seen
    if disconnected:
        names = ", ".join(sorted(node.name for node in disconnected))
        raise ContractError(f"Workflow contains disconnected tasks: {names}")
