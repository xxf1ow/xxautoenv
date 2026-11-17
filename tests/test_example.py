import importlib.util
import io
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest

from xxautoenv.contracts import CheckVerdict, RunStatus
from xxautoenv.runner import run
from xxautoenv.terminal import Terminal


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'example' / 'local-file'
SCRIPT = EXAMPLE / 'deploy.py'


class ExampleTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'Standalone example deploy.py is missing')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name) / 'target'

    def execute(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments], cwd=self.directory.name,
            input='', text=True, capture_output=True, timeout=15,
        )

    def terminal(self, arguments, answers):
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), *arguments], cwd=self.directory.name,
            stdin=slave, stdout=slave, stderr=slave,
        )
        os.close(slave)
        output = b''
        pending = list(answers)
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                    if pending and pending[0][0].encode() in output:
                        _, answer = pending.pop(0)
                        os.write(master, (answer + '\n').encode())
                if process.poll() is not None and not select.select([master], [], [], 0)[0]:
                    break
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            self.assertFalse(pending, output.decode(errors='replace'))
            return code, output.decode()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            os.close(master)

    def assert_checks(self, output):
        self.assertIn('[success] Content check:', output)
        self.assertIn('[success] Permission check:', output)
        self.assertEqual(output.count('[passed]'), 2)
        self.assertIn('Deployment: success', output)

    def test_first_delivery_then_reuse_still_checks_both_requirements(self):
        first = self.execute('--target-dir', str(self.target), '-y')
        self.assertEqual(first.returncode, 0, first.stderr)
        installed = self.target / 'message.txt'
        self.assertEqual(installed.read_text(), 'Hello from xxautoenv.\n')
        self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
        self.assertIn('[success] Deliver file:', first.stdout)
        self.assert_checks(first.stdout)
        before = installed.stat().st_mtime_ns
        second = self.execute('--target-dir', str(self.target))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('[skipped] Reuse file:', second.stdout)
        self.assertIn('[not_selected] Deliver file', second.stdout)
        self.assertNotIn('[y/N]', second.stdout)
        self.assertEqual(installed.stat().st_mtime_ns, before)
        self.assert_checks(second.stdout)

    def test_content_or_permission_changes_require_delivery(self):
        self.target.mkdir()
        installed = self.target / 'message.txt'
        for content, mode in [('external change\n', 0o644), ('Hello from xxautoenv.\n', 0o600)]:
            with self.subTest(content=content, mode=mode):
                installed.write_text(content)
                installed.chmod(mode)
                result = self.execute('--target-dir', str(self.target), '-y')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('[success] Deliver file:', result.stdout)
                self.assertEqual(installed.read_text(), 'Hello from xxautoenv.\n')
                self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
                self.assert_checks(result.stdout)

    def test_terminal_requests_input_and_approval(self):
        code, output = self.terminal([], [('directory\r\nTarget directory: ', str(self.target)), ('[y/N]', 'yes')])
        self.assertEqual(code, 0, output)
        for stage in ('guidance', 'execution', 'acceptance'):
            self.assertIn('Stage: ' + stage, output)
        self.assertIn('[success] Target directory:', output)
        self.assertIn('[success] Deliver file:', output)
        self.assert_checks(output)

    def test_terminal_refusal_preserves_existing_file(self):
        self.target.mkdir()
        installed = self.target / 'message.txt'
        installed.write_text('Keep this\n')
        installed.chmod(0o600)
        code, output = self.terminal(['--target-dir', str(self.target)], [('[y/N]', 'n')])
        self.assertEqual(code, 2, output)
        self.assertEqual(installed.read_text(), 'Keep this\n')
        self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
        self.assertIn('[cancelled] Deliver file:', output)
        self.assertIn('Deployment: cancelled', output)
        self.assertIn('[not_executed] Content check', output)
        self.assertIn('[not_executed] Permission check', output)

    def test_auto_approval_does_not_supply_missing_input(self):
        result = self.execute('-y')
        self.assertEqual(result.returncode, 1)
        self.assertIn('interactive input is required', result.stderr)
        self.assertFalse(self.target.exists())

    def test_symlink_target_fails_without_touching_referent(self):
        self.target.mkdir()
        referent = Path(self.directory.name) / 'keep.txt'
        referent.write_text('Keep this\n')
        (self.target / 'message.txt').symlink_to(referent)
        result = self.execute('--target-dir', str(self.target), '-y')
        self.assertEqual(result.returncode, 1)
        self.assertIn('must be an ordinary file', result.stderr)
        self.assertEqual(referent.read_text(), 'Keep this\n')

    def test_failed_content_check_still_runs_permission_check(self):
        spec = importlib.util.spec_from_file_location('local_file_example', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        target = self.target

        class ChangedAfterDelivery(Terminal):
            def before(self, declaration, action):
                if declaration.id == 'content_check':
                    (target / 'message.txt').write_text('External change after delivery\n')
                super().before(declaration, action)

        report = run(module.build(type('Args', (), {'target_dir': str(target)})()),
                     auto_approve=True, terminal=ChangedAfterDelivery(stdout=output, stderr=output))
        self.assertEqual(report.status, RunStatus.FAILED)
        self.assertEqual([check.verdict for check in report.checks],
                         [CheckVerdict.NOT_PASSED, CheckVerdict.PASSED])
        self.assertIn('[success] Content check:', output.getvalue())
        self.assertIn('[not_passed]', output.getvalue())
        self.assertIn('[success] Permission check:', output.getvalue())
        self.assertIn('Deployment: failed', output.getvalue())


if __name__ == '__main__':
    unittest.main()
