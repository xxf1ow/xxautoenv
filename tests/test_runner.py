import io
import unittest

from SpiffWorkflow.operators import Attrib, Equal
from SpiffWorkflow.specs import ExclusiveChoice, Simple, WorkflowSpec

from xxautoenv.contracts import (
    Action, ActionScope, CheckResult, CheckVerdict, ContractError, NodeResult, NodeStatus,
    Ref, RunStatus, Stage, StepDisposition,
)
from xxautoenv.nodes.function import action_node, function_node
from xxautoenv.runner import run
from xxautoenv.terminal import Terminal


class RecordingTerminal:
    def __init__(self, approve=True):
        self.approve = approve
        self.stages = []
        self.actions = []
        self.confirmations = []
        self.reports = []

    def begin(self, declarations):
        self.declarations = declarations

    def stage(self, stage, declarations):
        self.stages.append(stage)

    def stage_finished(self, stage, steps):
        pass

    def before(self, declaration, action):
        self.actions.append((declaration.id, action))

    def confirm(self, message):
        if not isinstance(message, str):
            raise AssertionError("Confirmation must describe the action in text")
        self.confirmations.append(message)
        return self.approve

    def finished(self, declaration, result):
        pass

    def failed(self, declaration, error, *, preparation_result=None):
        pass

    def prompt(self, message, default):
        raise AssertionError("Unexpected input")

    def stdout(self, text):
        pass

    def stderr(self, text):
        pass

    def report(self, report):
        self.reports.append(report)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.spec = WorkflowSpec(addstart=True)
        self.terminal = RecordingTerminal()

    def node(self, id, function, *, inputs=None, output=None, stage=Stage.EXECUTION):
        return function_node(
            self.spec, id, name=id, purpose="Test execution", stage=stage,
            inputs=inputs or {}, output=output or id, function=function, summary=str,
        )

    def check(self, *parents, inputs=None, function=None):
        node = self.node("check", function or (lambda **values: CheckResult(CheckVerdict.PASSED, "Verified")),
                         stage=Stage.ACCEPTANCE, inputs=inputs)
        node._is_check = True
        for parent in parents:
            parent.connect(node)
        return node

    def test_rerun_observes_new_values_and_keeps_inputs_results_and_stages_independent(self):
        observed = [4]
        seen = []
        source = self.node("source", lambda: observed[0], stage=Stage.GUIDANCE)
        consumer = self.node("consumer", lambda value: seen.append(value) or value + 1,
                             inputs={"value": Ref("source")})
        self.spec.start.connect(source)
        source.connect(consumer)
        self.check(consumer, inputs={"value": Ref("consumer")})
        first = run(self.spec, terminal=self.terminal)
        observed[0] = 9
        second_terminal = RecordingTerminal()
        second = run(self.spec, terminal=second_terminal)
        self.assertNotEqual(first.steps, second.steps)
        self.assertEqual(seen, [4, 9])
        self.assertEqual([step.result.value for step in first.steps[:2]], [4, 5])
        self.assertEqual([step.result.value for step in second.steps[:2]], [9, 10])
        self.assertEqual(second_terminal.stages, [Stage.GUIDANCE, Stage.EXECUTION, Stage.ACCEPTANCE])
        self.assertEqual(first.status, RunStatus.SUCCESS)
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(second.checks, (CheckResult(CheckVerdict.PASSED, "Verified"),))

    def test_failure_stops_downstream_and_keeps_original_cause_and_traceback(self):
        cause = OSError("Cannot read material")
        failure = ValueError("Prepare failed")
        reached = []

        def fail():
            raise failure from cause

        node = self.node("broken", fail)
        self.spec.start.connect(node)
        self.check(node, function=lambda: reached.append(True))
        report = run(self.spec, terminal=self.terminal)
        self.assertEqual(report.status, RunStatus.FAILED)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(reached, [])
        diagnostic = report.steps[0].diagnostic
        self.assertIs(diagnostic.error, failure)
        self.assertIs(diagnostic.error.__cause__, cause)
        self.assertEqual(diagnostic.node_id, "broken")
        self.assertEqual(diagnostic.stage, Stage.EXECUTION)
        self.assertIn("Cannot read material", diagnostic.traceback)
        self.assertIn("Prepare failed", diagnostic.traceback)
        self.assertIsNone(report.steps[1].result)
        self.assertEqual(report.steps[0].disposition, StepDisposition.EXECUTED)
        self.assertEqual(report.steps[1].disposition, StepDisposition.NOT_EXECUTED)

    def test_exclusive_branches_publish_common_output_and_only_selected_path_runs(self):
        choice_value = ["left"]
        trace = []
        source = self.node("source", lambda: choice_value[0], stage=Stage.GUIDANCE)
        choice = ExclusiveChoice(self.spec, "choice")
        left = self.node("left", lambda: trace.append("left") or 11, output="common")
        right = self.node("right", lambda: trace.append("right") or 22, output="common")
        source.connect(choice)
        choice.connect_if(Equal(Attrib("source"), "left"), left)
        choice.connect(right)
        self.spec.start.connect(source)
        self.check(left, right, inputs={"value": Ref("common")},
                   function=lambda value: CheckResult(CheckVerdict.PASSED, str(value)))
        output = io.StringIO()
        terminal = Terminal(stdout=output, stderr=output)
        for _ in range(2):
            first = run(self.spec, terminal=terminal)
            first_output = output.getvalue()
            self.assertEqual(first_output.count("步骤（2） [未选择]"), 1)
            output.seek(0)
            output.truncate()
        choice_value[0] = "right"
        second = run(self.spec, terminal=terminal)
        for text, unselected in ((first_output, 2), (output.getvalue(), 1)):
            disposition = f"步骤（{unselected}） [未选择]"
            self.assertEqual(text.count(disposition), 1)
            self.assertLess(text.index(disposition), text.index("阶段：最终验收"))
            self.assertEqual(text.strip().splitlines()[-1], "全部成功完成")
        self.assertEqual(trace, ["left", "left", "right"])
        first_steps = {step.declaration.id: step for step in first.steps}
        self.assertEqual(first_steps["left"].disposition, StepDisposition.EXECUTED)
        self.assertEqual(first_steps["right"].disposition, StepDisposition.NOT_SELECTED)
        self.assertIsNone(first_steps["right"].result)
        self.assertEqual(first.checks[0].summary, "11")
        self.assertEqual(second.checks[0].summary, "22")
        self.assertEqual([step.declaration.id for step in first.steps if step.result], ["source", "left", "check"])
        self.assertEqual([step.declaration.id for step in second.steps if step.result], ["source", "right", "check"])

    def test_failure_before_choice_keeps_undetermined_branches_pending(self):
        def fail():
            raise ValueError("Observation failed")

        source = self.node("source", fail)
        choice = ExclusiveChoice(self.spec, "choice")
        left, right = self.node("left", lambda: 1), self.node("right", lambda: 2)
        self.spec.start.connect(source)
        source.connect(choice)
        choice.connect_if(Equal(Attrib("source"), "left"), left)
        choice.connect(right)
        self.check(left, right)
        output = io.StringIO()
        report = run(self.spec, terminal=Terminal(stdout=output, stderr=output))
        self.assertEqual(output.getvalue().strip().splitlines()[-1], "部署：失败")
        for line in ("执行 步骤（2） [路由待定]", "执行 步骤（3） [路由待定]",
                     "最终验收 步骤（1） [路由待定]"):
            self.assertEqual(output.getvalue().count(line), 1)
        steps = {step.declaration.id: step for step in report.steps}
        self.assertEqual(steps["source"].disposition, StepDisposition.EXECUTED)
        for id in ("left", "right", "check"):
            self.assertEqual(steps[id].disposition, StepDisposition.PENDING)
            self.assertIsNone(steps[id].result)

    def test_failure_on_selected_branch_distinguishes_unselected_and_not_executed(self):
        def fail():
            raise ValueError("Selected action failed")

        source = action_node(
            self.spec, "route", name="route", purpose="Resolve the route at execution",
            stage=Stage.EXECUTION, inputs={}, output="route", allows=lambda scope: False,
            prepare=lambda: Action("Resolve the route at execution", (),
                                   lambda: NodeResult(NodeStatus.SUCCESS, "selected", "Route selected")),
        )
        choice = ExclusiveChoice(self.spec, "choice")
        selected = self.node("selected", fail)
        unselected = self.node("unselected", lambda: self.fail("Unselected callback ran"))
        self.spec.start.connect(source)
        source.connect(choice)
        choice.connect_if(Equal(Attrib("route"), "unselected"), unselected)
        choice.connect(selected)
        self.check(selected, unselected)
        output = io.StringIO()
        report = run(self.spec, terminal=Terminal(stdout=output, stderr=output))
        rendered = output.getvalue()
        before_failure = rendered.split("阶段：执行", 1)[1].split("Traceback", 1)[0]
        self.assertIn("pending", before_failure)
        self.assertEqual([line for line in before_failure.splitlines() if "） [" in line], [
            "步骤（1） [完成]", "步骤（2） [未选择]", "步骤（3） [失败]",
        ])
        self.assertEqual(rendered.count("步骤（2） [未选择]"), 1)
        self.assertEqual(rendered.count("步骤（3） [失败]"), 1)
        self.assertEqual(rendered.count("最终验收 步骤（1） [未执行]"), 1)
        self.assertIn("Selected action failed", rendered)
        self.assertEqual(rendered.strip().splitlines()[-1], "部署：失败")
        steps = {step.declaration.id: step for step in report.steps}
        self.assertEqual(steps["selected"].disposition, StepDisposition.EXECUTED)
        self.assertEqual(steps["unselected"].disposition, StepDisposition.NOT_SELECTED)
        self.assertEqual(steps["check"].disposition, StepDisposition.NOT_EXECUTED)

    def test_skipped_step_is_executed_and_enters_stage_without_approval(self):
        node = action_node(
            self.spec, "reuse", name="Reuse", purpose="Reuse current material",
            stage=Stage.EXECUTION, inputs={}, output="material",
            prepare=lambda: NodeResult(NodeStatus.SKIPPED, "existing", "Already present"),
            allows=lambda scope: False,
        )
        self.spec.start.connect(node)
        self.check(node)
        report = run(self.spec, terminal=self.terminal)
        self.assertEqual(report.steps[0].disposition, StepDisposition.EXECUTED)
        self.assertEqual(report.steps[0].result.status, NodeStatus.SKIPPED)
        self.assertEqual(self.terminal.stages, [Stage.EXECUTION, Stage.ACCEPTANCE])
        self.assertEqual(self.terminal.confirmations, [])

    def changing(self, execute):
        node = action_node(
            self.spec, "change", name="Change", purpose="Write test material",
            stage=Stage.EXECUTION, inputs={}, output="changed",
            prepare=lambda: Action("Write target", (ActionScope("write", "target"),), execute, True),
            allows=lambda scope: True,
        )
        self.spec.start.connect(node)
        self.check(node)
        return node

    def test_refusal_reports_cancelled_and_never_executes_action(self):
        reached = []
        self.changing(lambda: reached.append(True))
        report = run(self.spec, terminal=RecordingTerminal(approve=False))
        self.assertEqual(report.status, RunStatus.CANCELLED)
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(reached, [])
        self.assertEqual(report.steps[0].result.status, NodeStatus.CANCELLED)
        self.assertIsNone(report.steps[0].diagnostic)

    def test_auto_approval_displays_action_without_requesting_confirmation(self):
        self.changing(lambda: NodeResult(NodeStatus.SUCCESS, "target", "Written"))
        report = run(self.spec, auto_approve=True, terminal=self.terminal)
        self.assertEqual(report.status, RunStatus.SUCCESS)
        self.assertEqual(self.terminal.confirmations, [])
        self.assertEqual([id for id, action in self.terminal.actions], ["change", "check"])

    def test_read_only_actions_do_not_request_confirmation(self):
        node = self.node("read", lambda: 1)
        self.spec.start.connect(node)
        self.check(node)
        run(self.spec, terminal=self.terminal)
        self.assertEqual(self.terminal.confirmations, [])

    def test_keyboard_interrupt_reports_interrupted_and_stops(self):
        interruption = KeyboardInterrupt()
        reached = []

        def interrupt():
            raise interruption

        node = self.node("interrupt", interrupt)
        self.spec.start.connect(node)
        self.check(node, function=lambda: reached.append(True))
        output = io.StringIO()
        report = run(self.spec, terminal=Terminal(stdout=output, stderr=output))
        self.assertEqual(output.getvalue().count("步骤（1） [中断]"), 1)
        self.assertEqual(output.getvalue().count("最终验收 步骤（1） [未执行]"), 1)
        self.assertEqual(output.getvalue().strip().splitlines()[-1], "部署：中断")
        self.assertEqual(report.status, RunStatus.INTERRUPTED)
        self.assertEqual(report.exit_code, 130)
        self.assertIs(report.steps[0].diagnostic.error, interruption)
        self.assertEqual(reached, [])

    def test_system_exit_retains_exit_semantics(self):
        def exit_now():
            raise SystemExit(17)

        node = self.node("exit", exit_now)
        self.spec.start.connect(node)
        self.check(node)
        with self.assertRaises(SystemExit) as caught:
            run(self.spec, terminal=self.terminal)
        self.assertEqual(caught.exception.code, 17)

    def test_manual_native_node_is_rejected_before_any_step_executes(self):
        reached = []
        source = self.node("source", lambda: reached.append("source"))
        manual = Simple(self.spec, "manual", manual=True)
        self.spec.start.connect(source)
        source.connect(manual)
        self.check(manual, function=lambda: reached.append("check"))
        with self.assertRaises(ContractError):
            run(self.spec, terminal=self.terminal)
        self.assertEqual(reached, [])

    def test_incomplete_native_workflow_cannot_report_success(self):
        gate = Simple(self.spec, "gate")
        check_calls = []

        def pause_gate():
            # A change after validation exercises the native completion guard.
            gate.manual = True
            return "observed"

        source = self.node("source", pause_gate)
        self.spec.start.connect(source)
        source.connect(gate)
        self.check(gate, function=lambda: check_calls.append(True))
        report = run(self.spec, terminal=self.terminal)
        self.assertEqual(report.status, RunStatus.FAILED)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(check_calls, [])
        self.assertEqual(report.checks, ())
        self.assertEqual(report.steps[-1].disposition, StepDisposition.NOT_EXECUTED)

    def test_not_passed_check_fails_run_after_all_checks_complete(self):
        first = self.node("first", lambda: CheckResult(CheckVerdict.NOT_PASSED, "Content mismatch"),
                          stage=Stage.ACCEPTANCE)
        first._is_check = True
        self.spec.start.connect(first)
        self.check(first)
        report = run(self.spec, terminal=self.terminal)
        self.assertEqual(report.status, RunStatus.FAILED)
        self.assertEqual([check.verdict for check in report.checks],
                         [CheckVerdict.NOT_PASSED, CheckVerdict.PASSED])


if __name__ == "__main__":
    unittest.main()
