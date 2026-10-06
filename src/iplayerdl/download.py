import glob as globmod
import logging
import os
import time
from pathlib import Path
from queue import Queue
from threading import BoundedSemaphore

import yt_dlp

from iplayerdl.classes import DownloadCancelled, Folders, Stats, Task, TmdbPin
from iplayerdl.resolver import resolve_media_name
from iplayerdl.subtitles import convert_file
from iplayerdl.tracker import tracker

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m2ts", ".mpg"}


def _progress_hook(url: str):
    def hook(d: dict) -> None:
        if tracker.cancelled(url):
            logger.info("Download cancelled: %s", url)
            raise DownloadCancelled(url)
        if d.get("status") == "downloading":
            tracker.downloading(
                url,
                d.get("downloaded_bytes"),
                d.get("total_bytes") or d.get("total_bytes_estimate"),
            )
        elif d.get("status") == "finished":
            tracker.downloading(url, 1, 1)

    return hook


def is_subtitles_only(opts: dict) -> bool:
    """True when yt-dlp is configured to skip media but still write subs."""
    return bool(opts.get("skip_download"))


def find_subtitle_files(
    directory: Path,
    stem: str,
    min_mtime: float | None = None,
) -> list[Path]:
    """Locate freshly written subtitle files sharing ``stem``.

    Mirrors the ``title.*.*`` glob used by :func:`post_download` but filters
    by modification time so a retry never picks up stale files from a
    previous run.
    """
    candidates = [
        path
        for path in directory.glob(f"{globmod.escape(stem)}.*.*")
        if not path.name.endswith(".converted.srt")
        and path.suffix.lower() not in VIDEO_EXTS | {".part", ".ytdl"}
    ]
    if min_mtime is not None:
        fresh = [p for p in candidates if p.stat().st_mtime >= min_mtime - 1.0]
        return sorted(fresh, key=lambda p: p.stat().st_mtime)
    return sorted(candidates, key=lambda p: p.stat().st_mtime)


def convert_first_subtitle(sub_paths: list[Path], dest: Path) -> bool:
    """Convert the first usable subtitle to ``dest``; True on success."""
    converted = False
    for file in sub_paths:
        if converted:
            logger.warning("Skipping extra subtitle %s (already have %s)", file.name, dest.name)
            continue
        try:
            convert_file(file, dest)
            converted = True
        except Exception as e:  # noqa: BLE001 - one bad subtitle must not kill the pipeline
            logger.error(
                "Subtitle conversion failed for %s: %s: %s",
                file.name,
                type(e).__name__,
                e,
            )
    return converted


def _cleanup_extra_subtitles(sub_paths: list[Path], dest_name: str) -> None:
    """Remove leftover subtitle files after a successful conversion."""
    for file in sub_paths:
        if not file.exists():
            continue
        try:
            file.unlink()
            logger.debug("Removed extra subtitle %s (kept %s)", file.name, dest_name)
        except OSError as e:
            logger.warning("Could not remove extra subtitle %s: %s", file.name, e)


def acquire_download_slot(download_slots: BoundedSemaphore | None):
    if download_slots is not None:
        logger.debug("Waiting for non-transcoded download slot")
        download_slots.acquire()


def release_download_slot(download_slots: BoundedSemaphore | None):
    if download_slots is not None:
        download_slots.release()


def get_info(url: str, opts: dict | None = None) -> dict | None:
    """Downloads information from url (does not mutate the caller's opts).

    ``check_formats`` is always off here: metadata only needs the format
    list, and verifying every manifest costs ~209s vs ~15s on a 12-episode
    series. Availability is resolved at download time instead.
    """
    opts = dict(opts or {}, quiet=True, check_formats=False)
    logger.info("Downloading info for: %s", url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def find_downloaded_file(
    expected: Path,
    preferred_ext: str | None = None,
    min_mtime: float | None = None,
) -> Path | None:
    """
    Locate the actual downloaded media file.

    yt-dlp's prepare_filename() predicts the output path before format
    merging, so the real file can have a different container extension.
    Prefer the expected path; otherwise look for a video file with the same
    stem.
    """
    if expected.exists():
        return expected.resolve()
    candidates = [
        p
        for p in expected.parent.glob(f"{globmod.escape(expected.stem)}.*")
        if p.suffix.lower() in VIDEO_EXTS and not p.name.endswith(".part")
    ]
    if not candidates:
        return None
    # Prefer file matching the entry's actual extension if known.
    if preferred_ext:
        ext = f".{preferred_ext.lstrip('.').lower()}"
        ext_matches = [p for p in candidates if p.suffix.lower() == ext]
        if ext_matches:
            candidates = ext_matches
    # Exclude stale files left from a previous run by requiring mtime
    # to be at or after the download slot acquisition time.
    if min_mtime is not None:
        fresh = [p for p in candidates if p.stat().st_mtime >= min_mtime - 1.0]
        if not fresh:
            return None
        candidates = fresh
    return max(candidates, key=lambda p: p.stat().st_mtime).resolve()


def post_download(
    q: Queue,
    folders: Folders,
    dl_path: Path,
    overrides: dict,
    stats: Stats,
    download_slot: BoundedSemaphore | None = None,
    allow_adds: bool = False,
    url: str | None = None,
    pin: TmdbPin | None = None,
) -> bool:
    title = dl_path.stem
    logger.info("Finished Download: %s", title)
    media_name = resolve_media_name(title, overrides, allow_adds=allow_adds, pin=pin)
    if media_name is None:
        with stats._lock:
            stats.unresolved += 1
        tracker.unresolved(url or "", title)
        logger.error("No match found in Sonarr/Radarr/TMDb for %s", title)
        return False
    with stats._lock:
        stats.resolved += 1
    # The UI labels this item with the matched media rather than the URL.
    tracker.resolved(url or "", media_name)
    sub_paths = [
        path
        for path in dl_path.parent.glob(f"{globmod.escape(title)}.*.*")
        if not path.name.endswith(".converted.srt")
        and path != dl_path
        and path.suffix.lower() not in VIDEO_EXTS | {".part", ".ytdl"}
    ]
    dest = folders.media_dir / f"{media_name}.en.srt"
    convert_first_subtitle(sub_paths, dest)
    q.put(
        Task(
            input_file=dl_path,
            transcode_file=folders.transcode_dir / f"{media_name}.mp4",
            output_file=folders.media_dir / f"{media_name}.mp4",
            download_slot=download_slot,
            url=url,
        )
    )
    return True


def post_download_subtitles_only(
    folders: Folders,
    expected: Path,
    overrides: dict,
    stats: Stats,
    opts: dict,
    allow_adds: bool = False,
    url: str | None = None,
    pin: TmdbPin | None = None,
    min_mtime: float | None = None,
) -> None:
    """Resolve, convert and file subtitles when media download was skipped.

    Used when ``skip_download`` is set: there is no video to transcode, so
    the subtitle is converted straight to ``media_dir`` and the item is
    marked completed synchronously (no transcode task is queued).
    """
    title = expected.stem
    logger.info("Finished subtitles-only download: %s", title)
    if not opts.get("writesubtitles") and not opts.get("writeautomaticsubs"):
        logger.error(
            "skip_download is set but neither writesubtitles nor "
            "writeautomaticsubs is enabled for %s",
            title,
        )
        with stats._lock:
            stats.failed += 1
        tracker.completed_task(url or "", ok=False)
        return
    sub_paths = find_subtitle_files(expected.parent, title, min_mtime=min_mtime)
    if not sub_paths:
        logger.error("No subtitles downloaded for %s", expected)
        with stats._lock:
            stats.failed += 1
        tracker.completed_task(url or "", ok=False)
        return
    media_name = resolve_media_name(title, overrides, allow_adds=allow_adds, pin=pin)
    if media_name is None:
        with stats._lock:
            stats.unresolved += 1
        tracker.unresolved(url or "", title)
        logger.error("No match found in Sonarr/Radarr/TMDb for %s", title)
        return
    with stats._lock:
        stats.resolved += 1
    tracker.resolved(url or "", media_name)
    dest = folders.media_dir / f"{media_name}.en.srt"
    if not convert_first_subtitle(sub_paths, dest):
        with stats._lock:
            stats.failed += 1
        tracker.completed_task(url or "", ok=False)
        return
    _cleanup_extra_subtitles(sub_paths, dest.name)
    with stats._lock:
        stats.completed += 1
    tracker.completed_task(url or "", ok=True)


def post_download_cbc_subtitles_only(
    info: dict,
    expected: Path,
    folders: Folders,
    media_type: str,
    stats: Stats,
    opts: dict,
    progress_url: str | None = None,
    min_mtime: float | None = None,
) -> None:
    """CBC equivalent of :func:`post_download_subtitles_only`."""
    title = expected.stem
    logger.info("Finished subtitles-only download: %s", title)
    if not opts.get("writesubtitles") and not opts.get("writeautomaticsubs"):
        logger.error(
            "skip_download is set but neither writesubtitles nor "
            "writeautomaticsubs is enabled for %s",
            title,
        )
        with stats._lock:
            stats.failed += 1
        tracker.completed_task(progress_url or info.get("webpage_url") or "", ok=False)
        return
    sub_paths = find_subtitle_files(expected.parent, title, min_mtime=min_mtime)
    if not sub_paths:
        logger.error("No subtitles downloaded for %s", expected)
        with stats._lock:
            stats.failed += 1
        tracker.completed_task(progress_url or info.get("webpage_url") or "", ok=False)
        return
    try:
        media_path = expected.relative_to(folders.download_dir.resolve())
    except ValueError:
        media_path = Path(expected.name)
    url = progress_url or info.get("webpage_url") or ""
    tracker.resolved(url, f"{media_type}/{media_path.with_suffix('')}")
    dest = folders.media_dir / media_type / media_path.with_suffix(".en.srt")
    if not convert_first_subtitle(sub_paths, dest):
        with stats._lock:
            stats.failed += 1
        tracker.completed_task(url, ok=False)
        return
    _cleanup_extra_subtitles(sub_paths, dest.name)
    with stats._lock:
        stats.resolved += 1
        stats.completed += 1
    tracker.completed_task(url, ok=True)


def download_cbc(
    info: dict,
    opts: dict,
    q: Queue,
    folders: Folders,
    stats: Stats,
    download_slots: BoundedSemaphore | None = None,
    progress_url: str | None = None,
    pin: TmdbPin | None = None,
) -> None:
    opts = dict(opts)
    if pin is not None:
        # CBC paths come straight from CBC metadata, not the TMDb/Sonarr
        # resolver chain, so a pinned TMDb id can never apply. Warn rather
        # than silently ignoring it.
        logger.warning(
            "Ignoring metadata override %s:%s for CBC item %s: "
            "CBC paths use CBC metadata directly",
            pin.kind,
            pin.id,
            info.get("webpage_url") or info.get("title"),
        )
    if info["title"] == "Trailer":
        with stats._lock:
            stats.skipped += 1
        return
    username = os.getenv("CBC_EMAIL")
    password = os.getenv("CBC_PASSWORD")
    if username and password:
        opts.setdefault("username", username)
        opts.setdefault("password", password)
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
    start_mtime = time.time()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Reuse the already-extracted entry instead of re-extracting by
            # URL: saves a full extra info round-trip per episode. The entry
            # already carries requested formats/subtitles from get_info,
            # which used the same format/subtitle opts.
            ydl.process_ie_result(dict(info), download=True)
            expected = Path(ydl.prepare_filename(info))
            if is_subtitles_only(opts):
                post_download_cbc_subtitles_only(
                    info,
                    expected,
                    folders,
                    media_type,
                    stats,
                    opts,
                    progress_url,
                    min_mtime=start_mtime,
                )
                return
            dl_path = find_downloaded_file(
                expected,
                preferred_ext=info.get("ext"),
                min_mtime=start_mtime,
            )
            if dl_path is None:
                logger.error("Could not locate downloaded file for %s", expected)
                return
            try:
                media_path = dl_path.relative_to(folders.download_dir.resolve())
            except ValueError:
                media_path = Path(dl_path.name)
            # Label the item with the matched media rather than the URL, same
            # as the BBC path does via post_download.
            tracker.resolved(
                progress_url or info["webpage_url"],
                f"{media_type}/{media_path.with_suffix('')}",
            )
            q.put(
                Task(
                    input_file=dl_path,
                    transcode_file=folders.transcode_dir / media_type / media_path,
                    output_file=folders.media_dir / media_type / media_path,
                    download_slot=download_slots,
                    url=progress_url or info["webpage_url"],
                )
            )
            with stats._lock:
                stats.resolved += 1
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
    stats: Stats,
    download_slots: BoundedSemaphore | None = None,
    allow_adds: bool = False,
    progress_url: str | None = None,
    pin: TmdbPin | None = None,
) -> None:
    opts = dict(opts)
    opts["outtmpl"] = "%(title)s.%(ext)s"
    queued = False
    acquire_download_slot(download_slots)
    start_mtime = time.time()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # See download_cbc: process the extracted entry in place rather
            # than downloading by URL and paying for a second extraction.
            ydl.process_ie_result(dict(entry), download=True)
            expected = Path(ydl.prepare_filename(entry))
            if is_subtitles_only(opts):
                post_download_subtitles_only(
                    folders,
                    expected,
                    overrides,
                    stats,
                    opts,
                    allow_adds,
                    progress_url,
                    pin,
                    min_mtime=start_mtime,
                )
                return
            dl_path = find_downloaded_file(
                expected,
                preferred_ext=entry.get("ext"),
                min_mtime=start_mtime,
            )
            if dl_path is None:
                logger.error("Could not locate downloaded file for %s", expected)
                return
            queued = post_download(
                q,
                folders,
                dl_path,
                overrides,
                stats,
                download_slots,
                allow_adds,
                progress_url,
                pin,
            )
    finally:
        if not queued:
            release_download_slot(download_slots)


def download(
    entry: dict,
    opts: dict,
    q: Queue,
    folders: Folders,
    overrides: dict,
    stats: Stats,
    download_slots: BoundedSemaphore | None = None,
    allow_adds: bool = False,
    progress_url: str | None = None,
    pin: TmdbPin | None = None,
) -> None:
    opts = dict(opts)
    if str(entry["webpage_url"]).startswith("https://gem.cbc.ca"):
        download_cbc(entry, opts, q, folders, stats, download_slots, progress_url, pin)
    else:
        download_generic(
            entry,
            opts,
            q,
            folders,
            overrides,
            stats,
            download_slots,
            allow_adds,
            progress_url,
            pin,
        )


_MAX_RECURSION_DEPTH = 5


def download_url(
    q: Queue,
    url: str,
    opts: dict | None,
    folders: Folders,
    overrides: dict,
    stats: Stats,
    download_slots: BoundedSemaphore | None = None,
    allow_adds: bool = False,
    progress_url: str | None = None,
    pin: TmdbPin | None = None,
    _depth: int = 0,
    _visited: set[str] | None = None,
):
    if progress_url is None:
        progress_url = url
    if tracker.cancelled(progress_url):
        logger.info("Skipping cancelled URL: %s", progress_url)
        return
    if _visited is None:
        _visited = set()
    if url in _visited:
        logger.warning("Skipping already-visited URL (cycle detected): %s", url)
        return
    if _depth > _MAX_RECURSION_DEPTH:
        logger.error(
            "Max recursion depth (%d) exceeded for %s", _MAX_RECURSION_DEPTH, url
        )
        return
    _visited.add(url)
    opts = dict(opts or {})
    if url.startswith("https://gem.cbc.ca"):
        username = os.getenv("CBC_EMAIL")
        password = os.getenv("CBC_PASSWORD")
        if username and password:
            opts["username"] = username
            opts["password"] = password
    opts["paths"] = {"home": str(folders.download_dir.resolve())}
    tracker.resolving(progress_url)
    try:
        info = get_info(url, opts)
        if info is None:
            tracker.unresolved(progress_url)
            return
        entries = [e for e in info.get("entries", [info]) if e]
        is_series = len(entries) > 1
        if is_series:
            logger.info("Series detected: %d episodes for %s", len(entries), url)
            tracker.set_episodes(progress_url, len(entries))
        # progress hook only for download phase — not during get_info
        opts["progress_hooks"] = [_progress_hook(progress_url)]
        for index, entry in enumerate(entries, start=1):
            if is_series:
                tracker.episode_start(progress_url, index)
            if entry.get("formats") is not None:
                download(
                    entry,
                    dict(opts),
                    q,
                    folders,
                    overrides,
                    stats,
                    download_slots,
                    allow_adds,
                    progress_url,
                    pin,
                )
            elif entry.get("webpage_url"):
                next_url = entry["webpage_url"]
                if next_url in _visited:
                    logger.warning(
                        "Skipping already-visited entry URL (cycle detected): %s",
                        next_url,
                    )
                    continue
                download_url(
                    q,
                    next_url,
                    dict(opts),
                    folders,
                    overrides,
                    stats,
                    download_slots,
                    allow_adds,
                    progress_url,
                    pin,
                    _depth + 1,
                    _visited,
                )
            else:
                logger.warning("Skipping entry with no formats or webpage_url: %s", entry.get("id", entry))
    except DownloadCancelled:
        return
