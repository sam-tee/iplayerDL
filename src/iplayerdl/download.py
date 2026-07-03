import os
from pathlib import Path
from queue import Queue

import yt_dlp

from iplayerdl.classes import Folders, Task
from iplayerdl.info import get_media_name
from iplayerdl.subtitles import convert_file


def get_info(url: str, opts: dict | None = None) -> dict:
    """Downloads information from url"""
    if opts is None:
        opts = {}
    opts["quiet"] = True
    print(f"\033[34m[yt-dlp]\033[0m Downloading info for: {url}")
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def post_download(q: Queue, folders: Folders, dl_path: Path, overrides: dict):
    title = dl_path.stem
    print(f"\033[34m[yt-dlp]\033[0m Finished Download: {title}")
    media_name = get_media_name(title, overrides)
    if media_name is None:
        print(f"\033[31mError: No matching TMDb entry found for {title}\033[0m")
        return
    sub_paths = list(dl_path.parent.glob(f"{title}.*.*"))
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
        )
    )


def download_cbc(info: dict, opts: dict, q: Queue, folders: Folders):
    opts["username"] = os.getenv("CBC_EMAIL")
    opts["password"] = os.getenv("CBC_PASSWORD")
    if info["title"] == info["episode"]:
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
            "%(series)s (%(release_year)s)/Season %(season_number)02d/%(series)s (%(release_year)s) - S%(season_number)02dE%(episode_number)02d - %(title)s.%(ext)s"
        )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([info["webpage_url"]])
        dl_path = Path(ydl.prepare_filename(info)).resolve()
        media_path = dl_path.relative_to(folders.download_dir.resolve())
        q.put(
            Task(
                input_file=dl_path,
                transcode_file=folders.transcode_dir / media_type / media_path,
                output_file=folders.media_dir / media_type / media_path,
            )
        )


def download_generic(
    entry: dict, opts: dict, q: Queue, folders: Folders, overrides: dict
):
    opts["outtmpl"] = "%(title)s.%(ext)s"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([entry["webpage_url"]])
        dl_path = Path(ydl.prepare_filename(entry)).resolve()
        post_download(q, folders, dl_path, overrides)


def download_url(
    q: Queue, url: str, opts: dict | None, folders: Folders, overrides: dict
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
        if str(entry["webpage_url"]).startswith("https://gem.cbc.ca"):
            download_cbc(entry, opts, q, folders)
        else:
            download_generic(entry, opts, q, folders, overrides)
