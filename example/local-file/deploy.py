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
        raise ValueError('Target directory must not be empty')
    return Path(value).expanduser().absolute()


def requirements(target_dir: Path) -> FileArtifact:
    material = inspect_file(SOURCE)
    if not material.exists:
        raise FileNotFoundError(f'Source file {SOURCE} does not exist')
    return FileArtifact(target_dir / 'message.txt', material.sha256, 0o644)


def choose(material: FileArtifact, observed: FileObservation) -> str:
    return 'reuse' if (observed.exists and observed.sha256 == material.sha256
                       and observed.mode == material.mode) else 'deliver'


def reuse(material: FileArtifact) -> NodeResult:
    return NodeResult(NodeStatus.SKIPPED, material, f'File {material.path} already matches')


def content_check(artifact: FileArtifact) -> CheckResult:
    observed = inspect_file(artifact.path)
    passed = observed.exists and observed.sha256 == artifact.sha256
    return CheckResult(CheckVerdict.PASSED if passed else CheckVerdict.NOT_PASSED,
                       f'Content digest {"matches" if passed else "does not match"}: {artifact.path}')


def permission_check(artifact: FileArtifact) -> CheckResult:
    observed = inspect_file(artifact.path)
    passed = observed.exists and observed.mode == artifact.mode
    return CheckResult(CheckVerdict.PASSED if passed else CheckVerdict.NOT_PASSED,
                       f'Permissions {"match" if passed else "do not match"} 0644: {artifact.path}')


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--target-dir', help='Directory in which to install message.txt')


def build(args: argparse.Namespace) -> WorkflowSpec:
    spec = WorkflowSpec(addstart=True)
    target = input_node(
        spec, 'target', name='Target directory', purpose='Choose the installation directory',
        stage=Stage.GUIDANCE, inputs={'value': args.target_dir}, output='target_dir',
        prompt='Target directory', default=None, validate=target_directory,
    )
    material = function_node(
        spec, 'material', name='Source material', purpose='Read the required content and permissions',
        stage=Stage.GUIDANCE, inputs={'target_dir': Ref('target_dir')}, output='material',
        function=requirements, summary=lambda value: f'{SOURCE} → {value.path}, mode 0644',
    )
    observed = function_node(
        spec, 'observe', name='Observe target', purpose='Read current target content and permissions',
        stage=Stage.EXECUTION, inputs={'material': Ref('material')}, output='observed',
        function=lambda material: inspect_file(material.path), summary=str,
    )
    selection = function_node(
        spec, 'choose', name='Choose action', purpose='Select delivery or reuse from the observed file',
        stage=Stage.EXECUTION, inputs={'material': Ref('material'), 'observed': Ref('observed')},
        output='route', function=choose, summary=str,
    )
    choice = ExclusiveChoice(spec, 'delivery_choice')
    deliver = file_node(
        spec, 'deliver', name='Deliver file', purpose='Install message.txt with permissions 0644',
        stage=Stage.EXECUTION, inputs={'source': SOURCE, 'destination': Ref('destination')},
        output='artifact', mode=0o644,
    )
    destination = function_node(
        spec, 'destination', name='Delivery destination', purpose='Use the selected installation path',
        stage=Stage.EXECUTION, inputs={'material': Ref('material')}, output='destination',
        function=lambda material: material.path, summary=str,
    )
    reused = action_node(
        spec, 'reuse', name='Reuse file', purpose='Keep the matching file and provide its artifact',
        stage=Stage.EXECUTION, inputs={'material': Ref('material')}, output='artifact',
        prepare=reuse, allows=lambda scope: False,
    )
    content = check_node(
        spec, 'content_check', name='Content check', purpose='Verify the installed content digest',
        stage=Stage.ACCEPTANCE, inputs={'artifact': Ref('artifact')}, output='content_result',
        check=content_check,
    )
    permissions = check_node(
        spec, 'permission_check', name='Permission check', purpose='Verify the installed permissions',
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
