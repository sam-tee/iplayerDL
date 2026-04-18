from pathlib import Path
from queue import Queue

import yt_dlp

from iplayerdl.classes import Folders, Task
from iplayerdl.info import get_media_name
from iplayerdl.subtitles import convert_file


def get_info(url) -> dict:
    """Downloads information from url"""
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        return ydl.extract_info(url, download=False)


def get_hook(q: Queue, folders: Folders):
    def download_hook(d):
        if d["status"] == "finished":
            file = Path(d["filename"])
            print(f"\033[34m[yt-dlp]\033[0m Finished Download: {file.name}")
            title = file.name.split(".")[0]
            media_name = get_media_name(title)
            if file.suffix == ".ttml":
                convert_file(file, folders.media_dir / f"{media_name}.en.srt")
                return
            task = Task(
                input_file=file,
                transcode_file=folders.transcode_dir / f"{media_name}.mp4",
                output_file=folders.media_dir / f"{media_name}.mp4",
            )
            q.put(task)

    return download_hook


def download_url(
    q: Queue,
    url: str,
    opts: dict | None,
    folders: Folders,
):
    if opts is None:
        opts = {}
    opts["paths"] = {"home": str(folders.download_dir.resolve())}
    opts["progress_hooks"] = opts.get("progress_hooks", []) + [get_hook(q, folders)]
    info = get_info(url)
    entries = info.get("entries", [info])
    with yt_dlp.YoutubeDL(opts) as ydl:
        for entry in entries:
            ydl.download([entry["webpage_url"]])
