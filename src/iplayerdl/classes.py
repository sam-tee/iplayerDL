import threading
from dataclasses import dataclass, field
from pathlib import Path
from threading import BoundedSemaphore


class DownloadCancelled(Exception):
    """Raised to abort work for a URL cancelled via the web UI."""


@dataclass
class Task:
    input_file: Path
    transcode_file: Path
    output_file: Path
    download_slot: BoundedSemaphore | None = None
    url: str | None = None


@dataclass
class Folders:
    download_dir: Path = Path("./download")
    media_dir: Path = Path("")
    transcode_dir: Path = Path("./transcode")


@dataclass
class Pipeline:
    transcode: bool = True
    delete_downloads: bool = True
    max_non_transcoded: int | None = 5
    allow_speculative_adds: bool = False


@dataclass
class Stats:
    resolved: int = 0
    unresolved: int = 0
    skipped: int = 0
    completed: int = 0
    failed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def summary(self) -> str:
        with self._lock:
            completed = self.completed
            failed = self.failed
            unresolved = self.unresolved
            skipped = self.skipped
        parts = [
            f"{completed} processed",
            f"{failed} failed",
            f"{unresolved} unresolved",
            f"{skipped} skipped",
        ]
        return " | ".join(parts)


@dataclass
class TranscodeSettings:
    device: str = "/dev/dri/renderD128"
    quality: int = 20
    encoder: str = "none"
    crop: bool = True


@dataclass
class NtfyConfig:
    url_base: str = "https://ntfy.example.com/"
    topic: str = "iplayerDL"


@dataclass
class LoggingConfig:
    level: str = "WARNING"


@dataclass
class Config:
    folders: Folders = field(default_factory=Folders)
    urls: list[str] = field(default_factory=list)
    pipeline: Pipeline = field(default_factory=Pipeline)
    transcode_settings: TranscodeSettings = field(default_factory=TranscodeSettings)
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    download_settings: dict = field(default_factory=dict)
    title_overrides: dict = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
