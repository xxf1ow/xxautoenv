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
    RunStatus, StepDisposition, StepReport,
)
from .node import DeploymentNode, _RunCancelled
from .workflow import describe_workflow, validate_workflow

if TYPE_CHECKING:
    from .terminal import Terminal


class _Services:
    def __init__(self, terminal, auto_approve, declarations):
        self.terminal = terminal
        self.auto_approve = auto_approve
        self.declarations = declarations
        self.current_stage = None

    def ready(self, workflow, task):
        declaration = task.task_spec.declaration
        try:
            if declaration.stage is not self.current_stage:
                self.current_stage = declaration.stage
                self.terminal.stage(
                    declaration.stage,
                    tuple(item for item in self.declarations if item.stage is declaration.stage),
                )
        except BaseException as error:
            task.internal_data.setdefault("xxautoenv", {})["error"] = error
            self.failed(declaration, error)
            raise

    def before(self, declaration, action):
        self.terminal.before(declaration, action)
        return (
            not action.changes or self.auto_approve
            or self.terminal.confirm(f"Approve {action.description}?")
        )

    def finished(self, declaration, result):
        self.terminal.finished(declaration, result)

    def failed(self, declaration, error):
        self.terminal.failed(declaration, error)

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
        node.ready_event.connect(services.ready)
    try:
        workflow = Workflow(spec)
        status = RunStatus.SUCCESS
        try:
            workflow.run_all()
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
        report = _aggregate(workflow, declarations, status)
        terminal.report(report)
        return report
    finally:
        for node in nodes:
            node.ready_event.disconnect(services.ready)
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
                        help='Approve changing actions automatically; configuration still requires input')
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
