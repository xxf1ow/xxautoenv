"""Read interactive answers and display deployment evidence on standard streams."""

import sys
import traceback
from typing import TextIO

from .contracts import (
    Action, CheckResult, NodeDeclaration, NodeResult, NodeStatus, RunReport, Stage,
    StepDisposition,
)


class Terminal:
    """Use injected text streams or the current process streams.

    Configuration and approval require a terminal stdin. EOF raises EOFError;
    noninteractive requests raise ValueError. Output and exception chains are
    written directly, without filtering.
    """

    def __init__(self, stdin: TextIO | None = None, stdout: TextIO | None = None,
                 stderr: TextIO | None = None):
        self.stdin = sys.stdin if stdin is None else stdin
        self._stdout = sys.stdout if stdout is None else stdout
        self._stderr = sys.stderr if stderr is None else stderr

    def begin(self, declarations: tuple[NodeDeclaration, ...]) -> None:
        """Display declared steps as pending until the native graph selects them."""
        self.stdout('Deployment steps:\n')
        for declaration in declarations:
            self.stdout(f'  [{declaration.stage.value}: pending] {declaration.name}: {declaration.purpose}\n')

    def stage(self, stage: Stage, declarations: tuple[NodeDeclaration, ...]) -> None:
        self.stdout(f'\nStage: {stage.value}\n')
        for declaration in declarations:
            self.stdout(f'  [pending] {declaration.name}: {declaration.purpose}\n')

    def before(self, declaration: NodeDeclaration, action: Action) -> None:
        """Display the prepared action; the runner decides whether to confirm it."""
        self.stdout(f'\n{declaration.name}: {declaration.purpose}\nAction: {action.description}\n')
        for scope in action.scope:
            self.stdout(f'  {scope.action}: {scope.target}\n')

    def finished(self, declaration: NodeDeclaration, result: NodeResult) -> None:
        self.stdout(f'[{result.status.value}] {declaration.name}: {result.summary}\n')
        if isinstance(result.value, CheckResult):
            self.stdout(f'  [{result.value.verdict.value}] {result.value.summary}\n')

    def failed(self, declaration: NodeDeclaration, error: BaseException) -> None:
        status = NodeStatus.INTERRUPTED if isinstance(error, KeyboardInterrupt) else NodeStatus.FAILED
        self.stdout(f'[{status.value}] {declaration.name}: {declaration.purpose}\n')
        traceback.print_exception(error, file=self._stderr)
        self._stderr.flush()

    def _read(self, message: str) -> str:
        if not self.stdin.isatty():
            raise ValueError(f'{message}: interactive input is required')
        self.stdout(message)
        answer = self.stdin.readline()
        if answer == '':
            raise EOFError(f'{message}: input closed before an answer was received')
        return answer.rstrip('\r\n')

    def confirm(self, message: str) -> bool:
        """Accept y/yes; an interactive empty or other answer declines the action."""
        return self._read(f'{message} [y/N] ').strip().lower() in ('y', 'yes')

    def prompt(self, message: str, default: str | None) -> str:
        """Read configuration; only an interactive empty answer selects the default."""
        suffix = f' [{default}]' if default is not None else ''
        answer = self._read(f'{message}{suffix}: ')
        return default if answer == '' and default is not None else answer

    def stdout(self, text: str) -> None:
        self._stdout.write(text)
        self._stdout.flush()

    def stderr(self, text: str) -> None:
        self._stderr.write(text)
        self._stderr.flush()

    def report(self, report: RunReport) -> None:
        """Display run status and distinguish every step without a business result."""
        self.stdout(f'\nDeployment: {report.status.value}\n')
        for step in report.steps:
            if step.disposition is not StepDisposition.EXECUTED:
                self.stdout(f'[{step.disposition.value}] {step.declaration.name}\n')
