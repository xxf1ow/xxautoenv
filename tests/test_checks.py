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
from xxautoenv.nodes.function import action_node
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
                self.assertIn('[完成；通过]' if verdict is CheckVerdict.PASSED else '[完成；不通过]', self.out.getvalue())
                self.assertIn('部署：成功' if status is RunStatus.SUCCESS else '部署：失败', self.out.getvalue())

    def test_ordinary_prepare_and_execute_errors_are_indeterminate_and_continue(self):
        for during_prepare in (True, False):
            with self.subTest(during_prepare=during_prepare):
                self.spec = WorkflowSpec(addstart=True)
                self.out.seek(0)
                self.out.truncate()
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
                expected = '[准备异常；无法判定]' if during_prepare else '[完成；无法判定]'
                self.assertEqual(self.out.getvalue().count('步骤（1） ' + expected), 1)
                self.assertIn('部署：失败', self.out.getvalue())
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

    def test_acceptance_change_is_rejected_during_preparation_before_checks_or_approval(self):
        class ApprovalInput(io.StringIO):
            def isatty(self):
                return True

        for auto_approve in (False, True):
            with self.subTest(auto_approve=auto_approve):
                self.spec = WorkflowSpec(addstart=True)
                target = {'acceptable': True}
                reached = []
                answers = ApprovalInput('y\n')
                terminal = Terminal(answers, self.out, self.err)

                def verify():
                    reached.append('verify')
                    verdict = CheckVerdict.PASSED if target['acceptable'] else CheckVerdict.NOT_PASSED
                    return CheckResult(verdict, 'Target observed')

                def change():
                    reached.append('change')
                    target['acceptable'] = False
                    return NodeResult(NodeStatus.SUCCESS, None, 'Target changed')

                first = self.node('verify', verify)
                changing = action_node(
                    self.spec, 'change', name='Change', purpose='Change verified target',
                    stage=Stage.ACCEPTANCE, inputs={}, output='changed',
                    prepare=lambda: Action('Write target', (ActionScope('write', 'target'),), change, True),
                    allows=lambda scope: True,
                )
                self.spec.start.connect(first)
                first.connect(changing)
                report = run(self.spec, auto_approve=auto_approve, terminal=terminal)

                self.assertEqual(report.status, RunStatus.FAILED,
                                 f'{report.status.value=}, {target=}, {reached=}')
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(report.checks, ())
                self.assertTrue(target['acceptable'])
                self.assertEqual(reached, [])
                self.assertEqual(answers.tell(), 0)
                self.assertIsNone(report.steps[1].result)
                diagnostic = report.steps[1].diagnostic
                self.assertIsInstance(diagnostic.error, ContractError)
                self.assertEqual(diagnostic.node_id, 'change')
                self.assertEqual(diagnostic.stage, Stage.ACCEPTANCE)
