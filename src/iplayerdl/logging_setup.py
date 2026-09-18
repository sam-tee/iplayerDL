"""Central logging setup: console level comes from config, default auto.

`auto` (the default) means INFO on an interactive terminal and WARNING
everywhere else (systemd services, pipes, cron), so the journal stays
quiet unless the config pins a level explicitly.
"""

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}

# Environment variables systemd always sets for services it supervises.
_SYSTEMD_ENV_VARS = ("INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID")


def running_under_systemd() -> bool:
    """Detect execution as a systemd service (journal gets our stderr)."""
    return any(os.getenv(var) for var in _SYSTEMD_ENV_VARS)


def interactive_console() -> bool:
    """True when a human is likely watching stderr right now."""
    try:
        return sys.stderr.isatty() and not running_under_systemd()
    except Exception:  # noqa: BLE001 - logging detection must never fail
        return False


def auto_level() -> int:
    """Context default: INFO for interactive use, WARNING otherwise."""
    return logging.INFO if interactive_console() else logging.WARNING


def normalize_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    if not level:
        return auto_level()
    key = str(level).strip().upper()
    if key == "AUTO":
        return auto_level()
    return _LEVELS.get(key, logging.WARNING)


def get_log_level(config: object | None) -> int:
    """Effective console log level: explicit config wins, else auto."""
    try:
        level = getattr(getattr(config, "logging", None), "level", None)
    except Exception:  # noqa: BLE001 - never break startup over logging config
        level = None
    return normalize_level(level if level is not None else "AUTO")


def setup_logging(level: str | int | None = "AUTO") -> int:
    """Configure the root logger for console output at the given level.

    Only the console handler is managed here; other handlers (e.g. the web
    UI's in-memory handler) are left untouched. Safe to call multiple times.
    Returns the numeric level applied.
    """
    level_no = normalize_level(level)
    root = logging.getLogger()
    root.setLevel(level_no)

    console = None
    for handler in root.handlers:
        if getattr(handler, "_iplayerdl_console", False):
            console = handler
            break
    if console is None:
        console = logging.StreamHandler(sys.stderr)
        console._iplayerdl_console = True  # type: ignore[attr-defined]
        console.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(console)
    console.setLevel(level_no)
    console.setFormatter(logging.Formatter(_FORMAT))
    return level_no
