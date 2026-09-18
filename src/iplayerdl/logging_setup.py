"""Central logging setup: console level comes from config, default WARNING."""

import logging
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


def normalize_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    if not level:
        return logging.WARNING
    key = str(level).strip().upper()
    return _LEVELS.get(key, logging.WARNING)


def get_log_level(config: object | None) -> int:
    """Extract the configured console log level, defaulting to WARNING."""
    try:
        level = getattr(getattr(config, "logging", None), "level", None)
    except Exception:  # noqa: BLE001 - never break startup over logging config
        level = None
    return normalize_level(level if level is not None else "WARNING")


def setup_logging(level: str | int | None = "WARNING") -> int:
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
