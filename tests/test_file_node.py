import io
from pathlib import Path
import tempfile
import unittest

from SpiffWorkflow.specs import WorkflowSpec

from xxautoenv.contracts import ActionScope, CheckResult, CheckVerdict, NodeStatus, Ref, RunStatus, Stage
from xxautoenv.nodes.check import check_node
from xxautoenv.nodes.file import file_node
from xxautoenv.operations.files import FileArtifact
from xxautoenv.runner import run
from xxautoenv.terminal import Terminal


class FileNodeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'source'
        self.source.write_bytes(b'approved')
        self.destination = self.root / 'new' / 'target'
        self.spec = WorkflowSpec(addstart=True)
        self.node = file_node(
            self.spec, 'deliver', name='Deliver', purpose='Install local file',
            stage=Stage.EXECUTION, inputs={'source': self.source, 'destination': self.destination},
            output='artifact',
        )
        self.received = []

        def verify(artifact):
            self.received.append(artifact)
            return CheckResult(CheckVerdict.PASSED, 'Artifact available')

        check = check_node(self.spec, 'verify', name='Verify', purpose='Read output',
                           stage=Stage.ACCEPTANCE, inputs={'artifact': Ref('artifact')},
                           output='check', check=verify)
        self.spec.start.connect(self.node)
        self.node.connect(check)

    def terminal(self, callback):
        class ControlledTerminal(Terminal):
            def confirm(self, message):
                return callback(message)

        return ControlledTerminal(stdout=io.StringIO(), stderr=io.StringIO())

    def test_approved_delivery_and_skip_both_publish_artifact_to_check(self):
        confirmations = []
        terminal = self.terminal(lambda message: confirmations.append(message) or True)
        first = run(self.spec, terminal=terminal)
        self.assertTrue(self.node.allows(ActionScope('write', str(self.destination))))
        self.assertFalse(self.node.allows(ActionScope('write', str(self.source))))
        self.assertFalse(self.node.allows(ActionScope('mkdir', str(self.root / 'unrelated'))))
        mtime = self.destination.stat().st_mtime_ns
        second = run(self.spec, terminal=terminal)
        self.assertEqual((first.status, second.status), (RunStatus.SUCCESS, RunStatus.SUCCESS))
        self.assertEqual(first.steps[0].result.status, NodeStatus.SUCCESS)
        self.assertEqual(second.steps[0].result.status, NodeStatus.SKIPPED)
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(len(self.received), 2)
        self.assertIsInstance(self.received[0], FileArtifact)
        self.assertEqual(self.received[0], self.received[1])
        self.assertEqual(self.destination.read_bytes(), b'approved')
        self.assertEqual(self.destination.stat().st_mtime_ns, mtime)
        self.assertFalse(self.node.allows(ActionScope('write', str(self.destination))))

    def test_refusal_creates_neither_directories_nor_file(self):
        report = run(self.spec, terminal=self.terminal(lambda message: False))
        self.assertEqual(report.status, RunStatus.CANCELLED)
        self.assertFalse(self.destination.parent.exists())
        self.assertEqual(self.received, [])

    def test_source_changed_during_approval_fails_without_creating_directories(self):
        def approve(message):
            self.source.write_bytes(b'changed while confirming')
            return True

        report = run(self.spec, terminal=self.terminal(approve))
        self.assertEqual(report.status, RunStatus.FAILED)
        self.assertIsInstance(report.steps[0].diagnostic.error, RuntimeError)
        self.assertFalse(self.destination.parent.exists())
        self.assertEqual(self.received, [])

    def test_target_changed_during_approval_is_preserved(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b'original')

        def approve(message):
            self.destination.write_bytes(b'external update')
            return True

        report = run(self.spec, terminal=self.terminal(approve))
        self.assertEqual(report.status, RunStatus.FAILED)
        self.assertEqual(self.destination.read_bytes(), b'external update')
        self.assertEqual(self.received, [])

    def test_changed_input_mapping_during_approval_does_not_rebind_target(self):
        other = self.root / 'unapproved'

        def approve(message):
            self.node.declaration.inputs['destination'] = other
            return True

        report = run(self.spec, terminal=self.terminal(approve))
        self.assertEqual(report.status, RunStatus.SUCCESS)
        self.assertEqual(self.destination.read_bytes(), b'approved')
        self.assertFalse(other.exists())
        self.assertFalse(self.node.allows(ActionScope('write', str(other))))


if __name__ == '__main__':
    unittest.main()
