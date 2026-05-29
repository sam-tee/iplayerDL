import json
import re
import shutil
import subprocess
from pathlib import Path
from queue import Queue

from iplayerdl.classes import Pipeline, Task, TranscodeSettings


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
        str(file_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def detect_crop_at_timestamp(file_path: Path, timestamp: float) -> str | None:
    """Run ffmpeg cropdetect filter for a single frame at a specific time."""
    cmd: list[str] = [
        "ffmpeg",
        "-ss",
        str(timestamp),
        "-i",
        str(file_path),
        "-frames:v",
        "20",
        "-vf",
        "cropdetect=24:16:0",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    matches = re.findall(r"crop=([0-9]+:[0-9]+:[0-9]+:[0-9]+)", result.stderr)
    return matches[-1] if matches else None


def get_crop_region(file_path: Path, samples: int = 20) -> str | None:
    """
    Samples the video at multiple points to detect the optimal crop region.
    Returns the crop string (e.g., '1920:800:0:140') or None if detection fails.
    """
    try:
        duration = get_video_duration(file_path)
        timestamps = [(duration / (samples + 1)) * i for i in range(1, samples + 1)]
        crops: list[str] = []
        for ts in timestamps:
            crop = detect_crop_at_timestamp(file_path, ts)
            if crop:
                crops.append(crop)
        return max(set(crops), key=crops.count)

    except Exception:
        return None


def get_accel_params(settings: TranscodeSettings) -> list[str]:
    if settings.encoder == "qsv":
        return [
            "-init_hw_device",
            "qsv=hw",
            "-filter_hw_device",
            "hw",
            "-hwaccel",
            "qsv",
        ]
    elif settings.encoder == "vaapi":
        return [
            "-hwaccel",
            "vaapi",
            "-hwaccel_device",
            str(settings.device),
            "-hwaccel_output_format",
            "vaapi",
        ]
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
        return ["-vf", f"procamp_vaapi,crop={crop_val}"]
    else:
        return ["-vf", f"crop={crop_val}"]


def transcode(task: Task, settings: TranscodeSettings) -> int:
    base_command: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats",
    ]
    pre_input = get_accel_params(settings)
    file_input = ["-i", str(task.input_file)]
    crop_settings = get_crop_params(settings.crop, settings.encoder, task.input_file)
    if settings.encoder == "qsv":
        encoder = "av1_qsv"
    elif settings.encoder == "vaapi":
        encoder = "h264_vaapi"
    else:
        encoder = "libx264"
    video_params = [
        "-c:v",
        encoder,
        "-global_quality",
        str(settings.quality),
        "-preset",
        "slow",
        "-look_ahead",
        "1",
        "-r",
        "30",
        "-c:a",
        "copy",
        str(task.transcode_file.resolve()),
        "-y",
    ]
    task.transcode_file.parent.mkdir(exist_ok=True, parents=True)
    cmd = base_command + pre_input + file_input + crop_settings + video_params
    try:
        subprocess.run(cmd, capture_output=True, check=True, text=True)
        print(f"\033[32m[ffmpeg]\033[0m Transcoded: {task.transcode_file.name}")
        return 0
    except subprocess.CalledProcessError as e:
        task.transcode_file.unlink(missing_ok=True)
        print(f"\033[32m[ffmpeg]\033[0m \033[31mError: {e}\033[0m")
        return 1


def mimic_transcode(task: Task):
    """
    Runs when transcode is set to false and just copies the input file to transcode folder
    """
    shutil.copy(task.input_file, task.transcode_file)
    print(
        f"\033[32m[ffmpeg]\033[0m Transcode is False. Copied {task.input_file} instead"
    )
    return 0


def move(task: Task, pipeline: Pipeline):
    task.output_file.parent.mkdir(exist_ok=True, parents=True)
    shutil.move(task.transcode_file.resolve(), task.output_file.resolve())
    print(f"\033[36m[sorter]\033[0m Moved: {task.transcode_file} -> {task.output_file}")
    if pipeline.delete_downloads:
        task.input_file.unlink(missing_ok=True)
        print(f"\033[36m[sorter]\033[0m Deleted Download: {task.input_file}")


def transcode_worker(q: Queue, settings: TranscodeSettings, pipeline: Pipeline):
    while True:
        task: Task = q.get()
        if task is None:
            break
        if pipeline.transcode:
            transcode_status = transcode(task, settings)
        else:
            transcode_status = mimic_transcode(task)
        if transcode_status == 0:
            move(task, pipeline)
        q.task_done()


if __name__ == "__main__":
    vids = []
    for f in Path("/home/sam/hardDrive/media/dvd").rglob("*"):
        if f.suffix in [".mkv", ".mp4"]:
            test = f.relative_to("/home/sam/hardDrive/media/dvd")
            task = Task(
                input_file=f,
                transcode_file="/home/sam/hardDrive/media/film" / test,
                output_file=f,
            )
            sett = TranscodeSettings("", 20, "qsv", True)
            transcode(task, sett)
            f.unlink()
