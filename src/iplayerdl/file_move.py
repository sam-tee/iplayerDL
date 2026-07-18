import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RemotePath:
    host: str
    path: Path

    def get_scp_host(self) -> str:
        return f"{self.host}:{self.path}"


def parse_remote_path(path: Path) -> RemotePath | None:
    """
    Detect paths of the form <host>:<path>.
    """
    path_str = str(path)
    # windows check - local
    if path.drive:
        return None
    host, sep, remote_path = path_str.partition(":")
    if not sep or not host or not remote_path:
        return None
    if "/" in host or "\\" in host:
        return None
    return RemotePath(host, Path(remote_path).expanduser().resolve())


def move_file(src: Path, dst: Path):
    remote_output = parse_remote_path(dst)
    if remote_output is None:
        dst.parent.mkdir(exist_ok=True, parents=True)
        shutil.move(src.resolve(), dst.resolve())
    else:
        subprocess.run(
            [
                "ssh",
                remote_output.host,
                f"mkdir -p -- {shlex.quote(str(remote_output.path.parent))}",
            ],
            check=True,
        )
        subprocess.run(
            ["scp", str(src.resolve()), remote_output.get_scp_host()],
            check=True,
        )
        src.unlink()
