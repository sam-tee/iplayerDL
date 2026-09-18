import json
import logging
import os
import re
import shutil
import tomllib
from pathlib import Path

from dacite import Config as DaciteConfig
from dacite import from_dict

from iplayerdl.classes import (
    DEFAULT_DOWNLOAD_SETTINGS,
    Config,
    Folders,
    LoggingConfig,
    NtfyConfig,
    Pipeline,
    TranscodeSettings,
    WebConfig,
)

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "IPLAYERDL_CONFIG"
APP_DIR_NAME = "iplayerdl"
CONFIG_FILE_NAME = "config.toml"

# Secret names documented under [environment]. The section itself defaults
# to empty; these render commented-out so the names stay discoverable.
KNOWN_ENVIRONMENT_KEYS = (
    "TMDB_API_KEY",
    "RADARR_URL",
    "RADARR_API_KEY",
    "SONARR_URL",
    "SONARR_API_KEY",
    "CBC_EMAIL",
    "CBC_PASSWORD",
)


def _toml_literal(value: object) -> str:
    """Render a dataclass default as TOML (loudly fails on new types)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Path):
        return json.dumps(str(value))
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(v) for v in value) + "]"
    raise ValueError(f"Cannot render default {value!r} as TOML")


def _section(
    table: str, blurb: list[str], keys: list[tuple[str, object, list[str]]]
) -> str:
    """Render one commented-out TOML table; values always come from live defaults."""
    lines = [f"# {line}" for line in blurb] + [f"[{table}]"]
    for key, value, doc in keys:
        lines.extend(f"# {line}" for line in doc)
        lines.append(f"# {key} = {_toml_literal(value)}")
    return "\n".join(lines)


def _build_default_config() -> str:
    """Generate the default config template from the dataclass defaults.

    Values are read from live class instances, so the template can never
    drift from the code. Only the prose docs below are hand-written.
    """
    folders = Folders()
    pipeline = Pipeline()
    transcode = TranscodeSettings()
    ntfy = NtfyConfig()
    web = WebConfig()
    logging_cfg = LoggingConfig()
    parts = [
        "# iplayerDL settings",
        _section(
            "ntfy",
            ["ntfy notifications on pipeline success/failure. Leave topic empty to skip."],
            [("url_base", ntfy.url_base, []), ("topic", ntfy.topic, [])],
        ),
        _section(
            "web",
            [
                "Web interface bind address and port (iplayerdl web). Command-line",
                "--host/--port flags override these when given.",
            ],
            [("host", web.host, []), ("port", web.port, [])],
        ),
        _section(
            "environment",
            [
                "Secrets and service credentials. Exported into the process environment",
                "before the pipeline runs, so nothing secret ever lives anywhere else.",
            ],
            [(key, "", []) for key in KNOWN_ENVIRONMENT_KEYS],
        ),
        _section(
            "folders",
            ["Filesystem locations."],
            [
                ("download_dir", folders.download_dir, ["where full-quality files are downloaded to"]),
                ("media_dir", folders.media_dir, ["where finished files are written after transcode"]),
                ("transcode_dir", folders.transcode_dir, ["where temporary transcode files go"]),
            ],
        ),
        _section(
            "pipeline",
            ["Pipeline behaviour."],
            [
                ("transcode", pipeline.transcode, ["re-encode downloads with ffmpeg (false links/copies instead)"]),
                ("delete_downloads", pipeline.delete_downloads, ["delete full-quality downloads after a successful move"]),
                (
                    "max_non_transcoded",
                    pipeline.max_non_transcoded,
                    [
                        "cap on full-quality downloads waiting for transcode/move;",
                        "useful when delete_downloads is true and disk space is tight",
                    ],
                ),
                (
                    "allow_speculative_adds",
                    pipeline.allow_speculative_adds,
                    [
                        "let the resolver add missing shows/movies to Sonarr/Radarr",
                        "(rolled back if no episode matches); otherwise only match your",
                        "existing libraries (+ TMDb fallback)",
                    ],
                ),
            ],
        ),
        _section(
            "download_settings",
            [
                "yt-dlp download options, passed straight through to yt-dlp. The",
                "opinionated set below applies when unset; override individual keys",
                "as needed.",
            ],
            [(key, value, []) for key, value in DEFAULT_DOWNLOAD_SETTINGS.items()],
        ),
        _section(
            "transcode_settings",
            ["ffmpeg transcode options."],
            [
                ("encoder", transcode.encoder, ['one of "none" (libsvtav1/AV1 CPU), "qsv", "vaapi", "apple"']),
                ("quality", transcode.quality, ["encoder quality; lower is better quality (larger files)"]),
                ("device", transcode.device, ["GPU render node for qsv/vaapi"]),
                ("crop", transcode.crop, ["detect and remove letterbox/pillarbox padding"]),
            ],
        ),
        _section(
            "logging",
            [
                "Console log level: an explicit value wins everywhere. \"auto\" (the",
                "default) means WARNING on an interactive terminal and INFO otherwise",
                "(systemd journal, pipes, cron), keeping live output quiet while",
                "unattended logs keep full detail.",
                "One of auto, DEBUG, INFO, WARNING, ERROR, CRITICAL.",
            ],
            [("level", logging_cfg.level, [])],
        ),
    ]
    return "\n\n".join(parts) + "\n"


DEFAULT_CONFIG = _build_default_config()


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
    migrated_env_file = legacy.parent / ".env.migrated"
    created = False
    if not path.exists():
        created = True
        path.parent.mkdir(parents=True, exist_ok=True)
        if legacy.exists():
            shutil.copyfile(legacy, path)
        else:
            path.write_text(DEFAULT_CONFIG)
    # One-time migration of a legacy .env into [environment], only for
    # freshly created configs. Afterwards the .env is archived so later
    # runs never touch the config again (hand edits always win).
    if created and env_file.exists():
        try:
            env_vars = _parse_env_file(env_file)
            if env_vars:
                text = path.read_text()
                merged = _merge_environment_section(text, env_vars)
                if merged != text:
                    path.write_text(merged)
                logger.info(
                    "Migrated %d keys from %s into [environment]",
                    len(env_vars),
                    env_file,
                )
            env_file.replace(migrated_env_file)
        except OSError as e:
            logger.warning(
                "Legacy .env migration failed: %s: %s",
                type(e).__name__,
                e,
            )
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
        "web",
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
    config = from_dict(data_class=Config, data=data, config=dacite_config)
    # An explicitly empty [download_settings] table would otherwise shadow
    # the opinionated code defaults with {}; merge instead so individual
    # keys can be overridden while the rest fall back.
    user_ds = data.get("download_settings")
    if isinstance(user_ds, dict):
        config.download_settings = {**DEFAULT_DOWNLOAD_SETTINGS, **user_ds}
    return config


def apply_environment(config: Config) -> None:
    """Export the [environment] section of the config into os.environ."""
    for key, value in config.environment.items():
        os.environ[key] = str(value)


if __name__ == "__main__":
    print(get_config_path())
    print(load_config())
