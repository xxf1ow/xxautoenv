"""Bind per-run interaction and report execution owned by SpiffWorkflow."""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from SpiffWorkflow import Workflow
from SpiffWorkflow.specs import ExclusiveChoice, WorkflowSpec
from SpiffWorkflow.task import TaskState

from .contracts import (
    CheckResult, CheckVerdict, Diagnostic, NodeDeclaration, RunReport,
    RunStatus, StepDisposition, StepReport, PreparedStep, PreparationStatus, Ref, NodeStatus, NodeResult,
)
from .node import DeploymentNode, _RunCancelled, _PlanBoundary
from .workflow import describe_workflow, validate_workflow

if TYPE_CHECKING:
    from .terminal import Terminal


class _Services:
    def __init__(self, terminal, auto_approve, declarations):
        self.terminal = terminal
        self.auto_approve = auto_approve
        self.declarations = declarations
        self.current_stage = None
        self.preparing = False
        self.current_task = None
        self.receipts = []

    def enter(self, task):
        self.current_task = task
        node = task.task_spec
        if (node.declaration.stage is not self.current_stage
                or self.preparing and (not getattr(node, "_is_probe", False)
                                       or getattr(node, "_is_check", False))):
            raise _PlanBoundary(task)

    def before(self, declaration, action):
        if self.preparing:
            self.receipts.append((self.current_task.id, "before"))
            return True
        self.terminal.before(declaration, action)
        return (not action.changes or self.auto_approve
                or self.terminal.confirm("批准此步骤的动作及范围？"))

    def finished(self, declaration, result):
        state = self.current_task.internal_data.get("xxautoenv", {})
        if state.get("preparation_failure_displayed") and result is state.get("prepared"):
            return
        if self.preparing:
            self.receipts.append((self.current_task.id, "finished"))
        else:
            self._finish(self.current_task, result)

    def failed(self, declaration, error):
        state = self.current_task.internal_data.get("xxautoenv", {})
        if state.get("preparation_failure_displayed") and error is state.get("error"):
            return
        if self.preparing:
            self.receipts.append((self.current_task.id, "failed"))
        else:
            self._failure(self.current_task, error)

    def _show_unselected_before(self, task):
        """Publish resolved lower-numbered dispositions before a result receipt."""
        publish = getattr(self.terminal, "_disposition", None)
        if publish is None:
            return
        declaration = task.task_spec.declaration
        earlier = []
        for candidate in self.declarations:
            if candidate.id == declaration.id:
                break
            if candidate.stage is declaration.stage:
                earlier.append(candidate)
        if not earlier:
            return
        workflow = task.workflow
        dispositions = _unexecuted_dispositions(workflow)
        for candidate in earlier:
            if dispositions[workflow.spec.task_specs[candidate.id]] is StepDisposition.NOT_SELECTED:
                publish(candidate, StepDisposition.NOT_SELECTED)

    def _finish(self, task, result):
        self._show_unselected_before(task)
        self.terminal.finished(task.task_spec.declaration, result)

    def _failure(self, task, error):
        state = task.internal_data.get("xxautoenv", {})
        result = state.get("result", state.get("prepared"))
        completed_check = (getattr(task.task_spec, "_is_check", False)
                           and isinstance(result, NodeResult) and isinstance(result.value, CheckResult))
        if completed_check:
            self.terminal.stderr("".join(traceback.format_exception(error)))
        else:
            self._show_unselected_before(task)
            self.terminal.failed(task.task_spec.declaration, error)

    def flush(self, workflow):
        """Receipts retain only task IDs and event kinds; task facts own their payloads."""
        tasks = {task.id: task for task in workflow.get_tasks()}
        receipts, self.receipts = self.receipts, []
        for task_id, kind in receipts:
            task = tasks[task_id]
            declaration = task.task_spec.declaration
            state = task.internal_data["xxautoenv"]
            if kind == "preparation_failed":
                self.terminal.failed(declaration, state["error"], preparation_result=state["prepared"])
                state["preparation_failure_displayed"] = True
            elif kind == "failed":
                self._failure(task, state["error"])
            elif kind == "before":
                self.terminal.before(declaration, state["action"])
            else:
                self._finish(task, state["result"])

    def plan(self, workflow):
        """Prepare definite native tasks with known inputs without advancing them."""
        for task in workflow.get_tasks():
            node = task.task_spec
            if (not isinstance(node, DeploymentNode) or node.declaration.stage is not self.current_stage
                    or task.state not in (TaskState.READY, TaskState.FUTURE)):
                continue
            ancestry, ancestor = [], task
            while ancestor is not None:
                ancestry.append(ancestor)
                ancestor = ancestor.parent
            data = {}
            for ancestor in reversed(ancestry):
                data.update(ancestor.data)
            if any(isinstance(source, Ref) and source.name not in data
                   for source in node.declaration.inputs.values()):
                continue
            self.current_task = task
            try:
                node._prepare_task(task, data)
            except BaseException as error:
                self.failed(node.declaration, error)
                raise
            if "error" in task.internal_data["xxautoenv"]:
                self.receipts.append((task.id, "preparation_failed"))
        dispositions = _unexecuted_dispositions(workflow)
        tasks = {task.task_spec: task for task in workflow.get_tasks()}
        rows = []
        for declaration in self.declarations:
            if declaration.stage is not self.current_stage:
                continue
            node = workflow.spec.task_specs[declaration.id]
            task = tasks.get(node)
            state = {} if task is None else task.internal_data.get("xxautoenv", {})
            prepared = state.get("prepared")
            if task is not None and task.state == TaskState.COMPLETED:
                status = PreparationStatus.OBSERVED
            elif "error" in state:
                status = PreparationStatus.INDETERMINATE
            elif getattr(prepared, "status", None) is NodeStatus.SKIPPED:
                status = PreparationStatus.NO_CHANGE
            elif "action" in state:
                status = PreparationStatus.READY
            elif dispositions[node] is StepDisposition.NOT_SELECTED:
                status = PreparationStatus.NOT_SELECTED
            else:
                status = PreparationStatus.PENDING
            rows.append(PreparedStep(declaration, status, getattr(prepared, "scope", ())))
        try:
            self.terminal.stage(self.current_stage, tuple(rows))
        except BaseException as error:
            self.current_task.internal_data.setdefault("xxautoenv", {})["error"] = error
            self.failed(self.current_task.task_spec.declaration, error)
            raise
        self.flush(workflow)

    def drive(self, workflow):
        """Let native execution choose the next task; pause only for stage plans."""
        while True:
            try:
                workflow.run_all()
            except _PlanBoundary as boundary:
                stage = boundary.task.task_spec.declaration.stage
                if stage is not self.current_stage:
                    if self.preparing:
                        self.plan(workflow)
                    if self.current_stage is not None:
                        self.flush(workflow)
                        self.terminal.stage_finished(
                            self.current_stage,
                            _aggregate(workflow, self.declarations, RunStatus.SUCCESS).steps,
                        )
                    self.current_stage = stage
                    self.preparing = True
                else:
                    self.plan(workflow)
                    self.preparing = False
            else:
                if self.preparing:
                    self.plan(workflow)
                return

    def prompt(self, message, default):
        return self.terminal.prompt(message, default)

    def stdout(self, text):
        self.terminal.stdout(text)

    def stderr(self, text):
        self.terminal.stderr(text)


def _unexecuted_dispositions(workflow: Workflow) -> dict[object, StepDisposition]:
    """Read resolved native choices; unknown routes remain pending in the report."""
    selected = {}
    for task in workflow.get_tasks():
        if isinstance(task.task_spec, ExclusiveChoice) and task.state == TaskState.COMPLETED:
            selected[task.task_spec] = {child.task_spec for child in task.children}
    routes = {}

    def visit(node, pending):
        routes.setdefault(node, set()).add(pending)
        if isinstance(node, ExclusiveChoice):
            if node in selected:
                children = selected[node]
            else:
                children = node.outputs
                pending = True
        else:
            children = node.outputs
        for child in children:
            visit(child, pending)

    visit(workflow.spec.start, False)
    return {
        node: (
            StepDisposition.NOT_EXECUTED if False in routes.get(node, ())
            else StepDisposition.PENDING if node in routes
            else StepDisposition.NOT_SELECTED
        )
        for node in workflow.spec.task_specs.values()
    }


def _aggregate(workflow: Workflow, declarations: tuple[NodeDeclaration, ...],
               status: RunStatus) -> RunReport:
    dispositions = _unexecuted_dispositions(workflow)
    states = {}
    for task in workflow.get_tasks():
        state = task.internal_data.get("xxautoenv")
        if state:
            states[task.task_spec] = state
    steps = []
    checks = []
    for declaration in declarations:
        node = workflow.spec.task_specs[declaration.id]
        state = states.get(node, {})
        result = state.get("result")
        error = state.get("error")
        if (result is None and error is not None and getattr(node, "_is_check", False)
                and isinstance(state.get("prepared"), NodeResult)):
            result = state.get("prepared")
        diagnostic = None if error is None else Diagnostic(
            declaration.id, declaration.stage, error,
            "".join(traceback.format_exception(error)),
        )
        disposition = (
            StepDisposition.EXECUTED if result is not None or error is not None
            else dispositions[node]
        )
        steps.append(StepReport(declaration, result, diagnostic, disposition))
        if getattr(node, "_is_check", False) and result is not None:
            if isinstance(result.value, CheckResult):
                checks.append(result.value)
    if status is RunStatus.SUCCESS and any(
        check.verdict is not CheckVerdict.PASSED for check in checks
    ):
        status = RunStatus.FAILED
    return RunReport(status, tuple(steps), tuple(checks))


def run(spec: WorkflowSpec, *, auto_approve: bool = False,
        terminal: Terminal | None = None) -> RunReport:
    """Validate and execute a fresh native Workflow, then return its evidence.

    Each call rebinds interaction services and observes operations again. Ordinary
    node errors retain their exception and cause in diagnostics; KeyboardInterrupt
    returns INTERRUPTED, refusal returns CANCELLED, and SystemExit propagates.
    Incomplete native execution returns FAILED. Invalid declarations raise
    ContractError before execution. Concurrent runs sharing one spec are unsupported.
    """
    validate_workflow(spec)
    declarations = describe_workflow(spec)
    if terminal is None:
        from .terminal import Terminal
        terminal = Terminal()
    services = _Services(terminal, auto_approve, declarations)
    nodes = [node for node in spec.task_specs.values() if isinstance(node, DeploymentNode)]
    terminal.begin(declarations)
    for node in nodes:
        node.bind(services)
    try:
        workflow = Workflow(spec)
        status = RunStatus.SUCCESS
        try:
            services.drive(workflow)
            if not workflow.is_completed():
                status = RunStatus.FAILED
        except _RunCancelled:
            status = RunStatus.CANCELLED
        except KeyboardInterrupt:
            status = RunStatus.INTERRUPTED
        except Exception as error:
            if not any(
                task.internal_data.get("xxautoenv", {}).get("error") is error
                for task in workflow.get_tasks()
            ):
                raise
            status = RunStatus.FAILED
        services.flush(workflow)
        report = _aggregate(workflow, declarations, status)
        terminal.report(report)
        return report
    finally:
        if "workflow" in locals():
            services.flush(workflow)
        for node in nodes:
            node.services = None


def main(
    build: Callable[[argparse.Namespace], WorkflowSpec],
    *,
    argv: Sequence[str] | None = None,
    configure: Callable[[argparse.ArgumentParser], None] | None = None,
) -> int:
    """Parse shared -y and consumer arguments, build the native graph, and run.

    Consumers register their own arguments through configure. Return 0 for
    success, 1 for failure, 2 for refusal, or 130 for KeyboardInterrupt.
    ArgumentParser retains its standard help and invalid-argument exit behavior.
    Ordinary build/validation errors print their full traceback and return 1;
    SystemExit propagates.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-y', action='store_true', dest='auto_approve',
                        help='自动批准变更动作；配置仍需输入')
    if configure is not None:
        configure(parser)
    args = parser.parse_args(argv)
    try:
        return run(build(args), auto_approve=args.auto_approve).exit_code
    except KeyboardInterrupt:
        traceback.print_exc(file=sys.stderr)
        return 130
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
