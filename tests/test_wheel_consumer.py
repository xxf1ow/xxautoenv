import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
WHEEL = ROOT / 'dist' / 'xxautoenv-0.1.0-py3-none-any.whl'


class WheelConsumerTests(unittest.TestCase):
    def test_standalone_consumer_installs_wheel_and_runs_without_network(self):
        self.assertTrue(WHEEL.is_file(), 'Build the local product wheel first: uv build --wheel')
        with zipfile.ZipFile(WHEEL) as archive:
            members = archive.namelist()
        self.assertTrue(any(name.startswith('xxautoenv/') for name in members))
        self.assertTrue(any(name.endswith('/licenses/LICENSE') for name in members))
        self.assertTrue(all(name.startswith(('xxautoenv/', 'xxautoenv-0.1.0.dist-info/'))
                            for name in members), members)
        with tempfile.TemporaryDirectory(prefix='xxautoenv-consumer-') as directory:
            root = Path(directory).resolve()
            self.assertFalse(root.is_relative_to(ROOT))
            consumer = root / 'consumer'
            shutil.copytree(ROOT / 'example' / 'local-file', consumer,
                            ignore=shutil.ignore_patterns('.venv', '__pycache__'))
            dependency = consumer / 'vendor'
            dependency.mkdir()
            shutil.copy2(WHEEL, dependency / WHEEL.name)
            project = consumer / 'pyproject.toml'
            text = project.read_text()
            metadata = tomllib.loads(text)
            old_path = metadata['tool']['uv']['sources']['xxautoenv']['path']
            project.write_text(text.replace(old_path, 'vendor/' + WHEEL.name))
            env = os.environ.copy()
            env.pop('PYTHONPATH', None)
            env.pop('VIRTUAL_ENV', None)
            env['UV_PYTHON_DOWNLOADS'] = 'never'
            env['UV_OFFLINE'] = '1'
            env['UV_NO_BUILD'] = '1'
            env['UV_PROJECT_ENVIRONMENT'] = str(root / 'fresh-environment')

            def command(*argv):
                result = subprocess.run(argv, cwd=consumer, env=env, text=True,
                                        capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            command('uv', 'lock', '--offline', '--python', sys.executable)
            self.assertFalse((root / 'fresh-environment').exists())
            command('uv', 'sync', '--locked', '--offline', '--python', sys.executable)
            python = root / 'fresh-environment' / 'bin' / 'python'
            imported = command(str(python), '-I', '-c',
                               'import xxautoenv; print(xxautoenv.__file__)')
            location = Path(imported.stdout.strip()).resolve()
            self.assertTrue(location.is_relative_to(root / 'fresh-environment'))
            self.assertIn('site-packages', location.parts)
            self.assertFalse(location.is_relative_to(ROOT))
            target = root / 'target'
            first = command(str(python), 'deploy.py', '--target-dir', str(target), '-y')
            self.assertIn('[success] Deliver file:', first.stdout)
            self.assertEqual((target / 'message.txt').read_text(), 'Hello from xxautoenv.\n')
            self.assertEqual((target / 'message.txt').stat().st_mode & 0o777, 0o644)
            second = command(str(python), 'deploy.py', '--target-dir', str(target))
            self.assertIn('[skipped] Reuse file:', second.stdout)
            self.assertEqual(second.stdout.count('[passed]'), 2)
            blocked = command(str(python), '-c', '''
import runpy
import socket
import sys

def blocked(*args, **kwargs):
    raise AssertionError('Runtime attempted network access')

socket.socket = blocked
socket.create_connection = blocked
socket.getaddrinfo = blocked
sys.argv = ['deploy.py', '--target-dir', sys.argv[1], '-y']
runpy.run_path('deploy.py', run_name='__main__')
''', str(root / 'socket-blocked-target'))
            self.assertIn('Deployment: success', blocked.stdout)
            self.assertEqual(blocked.stdout.count('[passed]'), 2)


if __name__ == '__main__':
    unittest.main()
