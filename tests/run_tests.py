"""Dry-run pipeline harness.

For every URL in tests/urls.toml this logs, without downloading anything:
- yt-dlp entry metadata (title, extractor, duration, formats)
- the download path yt-dlp would write
- the metadata match (Sonarr/Radarr/TMDb) with a stage trace
- transcode + final media paths
- the exact ffmpeg command that the pipeline would run

Usage: uv run python tests/run_tests.py
Output: tests/logs/<timestamp>.log (human readable + JSON at the end)
"""

import datetime
import json
import os
import sys
import tomllib
from pathlib import Path

from iplayerdl.classes import Folders, TranscodeSettings
from iplayerdl.config_loader import apply_environment, load_config
from iplayerdl.download import get_info
from iplayerdl.resolver import resolve_media_name, resolve_movie, resolve_series
from iplayerdl.transcode import get_params

TESTS_DIR = Path(__file__).resolve().parent


def is_cbc(url: str) -> bool:
    return url.startswith("https://gem.cbc.ca")


def plan_download_path(entry: dict, url: str, opts: dict) -> str | None:
    if is_cbc(url):
        series = entry.get("series")
        title = entry.get("title")
        if series == title or series is None:
            if entry.get("release_year") is not None:
                return f"{title} ({entry['release_year']})/{title} ({entry['release_year']}).{entry.get('ext', 'mp4')}"
            return f"{title}/{title}.{entry.get('ext', 'mp4')}"
        return (
            f"{series}/Season {entry.get('season_number', 0):02d}/"
            f"{series} - S{entry.get('season_number', 0):02d}"
            f"E{entry.get('episode_number', 0):02d} - {title}.mp4"
        )
    return f"{entry['title']}.{entry.get('ext', 'mp4')}"


def resolve_entry(entry: dict, url: str, overrides: dict) -> tuple[str | None, list]:
    # Dry run: never mutate Sonarr/Radarr libraries.
    allow_adds = False
    trace: list = []
    title = entry.get("title", "")
    if is_cbc(url):
        series = entry.get("series")
        season = entry.get("season_number")
        ep_title = entry.get("episode_title") or title
        if not series or series == title:
            name = resolve_movie(title, trace, allow_adds)
            if name is None:
                name = resolve_media_name(title, overrides, trace, allow_adds)
            return name, trace
        name = resolve_series(series, int(season or 0), ep_title, trace, allow_adds)
        if name is None:
            name = resolve_movie(title, trace, allow_adds)
        if name is None:
            name = resolve_media_name(title, overrides, trace, allow_adds)
        return name, trace
    return resolve_media_name(title, overrides, trace, allow_adds), trace


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def log_entry(lines: list[str], entry_report: dict) -> None:
    lines.append(f"  [{entry_report['index']}] {entry_report['title']}")
    lines.append(f"      source      : {entry_report['source']}")
    lines.append(f"      extractor   : {entry_report.get('extractor', '?')}")
    lines.append(f"      duration    : {entry_report.get('duration_human')}")
    if entry_report.get("webpage_url"):
        lines.append(f"      page        : {entry_report['webpage_url']}")
    lines.append(f"      dl path     : {entry_report.get('download_path')}")
    for t in entry_report.get("trace", []):
        lines.append(f"      match       : {t}")
    lines.append(f"      resolved    : {entry_report.get('resolved_name')}")
    lines.append(f"      transcode   : {entry_report.get('transcode_path')}")
    lines.append(f"      final       : {entry_report.get('final_path')}")
    cmd = entry_report.get("ffmpeg_cmd")
    if cmd:
        lines.append("      ffmpeg cmd  :")
        for i in range(0, len(cmd), 4):
            lines.append("        " + " ".join(cmd[i : i + 4]))


def main() -> None:
    config = load_config()
    apply_environment(config)
    with open(TESTS_DIR / "urls.toml", "rb") as f:
        test_cfg = tomllib.load(f)

    folders = Folders(
        download_dir=Path("/tmp/opencode/iplayerDL-test/download"),
        media_dir=config.folders.media_dir,
        transcode_dir=Path("/tmp/opencode/iplayerDL-test/transcode"),
    )
    settings: TranscodeSettings = config.transcode_settings
    max_entries = test_cfg.get("max_entries_per_url", 0)
    reports: list[dict] = []
    lines: list[str] = [
        f"iplayerDL dry-run {datetime.datetime.now(tz=datetime.UTC).isoformat(timespec='seconds')}",
        "=" * 70,
    ]

    for url in test_cfg["urls"]:
        opts = dict(config.download_settings)
        if is_cbc(url):
            opts["username"] = os.getenv("CBC_EMAIL")
            opts["password"] = os.getenv("CBC_PASSWORD")
        lines.append("")
        lines.append(f"URL: {url}")
        lines.append("-" * 70)
        info = get_info(url, opts)
        if info is None:
            lines.append("  extract_info returned nothing")
            continue
        entries = info.get("entries") or [info]
        entries = [e for e in entries if e]
        if max_entries:
            entries = entries[:max_entries]
        pending = [e for e in entries if not e.get("formats")]
        flat: list[dict] = []
        for e in entries:
            if e.get("formats"):
                flat.append(e)
        for e in pending:
            sub = get_info(e["webpage_url"], opts)
            if sub is None:
                continue
            flat.extend(x for x in (sub.get("entries") or [sub]) if x)
        if max_entries:
            flat = flat[:max_entries]

        for idx, entry in enumerate(flat, start=1):
            if is_cbc(url) and entry.get("title") == "Trailer":
                lines.append("  [skip] CBC Trailer entry")
                continue
            rel_path = plan_download_path(entry, url, opts)
            title = entry.get("title", "")
            resolved, trace = resolve_entry(entry, url, config.title_overrides)
            report = {
                "url": url,
                "index": idx,
                "source": "cbc" if is_cbc(url) else "bbc",
                "extractor": entry.get("extractor_key", ""),
                "title": title,
                "id": entry.get("id"),
                "webpage_url": entry.get("webpage_url"),
                "duration": entry.get("duration"),
                "duration_human": format_duration(entry.get("duration")),
                "n_formats": len(entry.get("formats") or []),
                "download_path": str(folders.download_dir / rel_path)
                if rel_path
                else None,
                "resolved_name": resolved,
                "transcode_path": (
                    str(folders.transcode_dir / f"{resolved}.mp4") if resolved else None
                ),
                "final_path": (
                    str(folders.media_dir / f"{resolved}.mp4") if resolved else None
                ),
                "trace": trace,
            }
            try:
                fake_input = Path(report["download_path"] or "input.mp4")
                cmd = get_params(
                    settings, fake_input, Path(report["transcode_path"] or "out.mp4")
                )
                report["ffmpeg_cmd"] = cmd
            except Exception as e:  # noqa: BLE001
                report["ffmpeg_cmd"] = None
                report["ffmpeg_error"] = f"{type(e).__name__}: {e}"
            reports.append(report)
            log_entry(lines, report)

    matched = sum(1 for r in reports if r["resolved_name"])
    lines.append("")
    lines.append("=" * 70)
    lines.append(
        f"SUMMARY: {matched}/{len(reports)} entries resolved across "
        f"{len(test_cfg['urls'])} urls"
    )

    print("\n".join(lines))
    stamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d-%H%M%S")
    log_file = TESTS_DIR / "logs" / f"{stamp}.log"
    log_file.write_text(
        "\n".join(lines) + "\n\nJSON:\n" + json.dumps(reports, indent=2)
    )
    print(f"\nLog written to {log_file}")


if __name__ == "__main__":
    sys.exit(main())
