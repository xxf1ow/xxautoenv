"""Read interactive answers and display deployment evidence on standard streams."""

import sys
import traceback
from typing import TextIO

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .contracts import (
    Action, CheckResult, NodeDeclaration, NodeResult, NodeStatus, RunReport, Stage,
    StepDisposition, StepReport, PreparedStep, PreparationStatus, CheckVerdict, RunStatus,
)


_STAGES = {Stage.GUIDANCE: "配置引导", Stage.EXECUTION: "执行", Stage.ACCEPTANCE: "最终验收"}
_NODE_STATUS = {NodeStatus.SUCCESS: "完成", NodeStatus.SKIPPED: "跳过", NodeStatus.FAILED: "失败",
                NodeStatus.CANCELLED: "取消", NodeStatus.INTERRUPTED: "中断"}
_RUN_STATUS = {RunStatus.SUCCESS: "成功", RunStatus.FAILED: "失败", RunStatus.CANCELLED: "取消", RunStatus.INTERRUPTED: "中断"}
_VERDICTS = {CheckVerdict.PASSED: "通过", CheckVerdict.NOT_PASSED: "不通过", CheckVerdict.INDETERMINATE: "无法判定"}
_DISPOSITIONS = {StepDisposition.NOT_SELECTED: "未选择", StepDisposition.NOT_EXECUTED: "未执行", StepDisposition.PENDING: "路由待定"}


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
        """Assign stable positions within each stage without repeating descriptions."""
        self._positions = {}
        self._published_dispositions = set()
        self._stage_shown = False
        for stage in Stage:
            for position, declaration in enumerate((item for item in declarations if item.stage is stage), 1):
                self._positions[declaration.id] = position

    def _step(self, declaration: NodeDeclaration) -> str:
        return f"步骤（{self._positions[declaration.id]}）"

    def stage(self, stage: Stage, plan: tuple[PreparedStep, ...]) -> None:
        """Render preparation facts; rows do not authorize or schedule execution."""
        table = Table(title=Text(f"阶段：{_STAGES[stage]}"), show_lines=False)
        table.add_column("#", justify="right")
        table.add_column("说明")
        table.add_column("Allowed actions")
        table.add_column("Status")
        for position, item in enumerate(plan, 1):
            allowed = ", ".join(dict.fromkeys(scope.action for scope in item.scope))
            if not allowed:
                allowed = ("pending" if item.status is PreparationStatus.PENDING
                           else "n/a" if item.status is PreparationStatus.NOT_SELECTED else "read-only")
            table.add_row(str(position), Text(item.declaration.purpose), Text(allowed),
                          Text(item.status.value))
        console = Console(file=self._stdout)
        if self._stage_shown:
            console.rule()
        console.print(table)
        self._stage_shown = True

    def before(self, declaration: NodeDeclaration, action: Action) -> None:
        """Display concrete action details once before the runner requests approval."""
        details = action.description != declaration.purpose
        if details or action.scope:
            self.stdout(f"{self._step(declaration)} 动作：" + (action.description if details else "") + "\n")
        for scope in action.scope:
            self.stdout(f"  {scope.action}: {scope.target}\n")

    def finished(self, declaration: NodeDeclaration, result: NodeResult) -> None:
        status = _NODE_STATUS[result.status]
        summary = result.summary
        if isinstance(result.value, CheckResult):
            status += "；" + _VERDICTS[result.value.verdict]
            if result.value.summary != summary:
                summary += "\n  " + result.value.summary
        self.stdout(f"{self._step(declaration)} [{status}] {summary}\n")

    def failed(self, declaration: NodeDeclaration, error: BaseException, *,
               preparation_result: NodeResult | None = None) -> None:
        """Show one failure receipt; a preparation verdict does not imply execution."""
        status = "中断" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "失败"
        if preparation_result is not None:
            self.stdout(f"{self._step(declaration)} [准备异常；无法判定] {preparation_result.summary}\n")
        else:
            self.stdout(f"{self._step(declaration)} [{status}]\n")
        traceback.print_exception(error, file=self._stderr)
        self._stderr.flush()

    def _read(self, message: str) -> str:
        if not self.stdin.isatty():
            raise ValueError(f'{message}：需要交互式终端输入')
        self.stdout(message)
        answer = self.stdin.readline()
        if answer == '':
            raise EOFError(f'{message}：尚未收到回答，输入已关闭')
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

    def stage_finished(self, stage: Stage, steps: tuple[StepReport, ...]) -> None:
        """Publish remaining dispositions before the next native stage begins."""
        self._dispositions(tuple(step for step in steps if step.declaration.stage is stage))

    def _dispositions(self, steps: tuple[StepReport, ...]) -> None:
        for step in steps:
            if (step.disposition is not StepDisposition.EXECUTED
                    and step.declaration.id not in self._published_dispositions):
                self.stdout(f'{_STAGES[step.declaration.stage]} {self._step(step.declaration)} [{_DISPOSITIONS[step.disposition]}]\n')
                self._published_dispositions.add(step.declaration.id)

    def report(self, report: RunReport) -> None:
        """Publish outstanding dispositions, then the final deployment conclusion."""
        self._dispositions(report.steps)
        self.stdout(f'\n部署：{_RUN_STATUS[report.status]}\n')
