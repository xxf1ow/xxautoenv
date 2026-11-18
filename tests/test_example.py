import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from rich.text import Text

from xxautoenv.contracts import CheckVerdict, RunStatus
from xxautoenv.runner import run
from xxautoenv.terminal import Terminal


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'example' / 'local-file'
SCRIPT = EXAMPLE / 'deploy.py'


def run_in_terminal(test, argv, answers, *, cwd, env=None):
    master, slave = pty.openpty()
    process = subprocess.Popen(
        argv, cwd=cwd, env=env,
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
        test.assertFalse(pending, output.decode(errors='replace'))
        return code, output.decode()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        os.close(master)

class ExampleTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'Standalone example deploy.py is missing')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.example = self.root / 'consumer'
        self.example.mkdir()
        self.script = self.example / SCRIPT.name
        self.config_dir = self.example / '.xxautoenv' / 'config'
        shutil.copy2(SCRIPT, self.script)
        shutil.copytree(EXAMPLE / 'assets', self.example / 'assets')
        self.target = self.example / 'target'

    def write_config(self, values):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        config = self.config_dir / 'test.json'
        config.write_text(json.dumps(values))
        return config

    def execute(self, *arguments):
        return subprocess.run(
            [sys.executable, str(self.script), *arguments], cwd=self.root,
            input='', text=True, capture_output=True, timeout=15,
        )

    def terminal(self, arguments, answers):
        return run_in_terminal(
            self, [sys.executable, str(self.script), *arguments], answers, cwd=self.root,
        )

    def assert_configuration_stages(self, output, source):
        transitions = []
        for line in output.splitlines():
            line = Text.from_ansi(line).plain.strip()
            if line.startswith('阶段'):
                transitions.append(line)
            elif set(line) == {'─'}:
                transitions.append('分割线')
        self.assertEqual(transitions, [
            '阶段（1）：配置', '分割线', '阶段（2）：引导', '分割线',
            '阶段（3）：执行', '分割线', '阶段（4）：最终验收',
        ])
        config = output.index('阶段（1）：配置')
        guidance = output.index('阶段（2）：引导', config)
        execution = output.index('阶段（3）：执行', guidance)
        config_lines = output[config:guidance].splitlines()
        guidance_lines = output[guidance:execution].splitlines()
        for lines, purposes, receipts in (
            (config_lines, ['确定配置来源'], [f'步骤（1） [完成] {source}']),
            (guidance_lines, ['校验并补齐目标目录', '读取来源材料'],
             ['步骤（1） [完成]', '步骤（2） [完成]']),
        ):
            self.assertEqual(sum('Allowed actions' in line for line in lines), 1, lines)
            rows = [line for line in lines if line.startswith('│')]
            self.assertEqual([[cell.strip() for cell in row.split('│')[1:3]] for row in rows],
                             [[str(i), purpose] for i, purpose in enumerate(purposes, 1)])
            self.assertEqual([line for line in lines if line.startswith('步骤（')], receipts)

    def assert_checks(self, output):
        self.assertEqual(output.strip().splitlines()[-1], '全部成功完成')
        self.assertIn('步骤（1） [完成；通过]', output)
        self.assertIn('步骤（2） [完成；通过]', output)
        self.assertEqual(output.count('[完成；通过]'), 2)
        self.assertIn('全部成功完成', output)
        stages = ('阶段（3）：执行', '阶段（4）：最终验收')
        for index, (stage, count) in enumerate(zip(stages, (5, 2))):
            start = output.index(stage)
            stop = output.index(stages[index + 1]) if index + 1 < len(stages) else len(output)
            receipts = output[start:stop]
            for position in range(1, count + 1):
                self.assertEqual(receipts.count(f'步骤（{position}） ['), 1, receipts)

    def execution_receipts(self, output):
        execution = output.split('阶段（3）：执行', 1)[1].split('阶段（4）：最终验收', 1)[0]
        return [line for line in execution.splitlines() if '步骤（' in line and '） [' in line]

    def assert_english_columns(self, output):
        self.assertNotIn('汇合', output)
        headers = [line for line in output.splitlines() if 'Allowed actions' in line]
        self.assertEqual(len(headers), 4)
        for line in headers:
            self.assertEqual([cell.strip() for cell in line.split('┃')][1:-1],
                             ['#', '说明', 'Allowed actions', 'Status'])
        for line in output.splitlines():
            if line.startswith('│'):
                cells = [cell.strip() for cell in line.split('│')][1:-1]
                self.assertTrue(cells[-2].isascii(), line)
                self.assertTrue(cells[-1].isascii(), line)

    def test_first_delivery_then_reuse_still_checks_both_requirements(self):
        config = self.write_config({'target_dir': 'target'})
        first = self.execute('--config', str(config.relative_to(self.example)), '-y')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assert_configuration_stages(first.stdout, str(config))
        self.assertNotIn('选择配置: ', first.stdout)
        self.assertNotIn('目标目录: ', first.stdout)
        installed = self.target / 'message.txt'
        self.assertEqual(installed.read_text(), 'Hello from xxautoenv.\n')
        self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
        with self.subTest(run='first delivery'):
            self.assertEqual(self.execution_receipts(first.stdout), [
                '步骤（1） [完成]', '步骤（2） [完成]', '步骤（3） [完成]',
                '步骤（4） [完成]', '步骤（5） [未选择]',
            ])
        self.assert_checks(first.stdout)
        self.assert_english_columns(first.stdout)
        before = installed.stat().st_mtime_ns
        second = self.execute('--config', str(config))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.execution_receipts(second.stdout), [
            '步骤（1） [完成]', '步骤（2） [完成]', '步骤（3） [未选择]',
            '步骤（4） [未选择]', '步骤（5） [跳过]',
        ])
        self.assertNotIn('[y/N]', second.stdout)
        self.assertEqual(installed.stat().st_mtime_ns, before)
        self.assert_checks(second.stdout)
        self.assert_english_columns(second.stdout)

    def test_content_or_permission_changes_require_delivery(self):
        config = self.write_config({'target_dir': 'target'})
        self.target.mkdir()
        installed = self.target / 'message.txt'
        for content, mode in [('external change\n', 0o644), ('Hello from xxautoenv.\n', 0o600)]:
            with self.subTest(content=content, mode=mode):
                installed.write_text(content)
                installed.chmod(mode)
                result = self.execute('--config', str(config), '-y')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('步骤（4） [完成]\n', result.stdout)
                self.assertEqual(installed.read_text(), 'Hello from xxautoenv.\n')
                self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
                self.assert_checks(result.stdout)

    def test_terminal_creates_and_selects_configurations(self):
        code, output = self.terminal([], [('目标目录: ', 'target'), ('[y/N]', 'yes')])
        self.assertEqual(code, 0, output)
        self.assert_configuration_stages(output.replace('\r\n', '\n'), '新建配置')
        self.assertNotIn('选择配置: ', output)
        self.assertEqual(output.count('[y/N]'), 1)
        self.assert_checks(output)
        first, = self.config_dir.glob('target-*.json')
        self.assertRegex(first.name, r'^target-\d{8}-\d{6}\.json$')
        self.assertEqual(json.loads(first.read_text()), {'target_dir': 'target'})

        code, output = self.terminal(
            ['-y'], [('选择配置: ', '2'), ('目标目录: ', 'second')],
        )
        self.assertEqual(code, 0, output)
        self.assert_configuration_stages(output.replace('\r\n', '\n'), '新建配置')
        self.assertEqual(len(list(self.config_dir.glob('*.json'))), 2)
        second = self.example / 'second' / 'message.txt'
        self.assertTrue(second.is_file())

        installed = self.target / 'message.txt'
        installed.unlink()
        code, output = self.terminal(['-y'], [('选择配置: ', '2')])
        self.assertEqual(code, 0, output)
        self.assert_configuration_stages(output.replace('\r\n', '\n'), str(first))
        self.assertNotIn('目标目录: ', output)
        self.assertTrue(installed.is_file())
        self.assert_checks(output)
        installed.unlink()
        replay = self.execute('--config', str(first.relative_to(self.example)), '-y')
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assert_configuration_stages(replay.stdout, str(first))
        self.assertNotIn('选择配置: ', replay.stdout)
        self.assertTrue(installed.is_file())
        self.assert_checks(replay.stdout)

    def test_missing_field_is_saved_without_asking_to_write_configuration(self):
        config = self.write_config({})
        code, output = self.terminal(
            ['--config', str(config)], [('目标目录: ', 'target'), ('[y/N]', 'n')],
        )
        self.assertEqual(code, 2, output)
        self.assertEqual(output.count('[y/N]'), 1)
        self.assertEqual(json.loads(config.read_text()), {'target_dir': 'target'})
        self.assertFalse(self.target.exists())

    def test_invalid_configuration_stops_before_deployment(self):
        for content in ('{"target_dir": ""}', '{"target_dir": null}', '{"target_dir": "../target"}',
                        '{"target_dir": "/tmp/target"}', '[]', '{'):
            with self.subTest(content=content):
                config = self.write_config({})
                config.write_text(content)
                code, output = self.terminal(['--config', str(config), '-y'], [])
                self.assertEqual(code, 1, output)
                self.assertIn(str(config), output)
                self.assertLess(output.index(f'步骤（1） [完成] {config}'), output.index('阶段（2）：引导'))
                self.assertIn('步骤（1） [失败]', output)
                self.assertNotIn('目标目录: ', output)
                self.assertNotIn('阶段（3）：执行', output)
                self.assertEqual(config.read_text(), content)
        self.assertFalse(self.target.exists())

    def test_missing_config_path_is_reported_in_guidance(self):
        config = self.config_dir / 'missing.json'
        result = self.execute('--config', str(config), '-y')
        self.assertEqual(result.returncode, 1)
        self.assertLess(result.stdout.index(f'步骤（1） [完成] {config}'),
                        result.stdout.index('阶段（2）：引导'))
        self.assertIn('步骤（1） [失败]', result.stdout)
        self.assertIn(str(config), result.stderr)
        self.assertNotIn('阶段（3）：执行', result.stdout)
        self.assertFalse(self.target.exists())

    def test_missing_source_material_fails_the_second_guidance_step(self):
        config = self.write_config({'target_dir': 'target'})
        source = self.example / 'assets' / 'message.txt'
        source.unlink()
        result = self.execute('--config', str(config), '-y')
        self.assertEqual(result.returncode, 1)
        guidance = result.stdout.split('阶段（2）：引导', 1)[1]
        self.assertIn('步骤（1） [完成]', guidance)
        self.assertIn('步骤（2） [失败]', guidance)
        self.assertIn(str(source), result.stderr)
        self.assertNotIn('阶段（3）：执行', result.stdout)
        self.assertFalse(self.target.exists())

    def test_invalid_configuration_selection_stops_before_guidance(self):
        self.write_config({'target_dir': 'target'})
        code, output = self.terminal(['-y'], [('选择配置: ', '3')])
        self.assertEqual(code, 1, output)
        self.assertIn('步骤（1） [失败]', output)
        self.assertNotIn('阶段（2）：引导', output)
        self.assertFalse(self.target.exists())

    def test_new_configuration_refuses_a_name_collision(self):
        self.config_dir.mkdir(parents=True)
        existing = self.config_dir / 'target-20261117-143205.json'
        existing.write_text('{"target_dir": "previous/target"}')
        original = self.script
        self.script = self.example / 'fixed_clock.py'
        self.script.write_text(f'''
import datetime
import runpy

class FixedClock(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 17, 14, 32, 5)

datetime.datetime = FixedClock
runpy.run_path({str(original)!r}, run_name='__main__')
''')
        code, output = self.terminal(
            ['-y'], [('选择配置: ', '2'), ('目标目录: ', 'target')],
        )
        self.assertEqual(code, 1, output)
        self.assertIn(str(existing), output)
        self.assertIn('已存在', output)
        self.assertEqual(existing.read_text(), '{"target_dir": "previous/target"}')
        self.assertEqual(list(self.config_dir.iterdir()), [existing])
        self.assertFalse(self.target.exists())

    def test_terminal_refusal_preserves_existing_file(self):
        config = self.write_config({'target_dir': 'target'})
        self.target.mkdir()
        installed = self.target / 'message.txt'
        installed.write_text('Keep this\n')
        installed.chmod(0o600)
        code, output = self.terminal(['--config', str(config)], [('[y/N]', 'n')])
        self.assertEqual(code, 2, output)
        self.assertEqual(installed.read_text(), 'Keep this\n')
        self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.count('步骤（4） [取消]'), 1)
        self.assertIn('部署：取消', output)
        self.assertIn('最终验收 步骤（1） [未执行]', output)
        self.assertIn('最终验收 步骤（2） [未执行]', output)
        self.assertEqual(output.strip().splitlines()[-1], '部署：取消')
        for disposition in ('步骤（5） [未选择]', '最终验收 步骤（1） [未执行]',
                            '最终验收 步骤（2） [未执行]'):
            self.assertEqual(output.count(disposition), 1)
            self.assertLess(output.index(disposition), output.index('部署：取消'))

    def test_auto_approval_does_not_supply_missing_input(self):
        config = self.write_config({})
        result = self.execute('--config', str(config), '-y')
        self.assertEqual(result.returncode, 1)
        self.assertIn('需要交互式终端输入', result.stderr)
        self.assertIn('target_dir', result.stderr)
        self.assertEqual(config.read_text(), '{}')
        self.assertFalse(self.target.exists())

    def test_symlink_target_fails_without_touching_referent(self):
        config = self.write_config({'target_dir': 'target'})
        self.target.mkdir()
        referent = Path(self.directory.name) / 'keep.txt'
        referent.write_text('Keep this\n')
        (self.target / 'message.txt').symlink_to(referent)
        result = self.execute('--config', str(config), '-y')
        self.assertEqual(result.returncode, 1)
        self.assertIn('必须是普通文件', result.stderr)
        self.assertEqual(referent.read_text(), 'Keep this\n')

    def test_failed_content_check_still_runs_permission_check(self):
        spec = importlib.util.spec_from_file_location('local_file_example', self.script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        target = self.target

        class ChangedAfterDelivery(Terminal):
            def before(self, declaration, action):
                if declaration.id == 'content_check':
                    (target / 'message.txt').write_text('External change after delivery\n')
                super().before(declaration, action)

        report = run(module.build(module.requirements(target)),
                     auto_approve=True, terminal=ChangedAfterDelivery(stdout=output, stderr=output))
        self.assertEqual(report.status, RunStatus.FAILED)
        self.assertEqual([check.verdict for check in report.checks],
                         [CheckVerdict.NOT_PASSED, CheckVerdict.PASSED])
        self.assertIn(f'步骤（1） [完成；不通过] 内容摘要不匹配：{target / "message.txt"}', output.getvalue())
        self.assertEqual(output.getvalue().count('步骤（1） [完成；不通过]'), 1)
        self.assertIn('步骤（2） [完成；通过]', output.getvalue().splitlines())
        self.assertEqual(output.getvalue().strip().splitlines()[-1], '部署：失败')


if __name__ == '__main__':
    unittest.main()
