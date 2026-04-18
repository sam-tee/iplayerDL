from dataclasses import dataclass
from pathlib import Path


@dataclass
class Task:
    input_file: Path
    transcode_file: Path
    output_file: Path


@dataclass
class Folders:
    download_dir: Path
    media_dir: Path
    transcode_dir: Path


@dataclass
class Pipeline:
    transcode: bool
    delete_downloads: bool


@dataclass
class TranscodeSettings:
    device: str
    quality: int
    type: str
    crop: bool


@dataclass
class Config:
    folders: Folders
    urls: list[str]
    pipeline: Pipeline
    download_settings: dict
    transcode_settings: TranscodeSettings
