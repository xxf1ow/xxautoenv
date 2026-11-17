import io
import unittest

from SpiffWorkflow.specs import WorkflowSpec

from xxautoenv.contracts import CheckResult, CheckVerdict, RunStatus, Stage
from xxautoenv.nodes.function import function_node
from xxautoenv.nodes.input import input_node
from xxautoenv.terminal import Terminal
from xxautoenv.runner import run
from tests.test_terminal import InteractiveInput


class InputTests(unittest.TestCase):
    def execute(self, inputs, *, answer='', default=None, validate=str, interactive=True):
        self.out, self.err = io.StringIO(), io.StringIO()
        terminal = Terminal(InteractiveInput(answer) if interactive else io.StringIO(answer), self.out, self.err)
        spec = WorkflowSpec(addstart=True)
        node = input_node(spec, 'target', name='Target directory', purpose='Choose destination',
                          stage=Stage.GUIDANCE, inputs=inputs, output='target',
                          prompt='Destination', default=default, validate=validate)
        check = function_node(spec, 'check', name='Check', purpose='Verify', stage=Stage.ACCEPTANCE,
                              inputs={}, output='checked', function=lambda: CheckResult(CheckVerdict.PASSED, 'Ready'), summary=str)
        check._is_check = True
        spec.start.connect(node)
        node.connect(check)
        return run(spec, auto_approve=True, terminal=terminal)

    def test_supplied_value_is_validated_without_prompt(self):
        report = self.execute({'value': '42'}, validate=int, interactive=False)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.steps[0].result.value, 42)
        self.assertNotIn('Destination', self.out.getvalue())

    def test_empty_string_is_a_supplied_value(self):
        report = self.execute({'value': ''}, validate=lambda text: ('normalized', text), interactive=False)
        self.assertEqual(report.steps[0].result.value, ('normalized', ''))

    def test_missing_and_explicit_none_request_configuration(self):
        for inputs in ({}, {'value': None}):
            with self.subTest(inputs=inputs):
                report = self.execute(inputs, answer='chosen\n')
                self.assertEqual(report.steps[0].result.value, 'chosen')
                self.assertIn('Destination', self.out.getvalue())
                self.assertNotIn('[y/N]', self.out.getvalue())

    def test_interactive_blank_uses_default(self):
        report = self.execute({}, answer='\n', default='fallback')
        self.assertEqual(report.steps[0].result.value, 'fallback')
        self.assertIn('fallback', self.out.getvalue())

    def test_validation_error_retries_and_keeps_normalized_none(self):
        def validate(value):
            if value != 'valid':
                raise ValueError('Use valid')
            return None

        report = self.execute({}, answer='bad\nvalid\n', validate=validate)
        self.assertEqual(report.status, RunStatus.SUCCESS)
        self.assertIsNone(report.steps[0].result.value)
        self.assertIn('Use valid', self.err.getvalue())
        self.assertEqual(self.out.getvalue().count('Destination'), 2)

    def test_invalid_parameter_can_be_corrected_interactively(self):
        report = self.execute({'value': 'bad'}, answer='7\n', validate=int)
        self.assertEqual(report.steps[0].result.value, 7)
        self.assertIn('invalid literal', self.err.getvalue())

    def test_eof_and_nonterminal_missing_input_fail_despite_auto_approval(self):
        for interactive in (True, False):
            with self.subTest(interactive=interactive):
                report = self.execute({}, interactive=interactive)
                self.assertEqual(report.exit_code, 1)
                self.assertIsNotNone(report.steps[0].diagnostic)
                self.assertIsNone(report.steps[1].result)

    def test_default_does_not_answer_noninteractive_missing_input(self):
        report = self.execute({}, default='fallback', interactive=False)
        self.assertEqual(report.exit_code, 1)
