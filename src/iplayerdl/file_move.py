import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass
class RemotePath:
    host: str
    path: str


def parse_remote_path(path: Path) -> RemotePath | None:
    """
    Detect paths of the form <host>:<path>.
    """
    path_str = str(path)
    host, sep, remote_path = path_str.partition(":")
    if not sep or not host or "/" in host or not remote_path:
        return None
    return RemotePath(host=host, path=remote_path)


def remote_parent(path: str) -> str:
    return str(PurePosixPath(path).parent)


def move_remote(src: Path, dst: RemotePath):
    cmd = (
        f"mkdir -p -- {shlex.quote(remote_parent(dst.path))} "
        f"&& cat > {shlex.quote(dst.path)}"
    )
    with open(src.resolve(), "rb") as f:
        subprocess.run(["ssh", dst.host, cmd], stdin=f, check=True)
    src.unlink()


def move_file(src: Path, dst: Path):
    remote_output = parse_remote_path(dst)
    if remote_output is None:
        dst.parent.mkdir(exist_ok=True, parents=True)
        shutil.move(src.resolve(), dst.resolve())
    else:
        move_remote(src, remote_output)
