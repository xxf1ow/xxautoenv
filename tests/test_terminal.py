import io
import sys
import unittest
from unittest.mock import patch

from SpiffWorkflow.specs import WorkflowSpec

from xxautoenv.contracts import (
    Action, ActionScope, CheckResult, CheckVerdict, NodeResult, NodeStatus, RunStatus, Stage,
)
from xxautoenv.nodes.function import action_node, function_node
from xxautoenv.runner import run
from xxautoenv.terminal import Terminal
import xxautoenv.runner as runner


class InteractiveInput(io.StringIO):
    def isatty(self):
        return True


class TerminalTests(unittest.TestCase):
    def terminal(self, answer='', *, interactive=True):
        self.out, self.err = io.StringIO(), io.StringIO()
        return Terminal(InteractiveInput(answer) if interactive else io.StringIO(answer), self.out, self.err)

    def workflow(self, prepare):
        spec = WorkflowSpec(addstart=True)
        node = action_node(
            spec, 'write', name='Install material', purpose='Deliver selected material',
            stage=Stage.EXECUTION, inputs={}, output='installed', prepare=prepare,
            allows=lambda scope: True,
        )
        check = function_node(
            spec, 'verify', name='Verify material', purpose='Read installed material',
            stage=Stage.ACCEPTANCE, inputs={}, output='verified',
            function=lambda: CheckResult(CheckVerdict.PASSED, 'Verified'), summary=lambda result: result.summary,
        )
        check._is_check = True
        spec.start.connect(node)
        node.connect(check)
        return spec

    def action(self, reached, *, changes=True):
        return Action('Write target.txt' if changes else 'Read target.txt',
                      (ActionScope('write', 'target.txt'),) if changes else (),
                      lambda: reached.append('target') or NodeResult(NodeStatus.SUCCESS, 'target', 'Delivered'), changes)

    def test_changing_action_displays_details_and_asks_once(self):
        reached = []
        terminal = self.terminal('y\n')
        report = run(self.workflow(lambda: self.action(reached)), terminal=terminal)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(reached, ['target'])
        output = self.out.getvalue()
        for text in ('执行', '最终验收', 'Deliver selected material',
                     'Write target.txt', 'target.txt', '完成', 'Delivered', 'Verified'):
            self.assertIn(text, output)
        self.assertEqual(output.count('[y/N]'), 1)
        self.assertEqual(self.err.getvalue(), '')

    def test_blank_confirmation_refuses_without_execution(self):
        reached = []
        report = run(self.workflow(lambda: self.action(reached)), terminal=self.terminal('\n'))
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(reached, [])
        self.assertIn('取消', self.out.getvalue())

    def test_read_only_and_skipped_steps_never_confirm(self):
        for skipped in (False, True):
            with self.subTest(skipped=skipped):
                reached = []
                prepared = (NodeResult(NodeStatus.SKIPPED, 'target', 'Already present')
                            if skipped else self.action(reached, changes=False))
                report = run(self.workflow(lambda: prepared), terminal=self.terminal())
                self.assertEqual(report.status, RunStatus.SUCCESS)
                self.assertNotIn('[y/N]', self.out.getvalue())
                if skipped:
                    self.assertIn('Already present', self.out.getvalue())
                    self.assertIn('步骤（1）', self.out.getvalue())
                    self.assertIn('跳过', self.out.getvalue())

    def test_auto_approval_displays_action_without_reading_confirmation(self):
        reached = []
        report = run(self.workflow(lambda: self.action(reached)), auto_approve=True,
                     terminal=self.terminal(interactive=False))
        self.assertEqual(report.exit_code, 0)
        self.assertIn('Write target.txt', self.out.getvalue())
        self.assertNotIn('[y/N]', self.out.getvalue())

    def test_confirming_cannot_replace_prepared_object(self):
        selected = ['first']
        reached = []
        terminal = self.terminal('y\n')
        read = terminal.stdin.readline

        def answer():
            selected[0] = 'second'
            return read()

        terminal.stdin.readline = answer

        def prepare():
            captured = selected[0]
            return Action(f'Write {captured}', (ActionScope('write', captured),),
                          lambda: reached.append(captured) or NodeResult(NodeStatus.SUCCESS, captured, 'Written'), True)

        report = run(self.workflow(prepare), terminal=terminal)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(reached, ['first'])
        self.assertIn('Write first', self.out.getvalue())

    def test_unavailable_confirmation_fails_and_never_executes(self):
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                reached = []
                report = run(self.workflow(lambda: self.action(reached)),
                             terminal=self.terminal(interactive=interactive))
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(reached, [])
                self.assertIn('失败', self.out.getvalue())

    def test_error_prints_original_cause_and_failed_step(self):
        def prepare():
            try:
                raise OSError('source unreadable')
            except OSError as error:
                raise ValueError('cannot prepare') from error

        report = run(self.workflow(prepare), terminal=self.terminal())
        self.assertEqual(report.exit_code, 1)
        for text in ('source unreadable', 'cannot prepare', 'Traceback'):
            self.assertIn(text, self.err.getvalue())
        self.assertIn('步骤（1）', self.out.getvalue())
        self.assertIn('失败', self.out.getvalue())

    def test_streams_preserve_raw_output(self):
        terminal = self.terminal()
        terminal.stdout('stdout without newline')
        terminal.stderr('stderr without newline')
        self.assertEqual(self.out.getvalue(), 'stdout without newline')
        self.assertEqual(self.err.getvalue(), 'stderr without newline')

    def test_main_configures_build_and_auto_approves(self):
        self.terminal()
        reached = []

        def configure(parser):
            parser.add_argument('--target', required=True)

        def build(args):
            reached.append(args.target)
            return self.workflow(lambda: self.action(reached))

        with patch.object(sys, 'stdin', io.StringIO()), patch.object(sys, 'stdout', self.out), patch.object(sys, 'stderr', self.err):
            code = runner.main(build, argv=['-y', '--target', '/tmp/material'], configure=configure)
        self.assertEqual(code, 0)
        self.assertEqual(reached, ['/tmp/material', 'target'])
        self.assertIn('Write target.txt', self.out.getvalue())

    def test_main_maps_failure_refusal_and_interrupt(self):
        for error, wanted in ((ValueError('broken'), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(error=type(error).__name__):
                self.terminal()

                def fail():
                    raise error

                with patch.object(sys, 'stdout', self.out), patch.object(sys, 'stderr', self.err):
                    self.assertEqual(runner.main(lambda args: self.workflow(fail), argv=[]), wanted)
        self.terminal()
        with patch.object(sys, 'stdin', InteractiveInput('\n')), patch.object(sys, 'stdout', self.out), patch.object(sys, 'stderr', self.err):
            self.assertEqual(runner.main(lambda args: self.workflow(lambda: self.action([])), argv=[]), 2)

    def test_main_build_errors_keep_cause_and_return_failure(self):
        self.terminal()

        def build(args):
            raise ValueError('invalid consumer configuration')

        with patch.object(sys, 'stderr', self.err):
            self.assertEqual(runner.main(build, argv=[]), 1)
        self.assertIn('invalid consumer configuration', self.err.getvalue())

    def test_plan_has_four_columns_literal_description_and_single_step_receipt(self):
        reached = []
        spec = self.workflow(lambda: self.action(reached))
        from dataclasses import replace
        node = spec.task_specs['write']
        node.declaration = replace(node.declaration, purpose='[bold]用户说明[/bold]')
        terminal = self.terminal('y\n')
        run(spec, terminal=terminal)
        output = self.out.getvalue()
        self.assertEqual(output.count('[bold]用户说明[/bold]'), 1)
        self.assertNotIn('Install material', output)
        self.assertEqual(output.count('Write target.txt'), 1)
        self.assertEqual(output.count('Delivered'), 1)
        self.assertEqual(output.count('Verified'), 1)
        header = next(line for line in output.splitlines() if '允许动作' in line)
        self.assertEqual([cell.strip() for cell in header.split('┃')][1:-1], ['#', '说明', '允许动作', '状态'])
        self.assertIn('就绪', output)
        self.assertIn('完成；通过', output)
