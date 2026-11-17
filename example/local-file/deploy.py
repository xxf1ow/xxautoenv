"""Install one local text file using a consumer-owned native workflow."""

import argparse
from pathlib import Path

from SpiffWorkflow.operators import Attrib, Equal
from SpiffWorkflow.specs import ExclusiveChoice, WorkflowSpec

from xxautoenv.contracts import CheckResult, CheckVerdict, NodeResult, NodeStatus, Ref, Stage
from xxautoenv.nodes.check import check_node
from xxautoenv.nodes.file import file_node
from xxautoenv.nodes.function import action_node, function_node
from xxautoenv.nodes.input import input_node
from xxautoenv.operations.files import FileArtifact, FileObservation, inspect_file
from xxautoenv.runner import main


SOURCE = Path(__file__).resolve().parent / 'assets' / 'message.txt'


def target_directory(value: str) -> Path:
    if not value.strip():
        raise ValueError('目标目录不能为空')
    return Path(value).expanduser().absolute()


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
    parser.add_argument('--target-dir', help='安装 message.txt 的目录')


def build(args: argparse.Namespace) -> WorkflowSpec:
    spec = WorkflowSpec(addstart=True)
    target = input_node(
        spec, 'target', name='目标目录', purpose='选择安装目录',
        stage=Stage.GUIDANCE, inputs={'value': args.target_dir}, output='target_dir',
        prompt='目标目录', default=None, validate=target_directory,
    )
    material = function_node(
        spec, 'material', name='来源材料', purpose='读取所需内容和权限',
        stage=Stage.GUIDANCE, inputs={'target_dir': Ref('target_dir')}, output='material',
        function=requirements, summary=lambda value: f'{SOURCE} → {value.path}，权限 0644',
    )
    observed = function_node(
        spec, 'observe', name='观察目标', purpose='读取目标当前内容和权限',
        stage=Stage.EXECUTION, inputs={'material': Ref('material')}, output='observed',
        function=lambda material: inspect_file(material.path),
        summary=lambda value: (f'目标存在，摘要 {value.sha256}，权限 {value.mode:04o}'
                               if value.exists else f'目标尚不存在：{value.path}'),
    )
    selection = function_node(
        spec, 'choose', name='选择动作', purpose='根据文件现状选择交付或复用',
        stage=Stage.EXECUTION, inputs={'material': Ref('material'), 'observed': Ref('observed')},
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
        stage=Stage.EXECUTION, inputs={'material': Ref('material')}, output='destination',
        function=lambda material: material.path, summary=str,
    )
    reused = action_node(
        spec, 'reuse', name='复用文件', purpose='保留符合要求的文件并提供产出',
        stage=Stage.EXECUTION, inputs={'material': Ref('material')}, output='artifact',
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
    spec.start.connect(target)
    target.connect(material)
    material.connect(observed)
    observed.connect(selection)
    selection.connect(choice)
    choice.connect(destination)
    choice.connect_if(Equal(Attrib('route'), 'reuse'), reused)
    destination.connect(deliver)
    deliver.connect(content)
    reused.connect(content)
    content.connect(permissions)
    return spec


if __name__ == '__main__':
    raise SystemExit(main(build, configure=configure))
