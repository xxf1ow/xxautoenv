"""Observe and deliver ordinary local files through a prepared Action."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import stat
import tempfile

from ..contracts import Action, ActionScope, NodeResult, NodeStatus


@dataclass(frozen=True)
class FileArtifact:
    path: Path
    sha256: str
    mode: int


@dataclass(frozen=True)
class FileObservation:
    path: Path
    exists: bool
    sha256: str | None
    mode: int | None


def _read_file(path: Path) -> tuple[FileObservation, bytes | None]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return FileObservation(path, False, None, None), None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f'文件 {path} 必须是普通文件，不能是符号链接或其他文件类型')
    content = path.read_bytes()
    return FileObservation(path, True, hashlib.sha256(content).hexdigest(),
                           stat.S_IMODE(metadata.st_mode)), content


def inspect_file(path: Path) -> FileObservation:
    """Read digest and permissions without writes; missing paths are allowed.

    Symlinks (including dangling links) and other nonregular files raise
    ValueError. Other filesystem errors propagate to the caller.
    """
    return _read_file(path)[0]


def prepare_file(source: Path, destination: Path, mode: int) -> Action | NodeResult:
    """Bind source digest, destination state, and explicit parent creation.

    Matching content and mode return a SKIPPED result with a FileArtifact.
    Otherwise execute rechecks those observations, writes a same-directory
    temporary file, sets mode, and replaces the destination. Changed premises
    fail without rebinding. Temporary files are cleaned on failure; created
    parents are retained. This is not a transaction or a concurrent-write lock.
    Missing sources raise FileNotFoundError; nonregular files raise ValueError.
    """
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    material = inspect_file(source)
    if not material.exists:
        raise FileNotFoundError(f'来源文件 {source} 不存在')
    observed = inspect_file(destination)
    artifact = FileArtifact(destination, material.sha256, mode)
    if observed.exists and observed.sha256 == material.sha256 and observed.mode == mode:
        return NodeResult(NodeStatus.SKIPPED, artifact, f'文件 {destination} 已符合要求')

    missing = []
    anchor = destination.parent
    while not anchor.exists():
        # A dangling parent symlink is a conflict, not a directory to create.
        if anchor.is_symlink():
            raise ValueError(f'父路径 {anchor} 是悬空符号链接')
        missing.append(anchor)
        anchor = anchor.parent
    if not anchor.is_dir():
        raise NotADirectoryError(f'父路径 {anchor} 不是目录')
    missing.reverse()
    parent_metadata = anchor.stat()
    parent_link = anchor.lstat()
    parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino,
                       parent_link.st_dev, parent_link.st_ino)
    parents = tuple(missing)
    scope = (ActionScope('write', str(destination)),) + tuple(
        ActionScope('mkdir', str(parent)) for parent in parents
    )

    def verify_destination() -> None:
        if inspect_file(destination) != observed:
            raise RuntimeError(f'目标文件 {destination} 在准备后发生变化')

    def execute() -> NodeResult:
        current, content = _read_file(source)
        if not current.exists or current.sha256 != material.sha256:
            raise RuntimeError(f'来源文件 {source} 在准备后发生变化')
        verify_destination()
        metadata = anchor.stat()
        link = anchor.lstat()
        identity = (metadata.st_dev, metadata.st_ino, link.st_dev, link.st_ino)
        if not anchor.is_dir() or identity != parent_identity:
            raise RuntimeError(f'父目录 {anchor} 在准备后发生变化')
        for parent in parents:
            if parent.exists() or parent.is_symlink():
                raise RuntimeError(f'父目录 {parent} 在准备后出现')
        for parent in parents:
            parent.mkdir()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fchmod(handle.fileno(), mode)
            # Preserve an external target update that occurs while preparing bytes.
            verify_destination()
            os.replace(temporary, destination)
            return NodeResult(NodeStatus.SUCCESS, artifact, f'已交付文件 {destination}')
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    description = f'将 {source}（sha256: {material.sha256}）交付到 {destination}，权限 {mode:04o}'
    if parents:
        description += '；创建目录：' + ', '.join(str(parent) for parent in parents)
    return Action(description, scope, execute, changes=True)
