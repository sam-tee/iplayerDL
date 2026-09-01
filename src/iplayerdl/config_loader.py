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
urls = []

# Environment variables to load into the process before the pipeline runs.
[environment]
TMDB_API_KEY = ""
RADARR_URL = ""
RADARR_API_KEY = ""
SONARR_URL = ""
SONARR_API_KEY = ""
CBC_EMAIL = ""
CBC_PASSWORD = ""

[folders]
download_dir = "./download" # where files will be downloaded to
media_dir = ""              # where files will be written to after transcode
transcode_dir = "./transcode"

[pipeline]
transcode = true
delete_downloads = true
max_non_transcoded = 5
allow_speculative_adds = false

[download_settings]
format = "bv*+ba[language=en]/bv*+ba/best"
subtitleslangs = ["en.*"]
writesubtitles = true
quiet = true
noprogress = false
check_formats = true
ignoreerrors = "only_download"

[transcode_settings]
encoder = "none" # one of qsv, vaapi, none
quality = 20
device = "/dev/dri/renderD128"
crop = true

[ntfy]
url_base = "https://ntfy.example.com/"
topic = "iplayerDL"
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

    def _replace_line(m: re.Match) -> str:
        key = m.group(1)
        if key in env_vars:
            comment = m.group(2) or ""
            return f"{key} = {json.dumps(env_vars[key])}{comment}"
        return m.group(0)

    new_section = re.sub(
        r"(?m)^([A-Za-z_][\w-]*)[ \t]*=.*?(?:([ \t]*#.*))?$", _replace_line, section
    )
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


def load_config(config_file: Path | None = None) -> Config:
    dacite_config = DaciteConfig(type_hooks={Path: Path})
    if config_file is None:
        config_file = ensure_config(get_config_path())
    with open(config_file, "rb") as f:
        data = tomllib.load(f)
    return from_dict(data_class=Config, data=data, config=dacite_config)


def apply_environment(config: Config) -> None:
    """Export the [environment] section of the config into os.environ."""
    for key, value in config.environment.items():
        os.environ[key] = str(value)


if __name__ == "__main__":
    print(get_config_path())
    print(load_config())
