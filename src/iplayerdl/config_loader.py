import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from dacite import Config as DaciteConfig
from dacite import from_dict

from iplayerdl.classes import Config

CONFIG_ENV_VAR = "IPLAYERDL_CONFIG"
APP_DIR_NAME = "iplayerdl"
CONFIG_FILE_NAME = "config.toml"

DEFAULT_CONFIG = """# iplayerDL settings
#
# Everything below is commented out and shows the default value for each
# option. To override a default, uncomment its [section] header AND the
# option line, then change the value. (TOML files options under the most
# recent [section] header, so uncommenting an option without its header
# puts it in the wrong place and iplayerDL will tell you.)
#
# The urls list is special: it lives at the top level, above all [sections].
#
# This file is created automatically at first run and lives at
# $XDG_CONFIG_HOME/iplayerdl/config.toml (normally
# ~/.config/iplayerdl/config.toml). Set the IPLAYERDL_CONFIG environment
# variable to use a different location. Values from a legacy .env file next
# to the old repo config.toml are migrated into [environment] on startup.
#
# The web interface (iplayerdl web) can edit this file for you.

# URLs to process. Saving URLs in the web interface replaces this list.
# urls = []

# Secrets and service credentials. Exported into the process environment
# before the pipeline runs, so nothing secret ever lives anywhere else.
# [environment]
# TMDB_API_KEY = ""
# RADARR_URL = ""
# RADARR_API_KEY = ""
# SONARR_URL = ""
# SONARR_API_KEY = ""
# CBC_EMAIL = ""
# CBC_PASSWORD = ""

# Filesystem locations. Relative paths resolve against the working directory
# of the iplayerdl process.
# [folders]
# download_dir = "./download"  # where full-quality files are downloaded to
# media_dir = ""               # where finished files are written after transcode
# transcode_dir = "./transcode"  # where temporary transcode files go

# Pipeline behaviour.
# [pipeline]
# transcode = true           # re-encode downloads with ffmpeg (false links/copies instead)
# delete_downloads = true    # delete full-quality downloads after a successful move
# max_non_transcoded = 5     # cap on full-quality downloads waiting for transcode/move;
#                            # useful when delete_downloads is true and disk space is tight
# allow_speculative_adds = false  # let the resolver add missing shows/movies to
#                                 # Sonarr/Radarr (rolled back if no episode matches);
#                                 # otherwise only match your existing libraries (+ TMDb fallback)

# yt-dlp download options, passed straight through to yt-dlp. Absent keys
# fall back to yt-dlp's own defaults.
# [download_settings]
# format = "bv*+ba[language=en]/bv*+ba/best"
# subtitleslangs = ["en.*"]
# writesubtitles = true
# quiet = true
# noprogress = false
# check_formats = true
# ignoreerrors = "only_download"

# ffmpeg transcode options.
# [transcode_settings]
# encoder = "none"  # one of "none" (libsvtav1/AV1 CPU), "qsv", "vaapi", "apple"
# quality = 20      # encoder quality; lower is better quality (larger files)
# device = "/dev/dri/renderD128"  # GPU render node for qsv/vaapi
# crop = true       # detect and remove letterbox/pillarbox padding

# ntfy notifications on pipeline success/failure. Leave topic empty to skip.
# [ntfy]
# url_base = "https://ntfy.example.com/"
# topic = "iplayerDL"

# Map an iPlayer title to the exact title used for matching, e.g.
# "What We Do in the Shadows, Series 3, The Wellness Centre" = "What We Do in the Shadows, Series 3, The Wellness Center"
# [title_overrides]

# Console log level: an explicit value wins everywhere. "auto" (the
# default) means INFO on an interactive terminal and WARNING otherwise
# (systemd journal, pipes, cron), keeping unattended logs quiet.
# One of auto, DEBUG, INFO, WARNING, ERROR, CRITICAL.
# [logging]
# level = "auto"
"""


def get_config_path() -> Path:
    """Locate config.toml.

    Order: $IPLAYERDL_CONFIG, then $XDG_CONFIG_HOME/iplayerdl/config.toml
    (falling back to ~/.config/iplayerdl/config.toml). The file is created
    from a template if it does not exist yet.
    """
    env_path = os.getenv(CONFIG_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser()
    xdg_config_home = Path(
        os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")
    ).expanduser()
    return xdg_config_home / APP_DIR_NAME / CONFIG_FILE_NAME


def _parse_env_file(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        result[key.strip()] = value
    return result


def _split_toml_comment(line: str) -> tuple[str, str]:
    """Split a TOML line into (code, comment).

    A `#` inside a single- or double-quoted string is part of the value,
    not a comment (e.g. passwords containing `#`). Triple-quoted
    multi-line strings are not expected in config files.
    """
    in_single = False
    in_double = False
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
        elif ch == "\\" and in_double:
            escaped = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i], line[i:]
    return line, ""


def _merge_environment_section(text: str, env_vars: dict[str, str]) -> str:
    """Merge key/value pairs into the [environment] section of a TOML string."""
    match = re.search(r"(?m)^\[environment\][ \t]*(?:#.*)?$", text)
    if match is None:
        entries = "".join(f"{key} = {json.dumps(value)}\n" for key, value in env_vars.items())
        return f"{text.rstrip()}\n\n[environment]\n{entries}"
    start = match.end()
    next_table = re.search(r"(?m)^\[", text[start:])
    end = start + next_table.start() if next_table else len(text)
    section = text[start:end]

    key_re = re.compile(r"^([A-Za-z_][\w-]*)[ \t]*=")
    lines = []
    for line in section.splitlines(keepends=True):
        code, comment = _split_toml_comment(line.rstrip("\n"))
        m = key_re.match(code)
        if m and m.group(1) in env_vars:
            eol = "\n" if line.endswith("\n") else ""
            suffix = f" {comment.lstrip()}" if comment.strip() else ""
            lines.append(f"{m.group(1)} = {json.dumps(env_vars[m.group(1)])}{suffix}{eol}")
        else:
            lines.append(line)
    new_section = "".join(lines)
    missing = [
        key
        for key in env_vars
        if not re.search(rf"(?m)^{re.escape(key)}[ \t]*=", new_section)
    ]
    if missing:
        additions = "".join(
            f"{key} = {json.dumps(env_vars[key])}\n" for key in missing
        )
        new_section = new_section.rstrip() + "\n" + additions
        if end != len(text):
            new_section += "\n"
    return text[:start] + new_section + text[end:]


def ensure_config(path: Path) -> Path:
    legacy = Path(__file__).resolve().parent.parent.parent / CONFIG_FILE_NAME
    env_file = legacy.parent / ".env"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if legacy.exists():
            shutil.copyfile(legacy, path)
        else:
            path.write_text(DEFAULT_CONFIG)
    # Migrate any legacy .env values into the [environment] section.
    # This also covers custom $IPLAYERDL_CONFIG paths that were already created.
    if env_file.exists():
        try:
            text = path.read_text()
            env_vars = _parse_env_file(env_file)
            if env_vars:
                merged = _merge_environment_section(text, env_vars)
                if merged != text:
                    path.write_text(merged)
        except OSError:
            pass
    return path


ROOT_TABLES = frozenset(
    {
        "urls",
        "environment",
        "folders",
        "pipeline",
        "download_settings",
        "transcode_settings",
        "title_overrides",
        "ntfy",
        "logging",
    }
)


def _check_misplaced_keys(data: dict) -> None:
    """Point out root-level keys accidentally nested inside a [table].

    With everything commented out by default it is easy to uncomment (or
    append) e.g. `urls` underneath a live [table] header. TOML then files it
    under that table and dacite would fail cryptically, so raise a clear
    error naming only the offending keys (never their values, which may be
    secrets).
    """
    for table, values in data.items():
        if not isinstance(values, dict):
            continue
        misplaced = sorted(ROOT_TABLES.intersection(values) - {table})
        if misplaced:
            raise ValueError(
                f"Invalid config: {', '.join(repr(k) for k in misplaced)} "
                f"must be top-level, but was found inside [{table}]. "
                "Move it above the first [table] header (or uncomment its own "
                "[section] header)."
            )
    env = data.get("environment")
    if isinstance(env, dict):
        non_strings = sorted(k for k, v in env.items() if not isinstance(v, str))
        if non_strings:
            raise ValueError(
                f"Invalid config: [{', '.join(non_strings)}] inside "
                "[environment] must be strings (e.g. key = \"value\")."
            )


def load_config(config_file: Path | None = None) -> Config:
    dacite_config = DaciteConfig(type_hooks={Path: Path})
    if config_file is None:
        config_file = ensure_config(get_config_path())
    with open(config_file, "rb") as f:
        data = tomllib.load(f)
    _check_misplaced_keys(data)
    return from_dict(data_class=Config, data=data, config=dacite_config)


def apply_environment(config: Config) -> None:
    """Export the [environment] section of the config into os.environ."""
    for key, value in config.environment.items():
        os.environ[key] = str(value)


if __name__ == "__main__":
    print(get_config_path())
    print(load_config())
