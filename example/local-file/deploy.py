"""Install one local text file using a consumer-owned native workflow."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import tempfile

from SpiffWorkflow.operators import Attrib, Equal
from SpiffWorkflow.specs import ExclusiveChoice, WorkflowSpec

from xxautoenv.contracts import CheckResult, CheckVerdict, NodeResult, NodeStatus, Ref, Stage
from xxautoenv.nodes.check import check_node
from xxautoenv.nodes.file import file_node
from xxautoenv.nodes.function import action_node, function_node
from xxautoenv.operations.files import FileArtifact, FileObservation, inspect_file
from xxautoenv.runner import main
from xxautoenv.terminal import Terminal


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / '.xxautoenv' / 'config'
SOURCE = ROOT / 'assets' / 'message.txt'


def target_directory(value: object, source: Path | str) -> Path:
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'{source} 的 target_dir 必须是非空相对路径')
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f'{source} 的 target_dir 必须相对于部署脚本所在目录')
    resolved = (ROOT / relative).resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError(f'{source} 的 target_dir 必须位于部署脚本所在目录内')
    return resolved


def select_config(terminal: Terminal) -> Path | None:
    choices = sorted(CONFIG_DIR.glob('*.json'))
    if not choices:
        return None
    for index, path in enumerate(choices, start=1):
        terminal.stdout(f'   {index}. {path.name}\n')
    terminal.stdout(f'   {len(choices) + 1}. 新建配置\n')
    choice = terminal.prompt('选择配置', None)
    try:
        index = int(choice)
    except ValueError:
        index = 0
    if not 1 <= index <= len(choices) + 1:
        raise ValueError(f'请选择 1 到 {len(choices) + 1} 之间的配置序号')
    return choices[index - 1] if index <= len(choices) else None


def config_mode(path: Path) -> int:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError(f'配置文件 {path} 必须是普通文件')
    return stat.S_IMODE(mode)


def save_config(path: Path, data: dict, original: bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.config-')
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            if original is not None:
                os.fchmod(handle.fileno(), config_mode(path))
        if original is None:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise ValueError(f'配置文件 {path} 已存在') from None
        else:
            if path.read_bytes() != original:
                raise ValueError(f'配置文件 {path} 在补录期间发生变化')
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def configuration(path: Path | None, terminal: Terminal) -> Path:
    original = None
    data = {}
    if path is not None:
        config_mode(path)
        original = path.read_bytes()
        try:
            data = json.loads(original.decode('utf-8'))
        except ValueError as error:
            raise ValueError(f'配置文件 {path} 无法解析为 JSON：{error}') from None
        if not isinstance(data, dict):
            raise ValueError(f'配置文件 {path} 必须包含一个 JSON 对象')
        if 'target_dir' in data:
            return target_directory(data['target_dir'], path)
    source = path if path is not None else '新建配置'
    terminal.stdout(f'{source} 需要 target_dir。请输入部署目录下的相对路径，例如 targets/demo。\n')
    try:
        value = terminal.prompt('目标目录', None)
    except (EOFError, ValueError) as error:
        raise ValueError(f'{source} 缺少 target_dir。{error}') from None
    target = target_directory(value, source)
    data['target_dir'] = target.relative_to(ROOT).as_posix()
    if path is None:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        path = CONFIG_DIR / f'{target.name}-{stamp}.json'
    save_config(path, data, original)
    return target


def requirements(target_dir: Path) -> FileArtifact:
    material = inspect_file(SOURCE)
    if not material.exists:
        raise FileNotFoundError(f'来源文件 {SOURCE} 不存在')
    return FileArtifact(target_dir / 'message.txt', material.sha256, 0o644)


def choose(material: FileArtifact, observed: FileObservation) -> str:
    return 'reuse' if (observed.exists and observed.sha256 == material.sha256
                       and observed.mode == material.mode) else 'deliver'


def reuse(material: FileArtifact) -> NodeResult:
    return NodeResult(NodeStatus.SKIPPED, material, f'文件 {material.path} 已符合要求')


def content_check(artifact: FileArtifact) -> CheckResult:
    observed = inspect_file(artifact.path)
    passed = observed.exists and observed.sha256 == artifact.sha256
    return CheckResult(CheckVerdict.PASSED if passed else CheckVerdict.NOT_PASSED,
                       f'内容摘要{"匹配" if passed else "不匹配"}：{artifact.path}')


def permission_check(artifact: FileArtifact) -> CheckResult:
    observed = inspect_file(artifact.path)
    passed = observed.exists and observed.mode == artifact.mode
    return CheckResult(CheckVerdict.PASSED if passed else CheckVerdict.NOT_PASSED,
                       f'权限{"符合" if passed else "不符合"} 0644：{artifact.path}')


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--config', type=Path, help='配置文件路径，相对路径以部署脚本所在目录为基准')


def build(material: FileArtifact) -> WorkflowSpec:
    spec = WorkflowSpec(addstart=True)
    observed = function_node(
        spec, 'observe', name='观察目标', purpose='读取目标当前内容和权限',
        stage=Stage.EXECUTION, inputs={'material': material}, output='observed',
        function=lambda material: inspect_file(material.path),
        summary=lambda value: (f'目标存在，摘要 {value.sha256}，权限 {value.mode:04o}'
                               if value.exists else f'目标尚不存在：{value.path}'),
    )
    selection = function_node(
        spec, 'choose', name='选择动作', purpose='根据文件现状选择交付或复用',
        stage=Stage.EXECUTION, inputs={'material': material, 'observed': Ref('observed')},
        output='route', function=choose, summary=lambda value: '选择复用' if value == 'reuse' else '选择交付',
    )
    choice = ExclusiveChoice(spec, 'delivery_choice')
    deliver = file_node(
        spec, 'deliver', name='交付文件', purpose='安装 message.txt 并设置权限 0644',
        stage=Stage.EXECUTION, inputs={'source': SOURCE, 'destination': Ref('destination')},
        output='artifact', mode=0o644,
    )
    destination = function_node(
        spec, 'destination', name='交付位置', purpose='使用选定的安装位置',
        stage=Stage.EXECUTION, inputs={'material': material}, output='destination',
        function=lambda material: material.path, summary=str,
    )
    reused = action_node(
        spec, 'reuse', name='复用文件', purpose='保留符合要求的文件并提供产出',
        stage=Stage.EXECUTION, inputs={'material': material}, output='artifact',
        prepare=reuse, allows=lambda scope: False,
    )
    content = check_node(
        spec, 'content_check', name='内容核查', purpose='核对已安装文件的内容摘要',
        stage=Stage.ACCEPTANCE, inputs={'artifact': Ref('artifact')}, output='content_result',
        check=content_check,
    )
    permissions = check_node(
        spec, 'permission_check', name='权限核查', purpose='核对已安装文件的权限',
        stage=Stage.ACCEPTANCE, inputs={'artifact': Ref('artifact')}, output='permission_result',
        check=permission_check,
    )
    spec.start.connect(observed)
    observed.connect(selection)
    selection.connect(choice)
    choice.connect(destination)
    choice.connect_if(Equal(Attrib('route'), 'reuse'), reused)
    destination.connect(deliver)
    deliver.connect(content)
    reused.connect(content)
    content.connect(permissions)
    return spec


def deployment(args: argparse.Namespace, terminal: Terminal) -> WorkflowSpec:
    terminal.stage_table('配置', (('确定配置来源', 'read-only', 'ready'),))
    try:
        path = ROOT / args.config if args.config is not None else select_config(terminal)
    except (EOFError, OSError, ValueError) as error:
        terminal.step_result(1, NodeStatus.FAILED)
        raise SystemExit(f'确定配置来源失败：{error}') from None
    source = str(path) if path is not None else '新建配置'
    terminal.step_result(1, NodeStatus.SUCCESS, source)

    terminal.stage_table('引导', (
        ('校验并补齐目标目录', 'read, write', 'ready'),
        ('读取来源材料', 'read-only', 'pending'),
    ))
    try:
        target = configuration(path, terminal)
    except (EOFError, OSError, ValueError) as error:
        terminal.step_result(1, NodeStatus.FAILED)
        raise SystemExit(f'校验并补齐目标目录失败：{error}') from None
    terminal.step_result(1, NodeStatus.SUCCESS)
    try:
        material = requirements(target)
    except (OSError, ValueError) as error:
        terminal.step_result(2, NodeStatus.FAILED)
        raise SystemExit(f'读取来源材料失败：{error}') from None
    terminal.step_result(2, NodeStatus.SUCCESS)
    return build(material)


if __name__ == '__main__':
    terminal = Terminal()
    raise SystemExit(main(lambda args: deployment(args, terminal), configure=configure, terminal=terminal))
