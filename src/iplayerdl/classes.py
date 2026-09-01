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
    download_dir: Path
    media_dir: Path
    transcode_dir: Path


@dataclass
class Pipeline:
    transcode: bool
    delete_downloads: bool
    max_non_transcoded: int | None = None
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
    device: str
    quality: int
    encoder: str
    crop: bool


@dataclass
class NtfyConfig:
    url_base: str
    topic: str


@dataclass
class Config:
    folders: Folders
    urls: list[str]
    pipeline: Pipeline
    transcode_settings: TranscodeSettings
    ntfy: NtfyConfig
    download_settings: dict = field(default_factory=dict)
    title_overrides: dict = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
