import unittest
from unittest.mock import patch

from SpiffWorkflow import Workflow
from SpiffWorkflow.operators import Attrib, Equal
from SpiffWorkflow.specs import ExclusiveChoice, WorkflowSpec
from SpiffWorkflow.task import TaskState

from xxautoenv.contracts import Action, ActionScope, CheckResult, CheckVerdict, NodeResult, NodeStatus, Ref, RunStatus, Stage
from xxautoenv.nodes.check import check_node
from xxautoenv.nodes.function import action_node, function_node
from xxautoenv.runner import run
from tests.test_runner import RecordingTerminal


class PreparationTests(unittest.TestCase):
    def test_plan_has_observation_bound_actions_skip_and_pending_dependency(self):
        spec = WorkflowSpec(addstart=True)
        events, executed, actions, snapshots, natives, task_states = [], [], {}, [], [], []
        observe = function_node(spec, 'observe', name='Observe', purpose='Observe target',
            stage=Stage.EXECUTION, inputs={}, output='observed',
            function=lambda: events.append('observe') or None, summary=lambda value: 'Observed target')

        def operation(id, inputs, skipped=False):
            def prepare(**values):
                events.append('prepare:' + id)
                if skipped:
                    return NodeResult(NodeStatus.SKIPPED, 'existing', 'Already present')
                def execute():
                    executed.append(actions[id])
                    return NodeResult(NodeStatus.SUCCESS, id, id + ' done')
                actions[id] = Action(id, (ActionScope('write', id),), execute, True)
                return actions[id]
            return action_node(spec, id, name=id, purpose=id + ' purpose', stage=Stage.EXECUTION,
                inputs=inputs, output=id, prepare=prepare, allows=lambda scope: True)

        first = operation('first', {'value': Ref('observed')})
        second = operation('second', {'value': Ref('observed')})
        skipped = operation('skip', {}, True)
        late = operation('late', {'value': Ref('first')})
        check = check_node(spec, 'check', name='Check', purpose='Verify target', stage=Stage.ACCEPTANCE,
            inputs={}, output='check', check=lambda: CheckResult(CheckVerdict.PASSED, 'Verified'))
        previous = spec.start
        for node in (observe, first, second, skipped, late, check):
            previous.connect(node)
            previous = node

        class InspectingTerminal(RecordingTerminal):
            def stage(owner, stage, plan):
                if stage is Stage.EXECUTION:
                    snapshots.append((list(events), list(executed), plan))
                    if natives:
                        task_states.append({task.task_spec.name: task.state for task in natives[0].get_tasks()})
                super().stage(stage, plan)

        def native(spec):
            workflow = Workflow(spec)
            natives.append(workflow)
            return workflow

        terminal = InspectingTerminal()
        with patch('xxautoenv.runner.Workflow', side_effect=native):
            report = run(spec, terminal=terminal)
        self.assertEqual(report.status, RunStatus.SUCCESS)
        before, changes, plan = snapshots[0]
        self.assertEqual(before, ['observe', 'prepare:first', 'prepare:second', 'prepare:skip'])
        self.assertEqual(changes, [])
        self.assertEqual(task_states[0]['first'], TaskState.READY)
        self.assertEqual(task_states[0]['second'], TaskState.FUTURE)
        self.assertEqual([item.status.value for item in plan],
                         ['observed', 'ready', 'ready', 'no_change', 'pending'])
        self.assertEqual(events, before + ['prepare:late'])
        self.assertEqual(executed, [actions['first'], actions['second'], actions['late']])
        self.assertTrue(all(action is actions[id] for id, action in terminal.actions if id in actions))
        self.assertTrue(natives[0].is_completed())

    def test_native_choice_resolves_before_plan_without_preparing_unselected_branch(self):
        spec = WorkflowSpec(addstart=True)
        events, plans, prepared_at_plan = [], [], []
        source = function_node(spec, 'source', name='source', purpose='Observe route',
            stage=Stage.EXECUTION, inputs={}, output='route', function=lambda: 'left', summary=str)
        choice = ExclusiveChoice(spec, 'choice')
        def branch(id):
            def prepare():
                events.append(id)
                return NodeResult(NodeStatus.SKIPPED, id, 'Reused ' + id)
            return action_node(spec, id, name=id, purpose=id, stage=Stage.EXECUTION, inputs={},
                output='material', prepare=prepare, allows=lambda scope: False)
        left, right = branch('left'), branch('right')
        check = check_node(spec, 'check', name='check', purpose='Verify', stage=Stage.ACCEPTANCE,
            inputs={'value': Ref('material')}, output='check', check=lambda value: CheckResult(CheckVerdict.PASSED, value))
        spec.start.connect(source)
        source.connect(choice)
        choice.connect_if(Equal(Attrib('route'), 'left'), left)
        choice.connect(right)
        left.connect(check)
        right.connect(check)
        class InspectingTerminal(RecordingTerminal):
            def stage(owner, stage, plan):
                if stage is Stage.EXECUTION:
                    plans.append(plan)
                    prepared_at_plan.append(list(events))
        report = run(spec, terminal=InspectingTerminal())
        self.assertEqual(report.status, RunStatus.SUCCESS)
        self.assertEqual(events, ['left'])
        self.assertEqual(prepared_at_plan, [['left']])
        self.assertEqual({item.declaration.id: item.status.value for item in plans[0]},
                         {'source': 'observed', 'left': 'no_change', 'right': 'not_selected'})

    def test_preparation_stop_flushes_prior_probe_results_before_plan_or_approval(self):
        for error in (OSError('cannot observe'), KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(error=type(error).__name__):
                spec = WorkflowSpec(addstart=True)
                events = []
                source = function_node(spec, 'source', name='source', purpose='Observe',
                    stage=Stage.EXECUTION, inputs={}, output='source',
                    function=lambda: 1, summary=lambda value: 'Observed once')
                def fail():
                    raise error
                broken = action_node(spec, 'broken', name='broken', purpose='Prepare',
                    stage=Stage.EXECUTION, inputs={}, output='broken', prepare=fail,
                    allows=lambda scope: False)
                check = check_node(spec, 'check', name='check', purpose='Check', stage=Stage.ACCEPTANCE,
                    inputs={}, output='check', check=lambda: self.fail('Check ran after preparation failure'))
                spec.start.connect(source)
                source.connect(broken)
                broken.connect(check)
                class Recording(RecordingTerminal):
                    def finished(owner, declaration, result):
                        events.append(('result', declaration.id, result.summary))
                    def failed(owner, declaration, error):
                        events.append(('error', declaration.id, error))
                terminal = Recording()
                if isinstance(error, SystemExit):
                    with self.assertRaises(SystemExit) as caught:
                        run(spec, terminal=terminal)
                    self.assertIs(caught.exception, error)
                else:
                    report = run(spec, terminal=terminal)
                    self.assertIs(report.steps[1].diagnostic.error, error)
                self.assertEqual(terminal.stages, [])
                self.assertEqual(terminal.confirmations, [])
                self.assertEqual(events, [('result', 'source', 'Observed once'), ('error', 'broken', error)])

    def test_prepared_check_error_is_reported_when_later_preparation_stops(self):
        import io
        from xxautoenv.contracts import ContractError
        from xxautoenv.terminal import Terminal
        for stop in (ContractError('invalid later check'), KeyboardInterrupt()):
            with self.subTest(stop=type(stop).__name__):
                spec = WorkflowSpec(addstart=True)
                out, err, natives = io.StringIO(), io.StringIO(), []
                calls = []
                first = check_node(spec, 'first', name='first', purpose='First check', stage=Stage.ACCEPTANCE,
                    inputs={}, output='first', check=lambda: self.fail('Unprepared check executed'))
                second = check_node(spec, 'second', name='second', purpose='Second check', stage=Stage.ACCEPTANCE,
                    inputs={}, output='second', check=lambda: self.fail('Stopped check executed'))
                failure = OSError('first preparation unavailable')
                def fail_first():
                    calls.append('first')
                    raise failure
                def fail_second():
                    calls.append('second')
                    raise stop
                first.prepare, second.prepare = fail_first, fail_second
                spec.start.connect(first)
                first.connect(second)
                def native(spec):
                    workflow = Workflow(spec)
                    natives.append(workflow)
                    return workflow
                with patch('xxautoenv.runner.Workflow', side_effect=native):
                    report = run(spec, terminal=Terminal(stdout=out, stderr=err))
                self.assertEqual(calls, ['first', 'second'])
                self.assertEqual(out.getvalue().count('步骤（1） [准备异常；无法判定]'), 1)
                self.assertNotIn('完成', out.getvalue())
                self.assertNotIn('允许动作', out.getvalue())
                self.assertIn('first preparation unavailable', err.getvalue())
                self.assertEqual(report.checks, (CheckResult(CheckVerdict.INDETERMINATE, str(failure)),))
                self.assertIs(report.steps[0].diagnostic.error, failure)
                tasks = {task.task_spec.name: task for task in natives[0].get_tasks()}
                self.assertEqual(tasks['first'].state, TaskState.READY)
                self.assertEqual(tasks['second'].state, TaskState.FUTURE)
