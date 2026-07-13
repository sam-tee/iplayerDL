import os
from pathlib import Path
from queue import Queue
from threading import BoundedSemaphore

import yt_dlp

from iplayerdl.classes import Folders, Task
from iplayerdl.info import get_media_name
from iplayerdl.subtitles import convert_file


def acquire_download_slot(download_slots: BoundedSemaphore | None):
    if download_slots is not None:
        print("\033[34m[yt-dlp]\033[0m Waiting for non-transcoded download slot")
        download_slots.acquire()


def release_download_slot(download_slots: BoundedSemaphore | None):
    if download_slots is not None:
        download_slots.release()


def get_info(url: str, opts: dict | None = None) -> dict:
    """Downloads information from url"""
    if opts is None:
        opts = {}
    opts["quiet"] = True
    print(f"\033[34m[yt-dlp]\033[0m Downloading info for: {url}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def post_download(
    q: Queue,
    folders: Folders,
    dl_path: Path,
    overrides: dict,
    download_slot: BoundedSemaphore | None = None,
) -> bool:
    title = dl_path.stem
    print(f"\033[34m[yt-dlp]\033[0m Finished Download: {title}")
    media_name = get_media_name(title, overrides)
    if media_name is None:
        print(f"\033[31mError: No matching TMDb entry found for {title}\033[0m")
        return False
    sub_paths = [
        path
        for path in dl_path.parent.glob(f"{title}.*.*")
        if not path.name.endswith(".converted.srt")
    ]
    for file in sub_paths:
        try:
            convert_file(file, folders.media_dir / f"{media_name}.en.srt")
        except KeyError:
            print(f"\033[31mError: Subtitle conversion failed for {file.name}\033[0m")
    q.put(
        Task(
            input_file=dl_path,
            transcode_file=folders.transcode_dir / f"{media_name}.mp4",
            output_file=folders.media_dir / f"{media_name}.mp4",
            download_slot=download_slot,
        )
    )
    return True


def download_cbc(
    info: dict,
    opts: dict,
    q: Queue,
    folders: Folders,
    download_slots: BoundedSemaphore | None = None,
) -> None:
    opts["username"] = os.getenv("CBC_EMAIL")
    opts["password"] = os.getenv("CBC_PASSWORD")
    if info["title"] == "Trailer":
        return
    if info["series"] == info["title"]:
        media_type = "film"
        if info.get("release_year") is not None:
            opts["outtmpl"] = (
                "%(title)s (%(release_year)s)/%(title)s (%(release_year)s).%(ext)s"
            )
        else:
            opts["outtmpl"] = "%(title)s/%(title)s.%(ext)s"
    else:
        media_type = "tv"
        opts["outtmpl"] = (
            "%(series)s/Season %(season_number)02d/%(series)s - S%(season_number)02dE%(episode_number)02d - %(title)s.%(ext)s"
        )
    queued = False
    acquire_download_slot(download_slots)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([info["webpage_url"]])
            dl_path = Path(ydl.prepare_filename(info)).resolve()
            media_path = dl_path.relative_to(folders.download_dir.resolve())
            q.put(
                Task(
                    input_file=dl_path,
                    transcode_file=folders.transcode_dir / media_type / media_path,
                    output_file=folders.media_dir / media_type / media_path,
                    download_slot=download_slots,
                )
            )
            queued = True
    finally:
        if not queued:
            release_download_slot(download_slots)


def download_generic(
    entry: dict,
    opts: dict,
    q: Queue,
    folders: Folders,
    overrides: dict,
    download_slots: BoundedSemaphore | None = None,
) -> None:
    opts["outtmpl"] = "%(title)s.%(ext)s"
    queued = False
    acquire_download_slot(download_slots)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([entry["webpage_url"]])
            dl_path = Path(ydl.prepare_filename(entry)).resolve()
            queued = post_download(q, folders, dl_path, overrides, download_slots)
    finally:
        if not queued:
            release_download_slot(download_slots)


def download(
    entry: dict,
    opts: dict,
    q: Queue,
    folders: Folders,
    overrides: dict,
    download_slots: BoundedSemaphore | None = None,
) -> None:
    if str(entry["webpage_url"]).startswith("https://gem.cbc.ca"):
        download_cbc(entry, opts, q, folders, download_slots)
    else:
        download_generic(entry, opts, q, folders, overrides, download_slots)


def download_url(
    q: Queue,
    url: str,
    opts: dict | None,
    folders: Folders,
    overrides: dict,
    download_slots: BoundedSemaphore | None = None,
):
    if opts is None:
        opts = {}
    if url.startswith("https://gem.cbc.ca"):
        opts["username"] = os.getenv("CBC_EMAIL")
        opts["password"] = os.getenv("CBC_PASSWORD")
    opts["paths"] = {"home": str(folders.download_dir.resolve())}
    info = get_info(url, opts)
    entries = info.get("entries", [info])
    for entry in entries:
        if entry.get("formats") is not None:
            download(entry, opts, q, folders, overrides, download_slots)
        else:
            download_url(
                q, entry["webpage_url"], opts, folders, overrides, download_slots
            )
