import unittest

from SpiffWorkflow import Workflow
from SpiffWorkflow.specs import WorkflowSpec

from xxautoenv.contracts import (
    Action,
    ActionScope,
    ContractError,
    NodeResult,
    NodeStatus,
    Ref,
    Stage,
)
from xxautoenv.nodes.function import action_node, function_node
from xxautoenv.node import _RunCancelled


class RecordingServices:
    def __init__(self, before=None):
        self.actions = []
        self.results = []
        self.errors = []
        self.on_before = before

    def before(self, declaration, action):
        self.actions.append(action)
        return True if self.on_before is None else self.on_before(action)

    def finished(self, declaration, result):
        self.results.append((declaration, result))

    def failed(self, declaration, error):
        self.errors.append((declaration, error))

    def prompt(self, message, default):
        raise AssertionError("Unexpected prompt")

    def stdout(self, text):
        pass

    def stderr(self, text):
        pass


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.spec = WorkflowSpec(addstart=True)
        self.services = RecordingServices()

    def function(self, id, function, *, inputs=None, output=None):
        node = function_node(
            self.spec, id, name=id, purpose="Compute test data", stage=Stage.GUIDANCE,
            inputs={} if inputs is None else inputs, output=output or id,
            function=function, summary=lambda value: f"Produced {value!r}",
        )
        node.bind(self.services)
        return node

    def action(self, prepare, *, allows=lambda scope: False):
        node = action_node(
            self.spec, "action", name="Action", purpose="Apply test action",
            stage=Stage.EXECUTION, inputs={}, output="result", prepare=prepare,
            allows=allows,
        )
        node.bind(self.services)
        return node

    def workflow(self, *nodes):
        previous = self.spec.start
        for node in nodes:
            previous.connect(node)
            previous = node
        return Workflow(self.spec)

    def test_graph_construction_does_not_execute_functions(self):
        trace = []
        node = self.function("producer", lambda: trace.append("producer"))
        workflow = self.workflow(node)
        self.assertEqual(trace, [])
        workflow.run_all()
        self.assertEqual(trace, ["producer"])

    def test_upstream_integer_is_passed_as_declared_keyword(self):
        trace = []
        received = []

        def produce():
            trace.append("producer")
            return 7

        def consume(value):
            trace.append("consumer")
            received.append(value)
            return value + 1

        producer = self.function("producer", produce, output="number")
        consumer = self.function("consumer", consume, inputs={"value": Ref("number")})
        self.workflow(producer, consumer).run_all()
        self.assertEqual(trace, ["producer", "consumer"])
        self.assertEqual(received, [7])
        self.assertEqual(self.services.results[-1][1].value, 8)

    def test_none_output_is_passed_without_becoming_missing(self):
        received = []
        producer = self.function("producer", lambda: None, output="optional")
        consumer = self.function("consumer", lambda value: received.append(value),
                                 inputs={"value": Ref("optional")})
        self.workflow(producer, consumer).run_all()
        self.assertEqual(received, [None])

    def test_constants_including_none_are_passed_directly(self):
        received = []
        node = self.function("consumer", lambda text, value: received.append((text, value)),
                             inputs={"text": "literal", "value": None})
        self.workflow(node).run_all()
        self.assertEqual(received, [("literal", None)])

    def test_missing_reference_raises_contract_error_before_function(self):
        received = []
        node = self.function("consumer", lambda value: received.append(value),
                             inputs={"value": Ref("missing")})
        with self.assertRaises(ContractError):
            self.workflow(node).run_all()
        self.assertEqual(received, [])
        self.assertIsInstance(self.services.errors[0][1], ContractError)

    def test_skipped_result_delivers_value_without_action(self):
        received = []
        skipped = NodeResult(NodeStatus.SKIPPED, "existing", "Already present")
        producer = self.action(lambda: skipped)
        consumer = self.function("consumer", lambda value: received.append(value),
                                 inputs={"value": Ref("result")})
        self.workflow(producer, consumer).run_all()
        self.assertEqual(received, ["existing"])
        self.assertIs(self.services.results[0][1], skipped)
        self.assertEqual(len(self.services.actions), 1)

    def test_prepare_rejects_non_action_and_direct_success(self):
        for prepared in (None, "invalid", NodeResult(NodeStatus.SUCCESS, 1, "Done")):
            with self.subTest(prepared=prepared):
                self.setUp()
                node = self.action(lambda: prepared)
                with self.assertRaises(ContractError):
                    self.workflow(node).run_all()
                self.assertEqual(self.services.actions, [])

    def test_scope_rules_reject_invalid_actions_before_approval(self):
        for changes, scope in (
            (True, ()),
            (True, (ActionScope("write", "/allowed"), ActionScope("write", "/other"))),
            (False, (ActionScope("read", "/allowed"),)),
        ):
            with self.subTest(changes=changes, scope=scope):
                self.setUp()
                executed = []
                action = Action("Write file", scope, lambda: executed.append(True), changes)
                node = self.action(lambda: action, allows=lambda item: item.target == "/allowed")
                with self.assertRaises(ContractError):
                    self.workflow(node).run_all()
                self.assertEqual(executed, [])
                self.assertEqual(self.services.actions, [])

    def test_approved_action_is_exact_object_executed(self):
        shown = []
        executed = []
        self.services = RecordingServices(lambda action: shown.append(action) or True)

        def execute():
            executed.append(action)
            return NodeResult(NodeStatus.SUCCESS, "/allowed", "Written")

        action = Action("Write file", (ActionScope("write", "/allowed"),), execute, True)
        node = self.action(lambda: action, allows=lambda scope: scope.target == "/allowed")
        self.workflow(node).run_all()
        self.assertIs(shown[0], executed[0])

    def test_execute_requires_node_result(self):
        node = self.action(lambda: Action("Read value", (), lambda: "invalid"))
        with self.assertRaises(ContractError):
            self.workflow(node).run_all()

    def test_execution_error_preserves_original_exception_and_stops_downstream(self):
        failure = ValueError("Cannot read source")
        reached = []

        def fail():
            raise failure

        producer = self.function("producer", fail)
        consumer = self.function("consumer", lambda: reached.append(True))
        with self.assertRaises(ValueError) as caught:
            self.workflow(producer, consumer).run_all()
        self.assertIs(caught.exception, failure)
        self.assertIs(self.services.errors[0][1], failure)
        self.assertEqual(reached, [])

    def test_reusing_spec_executes_function_for_each_native_workflow(self):
        calls = []
        node = self.function("producer", lambda: calls.append(True) or len(calls))
        self.workflow(node).run_all()
        Workflow(self.spec).run_all()
        self.assertEqual([result.value for _, result in self.services.results], [1, 2])

    def test_rejection_records_cancellation_without_executing_or_continuing(self):
        self.services = RecordingServices(lambda action: False)
        reached = []
        action = Action("Write file", (ActionScope("write", "/allowed"),),
                        lambda: reached.append("execute"), True)
        producer = self.action(lambda: action, allows=lambda scope: True)
        consumer = self.function("consumer", lambda: reached.append("downstream"))
        with self.assertRaises(_RunCancelled):
            self.workflow(producer, consumer).run_all()
        self.assertEqual(reached, [])
        self.assertEqual(self.services.results[0][1].status, NodeStatus.CANCELLED)
        self.assertEqual(self.services.errors, [])

    def test_keyboard_interrupt_preserves_identity_and_stops_downstream(self):
        interruption = KeyboardInterrupt()
        reached = []

        def interrupt():
            raise interruption

        producer = self.function("producer", interrupt)
        consumer = self.function("consumer", lambda: reached.append(True))
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.workflow(producer, consumer).run_all()
        self.assertIs(caught.exception, interruption)
        self.assertIs(self.services.errors[0][1], interruption)
        self.assertEqual(reached, [])

    def test_runtime_inputs_action_and_result_are_stored_on_native_task(self):
        node = self.function("producer", lambda value: value + 1, inputs={"value": 4})
        workflow = self.workflow(node)
        workflow.run_all()
        task = next(task for task in workflow.get_tasks() if task.task_spec is node)
        state = task.internal_data["xxautoenv"]
        self.assertEqual(state["inputs"], {"value": 4})
        self.assertIs(state["action"], self.services.actions[0])
        self.assertIs(state["result"], self.services.results[0][1])
        self.assertEqual(task.data["producer"], 5)


if __name__ == "__main__":
    unittest.main()
