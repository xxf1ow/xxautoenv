import unittest

from SpiffWorkflow.specs import ExclusiveChoice, WorkflowSpec

from xxautoenv.contracts import ContractError, Ref, Stage
from xxautoenv.node import DeploymentNode
from xxautoenv.nodes.function import function_node
from xxautoenv.workflow import describe_workflow, validate_workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.spec = WorkflowSpec(addstart=True)

    def node(self, id, *, stage=Stage.EXECUTION, inputs=None, output=None):
        return function_node(
            self.spec, id, name=id, purpose="Test workflow", stage=stage,
            inputs=inputs or {}, output=output or id,
            function=lambda **values: values, summary=str,
        )

    def check(self, *parents):
        node = self.node("check", stage=Stage.ACCEPTANCE)
        node._is_check = True
        for parent in parents:
            parent.connect(node)
        return node

    def test_unknown_reference_is_rejected(self):
        consumer = self.node("consumer", inputs={"value": Ref("unknown")})
        self.spec.start.connect(consumer)
        self.check(consumer)
        with self.assertRaises(ContractError):
            validate_workflow(self.spec)

    def test_reference_from_unselected_sibling_is_rejected(self):
        choice = ExclusiveChoice(self.spec, "choice")
        producer = self.node("producer", output="value")
        consumer = self.node("consumer", inputs={"value": Ref("value")})
        self.spec.start.connect(choice)
        choice.connect(producer)
        choice.connect_if(None, consumer)
        self.check(producer, consumer)
        with self.assertRaises(ContractError):
            validate_workflow(self.spec)

    def test_reference_missing_on_one_branch_is_rejected_after_merge(self):
        choice = ExclusiveChoice(self.spec, "choice")
        producer = self.node("producer", output="value")
        other = self.node("other")
        consumer = self.node("consumer", inputs={"value": Ref("value")})
        self.spec.start.connect(choice)
        choice.connect(producer)
        choice.connect_if(None, other)
        producer.connect(consumer)
        other.connect(consumer)
        self.check(consumer)
        with self.assertRaises(ContractError):
            validate_workflow(self.spec)

    def test_duplicate_node_identifiers_raise_contract_error(self):
        self.node("same")
        with self.assertRaises(ContractError):
            self.node("same")

    def test_stage_regression_is_rejected(self):
        later = self.node("later", stage=Stage.EXECUTION)
        earlier = self.node("earlier", stage=Stage.GUIDANCE)
        self.spec.start.connect(later)
        later.connect(earlier)
        self.check(earlier)
        with self.assertRaises(ContractError):
            validate_workflow(self.spec)

    def test_missing_final_check_is_rejected_even_with_acceptance_function(self):
        node = self.node("ordinary", stage=Stage.ACCEPTANCE)
        self.spec.start.connect(node)
        with self.assertRaises(ContractError):
            validate_workflow(self.spec)

    def test_every_branch_requires_final_check(self):
        choice = ExclusiveChoice(self.spec, "choice")
        left, right = self.node("left"), self.node("right")
        self.spec.start.connect(choice)
        choice.connect(left)
        choice.connect_if(None, right)
        self.check(left)
        with self.assertRaises(ContractError):
            validate_workflow(self.spec)

    def test_cycle_is_rejected(self):
        node = self.node("loop")
        self.spec.start.connect(node)
        node.connect(node)
        with self.assertRaises(ContractError):
            validate_workflow(self.spec)

    def test_same_output_on_sequential_nodes_is_rejected(self):
        first, second = self.node("first", output="value"), self.node("second", output="value")
        self.spec.start.connect(first)
        first.connect(second)
        self.check(second)
        with self.assertRaises(ContractError):
            validate_workflow(self.spec)

    def test_common_branch_output_is_valid_without_executing_or_rewriting_graph(self):
        choice = ExclusiveChoice(self.spec, "choice")
        left = self.node("left", output="value")
        right = self.node("right", output="value")
        consumer = self.node("consumer", inputs={"value": Ref("value")})
        self.spec.start.connect(choice)
        choice.connect(left)
        choice.connect_if(None, right)
        left.connect(consumer)
        right.connect(consumer)
        self.check(consumer)
        original = tuple((node, tuple(node.outputs)) for node in self.spec.task_specs.values())
        validate_workflow(self.spec)
        self.assertEqual(tuple((node, tuple(node.outputs)) for node in self.spec.task_specs.values()), original)
        self.assertEqual({item.id for item in describe_workflow(self.spec)},
                         {"left", "right", "consumer", "check"})
        self.assertTrue(all(node.services is None for node in self.spec.task_specs.values()
                            if isinstance(node, DeploymentNode)))


if __name__ == "__main__":
    unittest.main()
