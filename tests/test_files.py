import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from xxautoenv.contracts import Action, ActionScope, NodeStatus
from xxautoenv.operations.files import FileArtifact, FileObservation, inspect_file, prepare_file


class FileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'source'
        self.source.write_bytes(b'approved\x00content\n')
        self.destination = self.root / 'destination'
        self.digest = hashlib.sha256(b'approved\x00content\n').hexdigest()

    def assert_delivered(self, result):
        self.assertEqual(result.status, NodeStatus.SUCCESS)
        self.assertEqual(result.value, FileArtifact(self.destination, self.digest, 0o644))
        self.assertEqual(self.destination.read_bytes(), b'approved\x00content\n')
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o644)
        self.assertEqual(inspect_file(self.destination),
                         FileObservation(self.destination, True, self.digest, 0o644))

    def test_missing_observation_does_not_create_file(self):
        self.assertEqual(inspect_file(self.destination),
                         FileObservation(self.destination, False, None, None))
        self.assertFalse(self.destination.exists())

    def test_first_delivery_creates_only_explicit_parents_after_execution(self):
        self.destination = self.root / 'one' / 'two' / 'destination'
        action = prepare_file(self.source, self.destination, 0o644)
        self.assertIsInstance(action, Action)
        self.assertTrue(action.changes)
        self.assertEqual(set(action.scope), {
            ActionScope('write', str(self.destination)),
            ActionScope('mkdir', str(self.root / 'one')),
            ActionScope('mkdir', str(self.root / 'one' / 'two')),
        })
        for parent in (self.root / 'one', self.root / 'one' / 'two'):
            self.assertIn(str(parent), action.description)
        self.assertIn(self.digest, action.description)
        self.assertFalse((self.root / 'one').exists())
        self.assert_delivered(action.execute())

    def test_content_update_replaces_complete_file(self):
        self.destination.write_bytes(b'previous content')
        self.destination.chmod(0o644)
        self.assert_delivered(prepare_file(self.source, self.destination, 0o644).execute())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['destination', 'source'])

    def test_permission_only_change_delivers_requested_mode(self):
        self.destination.write_bytes(self.source.read_bytes())
        self.destination.chmod(0o600)
        self.assert_delivered(prepare_file(self.source, self.destination, 0o644).execute())

    def test_matching_file_skips_and_preserves_mtime(self):
        self.destination.write_bytes(self.source.read_bytes())
        self.destination.chmod(0o644)
        os.utime(self.destination, ns=(1_000_000_000, 2_000_000_000))
        result = prepare_file(self.source, self.destination, 0o644)
        self.assertEqual(result.status, NodeStatus.SKIPPED)
        self.assertEqual(result.value, FileArtifact(self.destination, self.digest, 0o644))
        self.assertEqual(self.destination.stat().st_mtime_ns, 2_000_000_000)

    def test_source_mutation_after_prepare_preserves_existing_destination(self):
        self.destination.write_bytes(b'keep target')
        action = prepare_file(self.source, self.destination, 0o644)
        self.source.write_bytes(b'changed source')
        with self.assertRaisesRegex(RuntimeError, 'source|Source'):
            action.execute()
        self.assertEqual(self.destination.read_bytes(), b'keep target')

    def test_destination_mutation_after_prepare_preserves_external_content_and_mode(self):
        for mutation in ('content', 'mode'):
            with self.subTest(mutation=mutation):
                self.destination.write_bytes(b'original')
                self.destination.chmod(0o600)
                action = prepare_file(self.source, self.destination, 0o644)
                if mutation == 'content':
                    self.destination.write_bytes(b'external update')
                else:
                    self.destination.chmod(0o640)
                before = inspect_file(self.destination)
                with self.assertRaisesRegex(RuntimeError, 'destination|Destination'):
                    action.execute()
                self.assertEqual(inspect_file(self.destination), before)

    def test_nonregular_destination_is_preserved(self):
        for kind in ('directory', 'symlink', 'dangling', 'fifo'):
            with self.subTest(kind=kind):
                target = self.root / kind
                if kind == 'directory':
                    target.mkdir()
                elif kind == 'fifo':
                    os.mkfifo(target)
                else:
                    target.symlink_to(self.source if kind == 'symlink' else self.root / 'absent')
                before = target.lstat()
                with self.assertRaises((ValueError, OSError)):
                    prepare_file(self.source, target, 0o644)
                self.assertEqual(target.lstat(), before)
        self.assertEqual(self.source.read_bytes(), b'approved\x00content\n')

    def test_nonregular_source_and_missing_source_fail_without_writes(self):
        for source in (self.root, self.root / 'absent', self.root / 'link'):
            with self.subTest(source=source):
                if source.name == 'link':
                    source.symlink_to(self.source)
                with self.assertRaises((ValueError, OSError)):
                    prepare_file(source, self.destination, 0o644)
                self.assertFalse(self.destination.exists())

    def test_new_destination_after_prepare_is_not_overwritten(self):
        action = prepare_file(self.source, self.destination, 0o644)
        self.destination.write_bytes(b'external creation')
        with self.assertRaises(RuntimeError):
            action.execute()
        self.assertEqual(self.destination.read_bytes(), b'external creation')

    def test_parent_removal_does_not_expand_approved_creation_scope(self):
        parent = self.root / 'existing'
        parent.mkdir()
        target = parent / 'planned' / 'destination'
        action = prepare_file(self.source, target, 0o644)
        parent.rmdir()
        with self.assertRaises((RuntimeError, OSError)):
            action.execute()
        self.assertFalse(parent.exists())

    def test_external_parent_creation_invalidates_prepared_action(self):
        parent = self.root / 'planned'
        action = prepare_file(self.source, parent / 'destination', 0o644)
        parent.mkdir()
        with self.assertRaises(RuntimeError):
            action.execute()
        self.assertEqual(list(parent.iterdir()), [])

    def test_replaced_parent_does_not_redirect_write(self):
        parent = self.root / 'existing'
        parent.mkdir()
        action = prepare_file(self.source, parent / 'destination', 0o644)
        parent.rename(self.root / 'previous')
        parent.symlink_to(self.root / 'previous', target_is_directory=True)
        with self.assertRaises(RuntimeError):
            action.execute()
        self.assertEqual(list((self.root / 'previous').iterdir()), [])

    def test_failed_replace_cleans_local_temporary_file_and_preserves_target(self):
        self.destination.write_bytes(b'keep target')
        action = prepare_file(self.source, self.destination, 0o644)
        with patch('xxautoenv.operations.files.os.replace', side_effect=OSError('replace refused')):
            with self.assertRaisesRegex(OSError, 'replace refused'):
                action.execute()
        self.assertEqual(self.destination.read_bytes(), b'keep target')
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['destination', 'source'])


if __name__ == '__main__':
    unittest.main()
