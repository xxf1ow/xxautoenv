import io
import json
import os
from pathlib import Path
import pty
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from SpiffWorkflow.specs import WorkflowSpec

from xxautoenv.contracts import ActionScope, CheckResult, CheckVerdict, RunStatus, Stage
from xxautoenv.nodes.command import command_node
from xxautoenv.nodes.function import function_node
from xxautoenv.operations.command import CommandError, CommandRequest, execute_command
from xxautoenv.runner import run
from xxautoenv.terminal import Terminal


class CommandTests(unittest.TestCase):
    def request(self, script, *args, env=None, interactive=False):
        return CommandRequest((sys.executable, '-c', script, *args), Path.cwd(),
                              {} if env is None else env, interactive)

    def execute(self, request):
        stdout, stderr = [], []
        result = execute_command(request, stdout=stdout.append, stderr=stderr.append)
        return result, ''.join(stdout), ''.join(stderr)

    def test_preserves_both_output_streams_and_exit_seven(self):
        request = self.request("import os; os.write(1, b'out\\r\\nlast'); "
                               "os.write(2, b'err\\nend'); raise SystemExit(7)")
        stdout, stderr = [], []
        with self.assertRaises(CommandError) as caught:
            execute_command(request, stdout=stdout.append, stderr=stderr.append)
        error = caught.exception
        self.assertIs(error.request, request)
        self.assertEqual(error.result.returncode, 7)
        self.assertEqual(error.result.stdout, 'out\r\nlast')
        self.assertEqual(error.result.stderr, 'err\nend')
        self.assertEqual(''.join(stdout), 'out\r\nlast')
        self.assertEqual(''.join(stderr), 'err\nend')
        self.assertIn('7', str(error))

    def test_argv_cwd_and_environment_overlay_reach_real_process(self):
        with tempfile.TemporaryDirectory(prefix='command cwd ') as directory:
            request = CommandRequest(
                (sys.executable, '-c', 'import json, os, sys; '
                 'print(json.dumps([sys.argv[1:], os.getcwd(), os.environ["PATH"], '
                 'os.environ["XXAUTOENV_COMMAND_TEST"]]))', 'two words; $(echo wrong)'),
                Path(directory), {'XXAUTOENV_COMMAND_TEST': 'explicit value'},
            )
            result, stdout, stderr = self.execute(request)
            self.assertEqual(json.loads(result.stdout), [
                ['two words; $(echo wrong)'], str(Path(directory).resolve()),
                os.environ['PATH'], 'explicit value',
            ])
            self.assertEqual(result.returncode, 0)
            self.assertEqual(stdout, result.stdout)
            self.assertEqual(stderr, '')

    def workflow(self, request, terminal, *, changes=False):
        spec = WorkflowSpec(addstart=True)
        node = command_node(
            spec, 'command', name='Local command', purpose='Run test command',
            stage=Stage.EXECUTION, inputs={}, output='command_result',
            request=request, changes=changes,
            scope=(ActionScope('execute', 'test command'),) if changes else (),
        )
        check = function_node(
            spec, 'check', name='Check', purpose='Verify command', stage=Stage.ACCEPTANCE,
            inputs={}, output='check_result',
            function=lambda: CheckResult(CheckVerdict.PASSED, 'Verified'), summary=str,
        )
        check._is_check = True
        spec.start.connect(node)
        node.connect(check)
        return run(spec, terminal=terminal)

    def test_failure_retains_request_output_and_node_diagnostic(self):
        request = self.request("print('visible'); import sys; "
                               "print('reason', file=sys.stderr); sys.exit(7)")
        stdout, stderr = io.StringIO(), io.StringIO()
        report = self.workflow(lambda: request, Terminal(stdout=stdout, stderr=stderr))
        self.assertIs(report.status, RunStatus.FAILED)
        diagnostic = report.steps[0].diagnostic
        self.assertEqual(diagnostic.node_id, 'command')
        self.assertIsInstance(diagnostic.error, CommandError)
        self.assertIs(diagnostic.error.request, request)
        self.assertEqual(diagnostic.error.result.stdout, 'visible\n')
        self.assertEqual(diagnostic.error.result.stderr, 'reason\n')
        self.assertEqual(diagnostic.error.result.returncode, 7)
        self.assertIn('visible\n', stdout.getvalue())
        self.assertIn('reason\n', stderr.getvalue())
        self.assertIsNone(report.steps[1].result)

    def test_preparation_binds_one_request_and_copies_external_environment(self):
        env = {'XXAUTOENV_COMMAND_TEST': 'approved'}
        requests = []

        def request():
            bound = self.request('import os; print(os.environ["XXAUTOENV_COMMAND_TEST"])', env=env)
            requests.append(bound)
            return bound

        class ApprovingTerminal(Terminal):
            def confirm(self, message):
                env['XXAUTOENV_COMMAND_TEST'] = 'changed during approval'
                return True

        report = self.workflow(request, ApprovingTerminal(stdout=io.StringIO(), stderr=io.StringIO()),
                               changes=True)
        self.assertIs(report.status, RunStatus.SUCCESS)
        self.assertEqual(len(requests), 1)
        self.assertEqual(report.steps[0].result.value.stdout, 'approved\n')

    def read_until(self, descriptor, marker, *, timeout=10, line=False):
        output = bytearray()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while marker not in output or (line and b'\n' not in output.split(marker, 1)[1]):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    self.fail(f'Timed out waiting for {marker!r}; received {bytes(output)!r}')
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    self.fail(f'Closed before {marker!r}; received {bytes(output)!r}')
                output.extend(chunk)
        return bytes(output)

    @unittest.skipUnless(os.name == 'posix', 'POSIX terminal and signal behavior')
    def test_real_sigint_stops_managed_child_and_downstream_node(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_script = (
                'import os, pathlib, signal, sys; '
                'signal.signal(signal.SIGTERM, lambda *_: '
                '(pathlib.Path(sys.argv[1]).write_text("terminated"), sys.exit(0))); '
                'print("READY", os.getpid(), flush=True); signal.pause()'
            )
            probe = '''import json, pathlib, sys
from tests.test_command import CommandTests
from xxautoenv.terminal import Terminal
t = CommandTests()
report = t.workflow(lambda: t.request(sys.argv[2], sys.argv[1] + '/stopped'), Terminal())
pathlib.Path(sys.argv[1], 'report').write_text(json.dumps([
    report.status.value, report.exit_code, report.steps[1].result is None]))
'''
            process = subprocess.Popen(
                [sys.executable, '-c', probe, directory, child_script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
            child_pid = None
            try:
                ready = self.read_until(process.stdout.fileno(), b'READY ', line=True)
                pid_line = ready.split(b'READY ', 1)[1].splitlines()[0]
                child_pid = int(pid_line)
                os.kill(process.pid, signal.SIGINT)
                out, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, (ready + out, err))
                self.assertEqual(json.loads((root / 'report').read_text()), ['interrupted', 130, True])
                self.assertEqual((root / 'stopped').read_text(), 'terminated')
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()

    @unittest.skipUnless(os.name == 'posix', 'POSIX terminal inheritance')
    def test_interactive_command_inherits_terminal_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / 'result'
            script = '''import json, pathlib, sys
from tests.test_command import CommandTests
t = CommandTests()
result, stdout, stderr = t.execute(t.request(
    'import sys; assert all(s.isatty() for s in (sys.stdin, sys.stdout, sys.stderr)); '
    'print("INTERACTIVE_READY", flush=True); assert input() == "answer"', interactive=True))
pathlib.Path(sys.argv[1]).write_text(json.dumps([result.returncode, result.stdout, result.stderr, stdout, stderr]))
'''
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, '-c', script, str(result_path)],
                                       stdin=slave, stdout=slave, stderr=slave,
                                       start_new_session=True)
            os.close(slave)
            try:
                self.read_until(master, b'INTERACTIVE_READY')
                os.write(master, b'answer\n')
                process.wait(timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(json.loads(result_path.read_text()), [0, '', '', '', ''])
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                os.close(master)


if __name__ == '__main__':
    unittest.main()
