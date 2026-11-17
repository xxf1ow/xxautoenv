"""Execute local argv commands with inherited or streamed standard IO."""

import codecs
import locale
import os
import selectors
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class CommandRequest:
    """Bind argv, working directory, and explicit inherited-environment overrides.

    Copy argv and env at construction so later caller mutations cannot alter a
    prepared command. Interactive commands inherit the process's standard IO.
    """

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    interactive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'argv', tuple(self.argv))
        object.__setattr__(self, 'env', MappingProxyType(dict(self.env)))


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    """A nonzero command exit, retaining the bound request and complete output."""

    def __init__(self, request: CommandRequest, result: CommandResult):
        self.request = request
        self.result = result
        super().__init__(
            f'命令 {request.argv!r} 在 {str(request.cwd)!r} 退出，退出码 '
            f'{result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
        )


def execute_command(
    request: CommandRequest,
    *,
    stdout: Callable[[str], None],
    stderr: Callable[[str], None],
) -> CommandResult:
    """Run argv without a shell; stream and retain noninteractive text verbatim.

    Explicit env entries overlay the current environment. Interactive output
    goes directly to inherited streams and produces empty captured strings.
    Nonzero exits raise CommandError. On interruption or callback failure, stop
    and reap this command's process before propagating the original exception.
    """
    captured = [[], []]
    process = subprocess.Popen(
        request.argv, cwd=request.cwd, env={**os.environ, **request.env},
        stdout=None if request.interactive else subprocess.PIPE,
        stderr=None if request.interactive else subprocess.PIPE,
    )
    try:
        if not request.interactive:
            with selectors.DefaultSelector() as selector:
                for index, (pipe, callback) in enumerate(
                    ((process.stdout, stdout), (process.stderr, stderr))
                ):
                    decoder = codecs.getincrementaldecoder(locale.getencoding())()
                    selector.register(pipe, selectors.EVENT_READ, (index, callback, decoder))
                while selector.get_map():
                    for key, _ in selector.select():
                        index, callback, decoder = key.data
                        chunk = os.read(key.fd, 65536)
                        text = decoder.decode(chunk, final=not chunk)
                        if text:
                            captured[index].append(text)
                            callback(text)
                        if not chunk:
                            selector.unregister(key.fileobj)
        returncode = process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    result = CommandResult(returncode, ''.join(captured[0]), ''.join(captured[1]))
    if returncode:
        raise CommandError(request, result)
    return result
