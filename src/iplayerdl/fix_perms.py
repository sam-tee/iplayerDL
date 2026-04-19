import subprocess
from pathlib import Path

from iplayerdl.classes import Permissions


def mk_chown(type: str, perm: int, root_dir: Path) -> list[str]:
    return [
        "find",
        str(root_dir),
        "-type",
        type,
        "-exec",
        "chmod",
        str(perm),
        "{}",
        "\\;",
    ]


def fix_perms(root_dir: Path, perms: Permissions):
    """
    Sets owner, group and permissions of files in root_dir recursively
    """

    subprocess.run(
        mk_chown("f", perms.file_perms, root_dir),
        capture_output=True,
    )
    subprocess.run(
        mk_chown("d", perms.dir_perms, root_dir),
        capture_output=True,
    )
    try:
        subprocess.run(
            ["sudo", "chown", f"{perms.owner}:{perms.group}", str(root_dir), "-R"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print("\033[31mError: Changing ownership failed\033[0m")
        print(e.stdout)
        print(e.stderr)


if __name__ == "__main__":
    perms = Permissions(
        enable=True, owner="sam", group="media", file_perms=664, dir_perms=775
    )
    fix_perms(Path("/home/sam/hardDrive/media"), perms)
