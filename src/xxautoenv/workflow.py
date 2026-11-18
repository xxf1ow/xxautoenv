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
            raise ContractError(f"节点 {node.name!r} 形成不支持的循环")
        if node in seen:
            return
        seen.add(node)
        for child in reversed(node.outputs):
            visit(child, ancestors | {node})
        if isinstance(node, DeploymentNode):
            declarations.append(node.declaration)

    if spec.start is None:
        raise ContractError("流程必须包含原生起始任务")
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
        raise ContractError("流程必须包含原生起始任务")
    seen = set()
    ids = {}
    stages = {stage: index for index, stage in enumerate(Stage)}

    def visit(node, ancestors, outputs, stage, checked, check_outputs):
        if node in ancestors:
            raise ContractError(f"节点 {node.name!r} 形成不支持的循环")
        if not isinstance(node, DeploymentNode) and type(node) not in (
            StartTask, Simple, ExclusiveChoice,
        ):
            raise ContractError(f"节点 {node.name!r} 使用不支持的原生类型 {type(node).__name__}")
        if node.manual:
            raise ContractError(f"节点 {node.name!r} 要求不支持的手动执行")
        if len(node.outputs) > 1 and not isinstance(node, ExclusiveChoice):
            raise ContractError(f"节点 {node.name!r} 包含不支持的并行分支")
        if isinstance(node, ExclusiveChoice) and node.default_task_spec is None:
            raise ContractError(f"分支 {node.name!r} 必须声明默认路径")
        seen.add(node)
        if isinstance(node, DeploymentNode):
            declaration = node.declaration
            previous = ids.setdefault(declaration.id, node)
            if previous is not node or declaration.id != node.name:
                raise ContractError(f"节点标识重复或不匹配：{declaration.id!r}")
            if declaration.stage not in stages:
                raise ContractError(f"节点 {declaration.id!r} 声明的阶段无效")
            current_stage = stages[declaration.stage]
            if current_stage < stage:
                raise ContractError(f"节点 {declaration.id!r} 倒退到阶段 {declaration.stage.value}")
            stage = current_stage
            is_check = getattr(node, "_is_check", False)
            if is_check and declaration.stage is not Stage.ACCEPTANCE:
                raise ContractError(f"核查 {declaration.id!r} 必须位于 acceptance 阶段")
            for source in declaration.inputs.values():
                if isinstance(source, Ref):
                    if source.name not in outputs:
                        raise ContractError(
                            f"节点 {declaration.id!r} 引用产出 {source.name!r}，"
                            "但并非每条上游路径都有生产者"
                        )
                    if is_check and source.name in check_outputs:
                        raise ContractError(f"核查 {declaration.id!r} 不能依赖另一项核查")
            if declaration.output in outputs:
                raise ContractError(f"节点 {declaration.id!r} 覆盖产出 {declaration.output!r}")
            outputs = outputs | {declaration.output}
            if is_check:
                checked = True
                check_outputs = check_outputs | {declaration.output}
        if not node.outputs and not checked:
            raise ContractError(f"以 {node.name!r} 结束的路径必须包含最终核查")
        for child in node.outputs:
            visit(child, ancestors | {node}, outputs, stage, checked, check_outputs)

    visit(spec.start, set(), set(), -1, False, set())
    disconnected = set(spec.task_specs.values()) - seen
    if disconnected:
        names = ", ".join(sorted(node.name for node in disconnected))
        raise ContractError(f"流程包含未连接的任务：{names}")
