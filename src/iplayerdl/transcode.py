import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from queue import Queue

from iplayerdl.classes import (
    DownloadCancelled,
    Pipeline,
    Stats,
    Task,
    TranscodeSettings,
)
from iplayerdl.file_move import move_file
from iplayerdl.tracker import tracker

logger = logging.getLogger(__name__)

CROP_SAMPLES = 20
_stats_lock = threading.Lock()


def resolve_path(path: Path) -> Path:
    return path.expanduser().resolve()


def get_video_duration(file_path: Path) -> float:
    """Get the duration of the video in seconds using ffprobe."""
    cmd: list[str] = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(resolve_path(file_path)),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def get_crop_region(file_path: Path) -> str | None:
    """
    Detect the dominant crop region in a single ffmpeg pass.

    Samples ~CROP_SAMPLES frames spread across the video and returns the most
    common crop string (e.g. '1920:800:0:140'), or None if detection fails.
    """
    try:
        duration = get_video_duration(file_path)
        # Sample roughly every duration/CROP_SAMPLES seconds; fps filter is a
        # cheap way to spread samples across the file in one process.
        interval = max(duration / CROP_SAMPLES, 1.0)
        cmd: list[str] = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(resolve_path(file_path)),
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"fps=1/{interval:.3f},cropdetect=24:16:0",
            "-f",
            "null",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        matches = re.findall(r"crop=([0-9]+:[0-9]+:[0-9]+:[0-9]+)", result.stderr)
        if not matches:
            logger.warning("cropdetect found no crop for %s", file_path)
            return None
        return Counter(matches).most_common(1)[0][0]
    except (subprocess.CalledProcessError, ValueError, KeyError, OSError) as e:
        logger.warning(
            "cropdetect failed for %s: %s: %s", file_path, type(e).__name__, e
        )
        return None


def get_accel_params(encoder: str, device: str) -> list[str]:
    if encoder == "qsv":
        return [
            "-init_hw_device",
            "qsv=hw",
            "-filter_hw_device",
            "hw",
            "-hwaccel",
            "qsv",
        ]
    elif encoder == "vaapi":
        return [
            "-hwaccel",
            "vaapi",
            "-hwaccel_device",
            str(device),
            "-hwaccel_output_format",
            "vaapi",
        ]
    elif encoder == "apple":
        return ["-hwaccel", "videotoolbox"]
    else:
        return []


def get_crop_params(crop: bool, encoder: str, file: Path) -> list[str]:
    if not crop:
        return []
    crop_val = get_crop_region(file)
    if crop_val is None:
        return []
    if encoder == "qsv":
        width, height, cx, cy = crop_val.split(":")
        return ["-vf", f"vpp_qsv=cw={width}:ch={height}:cx={cx}:cy={cy}"]
    elif encoder == "vaapi":
        return ["-vf", f"crop_vaapi={crop_val}"]
    else:
        return ["-vf", f"crop={crop_val}"]


def convert_quality(quality: int) -> str:
    scaled = round(100 * (1 - quality / 64))
    return str(scaled)


def get_encoder_params(encoder: str, quality: int) -> list[str]:
    if encoder == "qsv":
        encoder_params = [
            "-c:v",
            "av1_qsv",
            "-global_quality",
            str(quality),
            "-look_ahead",
            "1",
        ]
    elif encoder == "vaapi":
        encoder_params = [
            "-c:v",
            "h264_vaapi",
            "-global_quality",
            str(quality),
        ]
    elif encoder == "apple":
        encoder_params = [
            "-c:v",
            "h264_videotoolbox",
            "-q:v",
            convert_quality(quality),
        ]
    else:
        encoder_params = [
            "-c:v",
            "libsvtav1",
            "-global_quality",
            str(quality),
        ]
    return encoder_params


def get_params(settings: TranscodeSettings, file: Path, output_file: Path) -> list[str]:
    """
    Collects all parameters from given settings
    """
    base_command: list[str] = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats",
    ]
    pre_input = get_accel_params(settings.encoder, settings.device)
    file_input = ["-i", str(resolve_path(file))]
    crop_settings = get_crop_params(settings.crop, settings.encoder, file)
    encoder_settings = get_encoder_params(settings.encoder, settings.quality)
    output_params = [
        "-map",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-c:s",
        "copy",
        str(resolve_path(output_file)),
    ]
    cmd = (
        base_command
        + pre_input
        + file_input
        + crop_settings
        + encoder_settings
        + output_params
    )
    return cmd


def transcode(
    task: Task,
    settings: TranscodeSettings,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    if not task.input_file.exists():
        return 1
    transcode_file = resolve_path(task.transcode_file)
    transcode_file.parent.mkdir(exist_ok=True, parents=True)
    cmd = get_params(settings, task.input_file, transcode_file)
    try:
        with tempfile.TemporaryFile(mode="w+") as errf:
            proc = subprocess.Popen(cmd, stdout=errf, stderr=errf)
            while True:
                try:
                    returncode = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if cancelled is not None and cancelled():
                        logger.info("Transcode cancelled: %s", transcode_file.name)
                        proc.kill()
                        proc.wait()
                        transcode_file.unlink(missing_ok=True)
                        raise DownloadCancelled(task.url or "") from None
            if returncode == 0:
                logger.info("Transcoded: %s", transcode_file.name)
                return 0
            transcode_file.unlink(missing_ok=True)
            errf.seek(0)
            stderr_tail = (errf.read().strip().splitlines() or [str(returncode)])[-1]
            logger.error("Error transcoding %s: %s", transcode_file.name, stderr_tail)
            return 1
    except OSError as e:
        transcode_file.unlink(missing_ok=True)
        logger.error(
            "Error transcoding %s: %s: %s", transcode_file.name, type(e).__name__, e
        )
        return 1


def mimic_transcode(task: Task):
    """
    Runs when transcode is set to false; links (or copies) the input file to
    the transcode folder instead of re-encoding it.
    """
    input_file = resolve_path(task.input_file)
    if not input_file.exists():
        return 1
    transcode_file = resolve_path(task.transcode_file)
    transcode_file.parent.mkdir(exist_ok=True, parents=True)
    try:
        os.link(input_file, transcode_file)
        logger.info("Transcode is False. Linked %s instead", task.input_file)
    except OSError as e:
        logger.debug("Hard link failed for %s (%s: %s), falling back to copy", task.input_file, type(e).__name__, e)
        try:
            shutil.copy2(input_file, transcode_file)
        except OSError as ce:
            logger.error("Copy failed for %s: %s: %s", task.input_file, type(ce).__name__, ce)
            return 1
        logger.info("Transcode is False. Copied %s instead", task.input_file)
    return 0


def move(task: Task, pipeline: Pipeline):
    try:
        move_file(task.transcode_file, task.output_file)
    except Exception:
        logger.exception("Move failed: %s -> %s", task.transcode_file, task.output_file)
        raise
    logger.info("Moved: %s -> %s", task.transcode_file, task.output_file)
    if pipeline.delete_downloads:
        task.input_file.unlink(missing_ok=True)
        logger.info("Deleted Download: %s", task.input_file)


def transcode_worker(
    q: Queue, settings: TranscodeSettings, pipeline: Pipeline, stats: Stats
):
    while True:
        task: Task = q.get()
        if task is None:
            q.task_done()
            break
        if task.url and tracker.cancelled(task.url):
            logger.info("Skipping cancelled task: %s", task.input_file.name)
            if task.download_slot is not None:
                task.download_slot.release()
            q.task_done()
            continue
        if task.url:
            tracker.transcoding(task.url)
        try:
            try:
                if pipeline.transcode:
                    transcode_status = transcode(
                        task,
                        settings,
                        lambda url=task.url: url is not None and tracker.cancelled(url),
                    )
                else:
                    transcode_status = mimic_transcode(task)
                if transcode_status == 0:
                    try:
                        move(task, pipeline)
                    except Exception:
                        # move failed — clean up transcode_file to avoid disk leak
                        try:
                            Path(task.transcode_file).unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise
                    with stats._lock:
                        stats.completed += 1
                    tracker.completed_task(task.url, ok=True)
                else:
                    with stats._lock:
                        stats.failed += 1
                    if not task.input_file.exists():
                        logger.warning("Skipped missing input: %s", task.input_file)
                    else:
                        logger.warning("Transcode failed for: %s", task.input_file)
                    tracker.completed_task(task.url, ok=False)
            except DownloadCancelled:
                pass  # status already set by Tracker.cancel()
            except Exception:  # worker must survive any task failure
                with stats._lock:
                    stats.failed += 1
                logger.exception("Task failed for %s", task.input_file)
                tracker.completed_task(task.url, ok=False)
        finally:
            if task.download_slot is not None:
                task.download_slot.release()
            q.task_done()


if __name__ == "__main__":
    task = Task(
        input_file=Path("~/temp/The Nice Guys.mp4"),
        output_file=Path("~/temp/transcoded/The Nice Guys.mp4"),
        transcode_file=Path("~/temp/temp/temp.mp4"),
    )
    sett = TranscodeSettings("", 20, "apple", True)
    print(" ".join(get_params(sett, task.input_file, task.transcode_file)))
    transcode(task, sett)
