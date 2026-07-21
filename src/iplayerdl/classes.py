from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore


@dataclass
class Task:
    input_file: Path
    transcode_file: Path
    output_file: Path
    download_slot: BoundedSemaphore | None = None


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
    download_settings: dict
    transcode_settings: TranscodeSettings
    title_overrides: dict
    ntfy: NtfyConfig
