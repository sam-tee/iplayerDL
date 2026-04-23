from pathlib import Path
from queue import Queue

import yt_dlp

from iplayerdl.classes import Folders, Task
from iplayerdl.info import get_media_name
from iplayerdl.subtitles import convert_file


def get_info(url) -> dict:
    """Downloads information from url"""
    print(f"\033[34m[yt-dlp]\033[0m Downloading info for: {url}")
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
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


def download_url(
    q: Queue, url: str, opts: dict | None, folders: Folders, overrides: dict
):
    if opts is None:
        opts = {}
    opts["paths"] = {"home": str(folders.download_dir.resolve())}
    info = get_info(url)
    entries = info.get("entries", [info])
    with yt_dlp.YoutubeDL(opts) as ydl:
        for entry in entries:
            ydl.download([entry["webpage_url"]])
            dl_path = Path(ydl.prepare_filename(entry)).resolve()
            post_download(q, folders, dl_path, overrides)
