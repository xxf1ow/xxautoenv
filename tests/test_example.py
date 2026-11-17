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
        self.assertEqual(output.strip().splitlines()[-1], '全部成功完成')
        self.assertIn('步骤（1） [完成；通过]', output)
        self.assertIn('步骤（2） [完成；通过]', output)
        self.assertEqual(output.count('[完成；通过]'), 2)
        self.assertIn('全部成功完成', output)
        stages = ('配置引导', '执行', '最终验收')
        for index, (stage, count) in enumerate(zip(stages, (2, 5, 2))):
            start = output.index('阶段：' + stage)
            stop = output.index('阶段：' + stages[index + 1]) if index < 2 else len(output)
            receipts = output[start:stop]
            for position in range(1, count + 1):
                self.assertEqual(receipts.count(f'步骤（{position}） ['), 1, receipts)

    def assert_stage_closure(self, output, disposition, receipt):
        self.assertEqual(output.count(disposition), 1)
        self.assertLess(output.index(receipt), output.index(disposition))
        self.assertLess(output.index(disposition), output.index('阶段：最终验收'))

    def assert_english_columns(self, output):
        headers = [line for line in output.splitlines() if 'Allowed actions' in line]
        self.assertEqual(len(headers), 3)
        for line in headers:
            self.assertEqual([cell.strip() for cell in line.split('┃')][1:-1],
                             ['#', '说明', 'Allowed actions', 'Status'])
        for line in output.splitlines():
            if line.startswith('│'):
                cells = [cell.strip() for cell in line.split('│')][1:-1]
                self.assertTrue(cells[-2].isascii(), line)
                self.assertTrue(cells[-1].isascii(), line)

    def test_first_delivery_then_reuse_still_checks_both_requirements(self):
        first = self.execute('--target-dir', str(self.target), '-y')
        self.assertEqual(first.returncode, 0, first.stderr)
        installed = self.target / 'message.txt'
        self.assertEqual(installed.read_text(), 'Hello from xxautoenv.\n')
        self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
        self.assertIn('步骤（4） [完成] 已交付文件', first.stdout)
        self.assert_stage_closure(first.stdout, '执行 步骤（5） [未选择]', '步骤（4） [完成]')
        self.assert_checks(first.stdout)
        self.assert_english_columns(first.stdout)
        before = installed.stat().st_mtime_ns
        second = self.execute('--target-dir', str(self.target))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('步骤（5） [跳过]', second.stdout)
        self.assertIn('执行 步骤（4） [未选择]', second.stdout)
        self.assertNotIn('[y/N]', second.stdout)
        self.assertEqual(installed.stat().st_mtime_ns, before)
        self.assert_checks(second.stdout)
        self.assert_stage_closure(second.stdout, '执行 步骤（4） [未选择]', '步骤（5） [跳过]')
        self.assert_english_columns(second.stdout)

    def test_content_or_permission_changes_require_delivery(self):
        self.target.mkdir()
        installed = self.target / 'message.txt'
        for content, mode in [('external change\n', 0o644), ('Hello from xxautoenv.\n', 0o600)]:
            with self.subTest(content=content, mode=mode):
                installed.write_text(content)
                installed.chmod(mode)
                result = self.execute('--target-dir', str(self.target), '-y')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('步骤（4） [完成] 已交付文件', result.stdout)
                self.assertEqual(installed.read_text(), 'Hello from xxautoenv.\n')
                self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
                self.assert_checks(result.stdout)

    def test_terminal_requests_input_and_approval(self):
        code, output = self.terminal([], [('目标目录: ', str(self.target)), ('[y/N]', 'yes')])
        self.assertEqual(code, 0, output)
        for stage in ('配置引导', '执行', '最终验收'):
            self.assertIn('阶段：' + stage, output)
        self.assertIn('步骤（1） [完成] 配置已接受', output)
        self.assertIn('步骤（4） [完成] 已交付文件', output)
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
        self.assertIn('步骤（4） [取消]', output)
        self.assertIn('部署：取消', output)
        self.assertIn('最终验收 步骤（1） [未执行]', output)
        self.assertIn('最终验收 步骤（2） [未执行]', output)
        self.assertEqual(output.strip().splitlines()[-1], '部署：取消')
        for disposition in ('执行 步骤（5） [未选择]', '最终验收 步骤（1） [未执行]',
                            '最终验收 步骤（2） [未执行]'):
            self.assertEqual(output.count(disposition), 1)
            self.assertLess(output.index(disposition), output.index('部署：取消'))

    def test_auto_approval_does_not_supply_missing_input(self):
        result = self.execute('-y')
        self.assertEqual(result.returncode, 1)
        self.assertIn('需要交互式终端输入', result.stderr)
        self.assertFalse(self.target.exists())

    def test_symlink_target_fails_without_touching_referent(self):
        self.target.mkdir()
        referent = Path(self.directory.name) / 'keep.txt'
        referent.write_text('Keep this\n')
        (self.target / 'message.txt').symlink_to(referent)
        result = self.execute('--target-dir', str(self.target), '-y')
        self.assertEqual(result.returncode, 1)
        self.assertIn('必须是普通文件', result.stderr)
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
        self.assertIn('步骤（1） [完成；不通过]', output.getvalue())
        self.assertIn('[完成；不通过]', output.getvalue())
        self.assertIn('步骤（2） [完成；通过]', output.getvalue())
        self.assertIn('部署：失败', output.getvalue())


if __name__ == '__main__':
    unittest.main()
