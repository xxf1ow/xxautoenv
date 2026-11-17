import io
import unittest
from unittest.mock import patch

from SpiffWorkflow import Workflow
from SpiffWorkflow.specs import WorkflowSpec
from SpiffWorkflow.task import TaskState

from xxautoenv.contracts import (
    Action, ActionScope, CheckResult, CheckVerdict, ContractError, NodeResult, NodeStatus, RunStatus, Stage,
)
from xxautoenv.runner import run
from xxautoenv.nodes.check import check_node
from xxautoenv.terminal import Terminal


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        self.terminal = Terminal(io.StringIO(), self.out, self.err)
        self.spec = WorkflowSpec(addstart=True)
        self.reached = []

    def node(self, id, check, *, stage=Stage.ACCEPTANCE):
        return check_node(self.spec, id, name=id, purpose=f'Check {id}', stage=stage,
                            inputs={}, output=id, check=check)

    def pair(self, first):
        second = self.node('second', lambda: self.reached.append('second') or CheckResult(CheckVerdict.PASSED, 'Second passed'))
        self.spec.start.connect(first)
        first.connect(second)

    def test_passed_and_not_passed_checks_complete_native_nodes_and_continue(self):
        for verdict, status in ((CheckVerdict.PASSED, RunStatus.SUCCESS), (CheckVerdict.NOT_PASSED, RunStatus.FAILED)):
            with self.subTest(verdict=verdict):
                self.spec = WorkflowSpec(addstart=True)
                first = self.node('first', lambda: CheckResult(verdict, 'Content observed'))
                self.pair(first)
                captured = []

                def workflow(spec):
                    native = Workflow(spec)
                    captured.append(native)
                    return native

                with patch('xxautoenv.runner.Workflow', side_effect=workflow):
                    report = run(self.spec, terminal=self.terminal)
                self.assertEqual(report.status, status)
                self.assertEqual([item.verdict for item in report.checks], [verdict, CheckVerdict.PASSED])
                task = next(task for task in captured[0].get_tasks() if task.task_spec is first)
                self.assertEqual(task.state, TaskState.COMPLETED)
                self.assertEqual(report.steps[0].result.status, NodeStatus.SUCCESS)
                self.assertIn(f'[{verdict.value}]', self.out.getvalue())
                self.assertIn(f'Deployment: {status.value}', self.out.getvalue())

    def test_ordinary_prepare_and_execute_errors_are_indeterminate_and_continue(self):
        for during_prepare in (True, False):
            with self.subTest(during_prepare=during_prepare):
                self.spec = WorkflowSpec(addstart=True)
                self.reached = []
                cause = OSError('file unavailable')
                failure = ValueError('cannot determine content')

                def fail():
                    raise failure from cause

                first = self.node('first', fail)
                if during_prepare:
                    first.prepare = fail
                self.pair(first)
                captured = []

                def workflow(spec):
                    native = Workflow(spec)
                    captured.append(native)
                    return native

                with patch('xxautoenv.runner.Workflow', side_effect=workflow):
                    report = run(self.spec, terminal=self.terminal)
                self.assertEqual(report.status, RunStatus.FAILED)
                self.assertEqual(self.reached, ['second'])
                self.assertEqual([check.verdict for check in report.checks], [CheckVerdict.INDETERMINATE, CheckVerdict.PASSED])
                self.assertIs(report.steps[0].diagnostic.error, failure)
                self.assertIs(report.steps[0].diagnostic.error.__cause__, cause)
                self.assertIn('file unavailable', self.err.getvalue())
                self.assertIn('cannot determine content', self.err.getvalue())
                self.assertIn('[indeterminate]', self.out.getvalue())
                self.assertIn('Deployment: failed', self.out.getvalue())
                task = next(task for task in captured[0].get_tasks() if task.task_spec is first)
                self.assertEqual(task.state, TaskState.COMPLETED)
                self.assertEqual(report.steps[0].result.status, NodeStatus.SUCCESS)

    def test_contract_error_and_interrupt_stop_remaining_checks(self):
        for during_prepare in (False, True):
            for error, wanted in ((ContractError('broken contract'), RunStatus.FAILED), (KeyboardInterrupt(), RunStatus.INTERRUPTED)):
                with self.subTest(error=type(error).__name__, during_prepare=during_prepare):
                    self.spec = WorkflowSpec(addstart=True)
                    self.reached = []

                    def fail():
                        raise error

                    first = self.node('first', fail)
                    if during_prepare:
                        first.prepare = fail
                    self.pair(first)
                    report = run(self.spec, terminal=self.terminal)
                    self.assertEqual(report.status, wanted)
                    self.assertEqual(self.reached, [])
                    self.assertIs(report.steps[0].diagnostic.error, error)
                    self.assertEqual(report.checks, ())

    def test_invalid_check_return_is_a_contract_error(self):
        first = self.node('first', lambda: 'passed')
        self.pair(first)
        report = run(self.spec, terminal=self.terminal)
        self.assertIsInstance(report.steps[0].diagnostic.error, ContractError)
        self.assertEqual(self.reached, [])

    def test_check_factory_rejects_other_stages(self):
        with self.assertRaises(ContractError):
            self.node('first', lambda: CheckResult(CheckVerdict.PASSED, 'ok'), stage=Stage.EXECUTION)

    def test_check_cannot_execute_a_changing_action(self):
        first = self.node('first', lambda: CheckResult(CheckVerdict.PASSED, 'ok'))
        first.prepare = lambda: Action('Write file', (ActionScope('write', 'file'),),
                                      lambda: self.reached.append('write') or NodeResult(NodeStatus.SUCCESS, None, 'written'), True)
        self.pair(first)
        report = run(self.spec, auto_approve=True, terminal=self.terminal)
        self.assertEqual(report.status, RunStatus.FAILED)
        self.assertIsInstance(report.steps[0].diagnostic.error, ContractError)
        self.assertEqual(self.reached, [])
