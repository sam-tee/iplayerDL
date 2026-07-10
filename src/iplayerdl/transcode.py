import json
import re
import shutil
import subprocess
from pathlib import Path
from queue import Queue

from iplayerdl.classes import Pipeline, Task, TranscodeSettings
from iplayerdl.file_move import move_file


def mkPath(path: Path) -> Path:
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
        str(mkPath(file_path)),
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
        str(mkPath(file_path)),
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
        return ["-vf", f"procamp_vaapi,crop={crop_val}"]
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
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats",
    ]
    pre_input = get_accel_params(settings.encoder, settings.device)
    file_input = ["-i", str(mkPath(file))]
    crop_settings = get_crop_params(settings.crop, settings.encoder, file)
    encoder_settings = get_encoder_params(settings.encoder, settings.quality)
    video_params = [
        "-r",
        "30",
        "-c:a",
        "copy",
        str(mkPath(output_file)),
        "-y",
    ]
    cmd = (
        base_command
        + pre_input
        + file_input
        + crop_settings
        + encoder_settings
        + video_params
    )
    return cmd


def transcode(task: Task, settings: TranscodeSettings) -> int:
    transcode_file = mkPath(task.transcode_file)
    transcode_file.parent.mkdir(exist_ok=True, parents=True)
    cmd = get_params(settings, task.input_file, transcode_file)
    try:
        subprocess.run(cmd, capture_output=True, check=True, text=True)
        print(f"\033[32m[ffmpeg]\033[0m Transcoded: {transcode_file.name}")
        return 0
    except subprocess.CalledProcessError as e:
        transcode_file.unlink(missing_ok=True)
        print(f"\033[32m[ffmpeg]\033[0m \033[31mError: {e}\033[0m")
        return 1


def mimic_transcode(task: Task):
    """
    Runs when transcode is set to false and just copies the input file to transcode folder
    """
    input_file = mkPath(task.input_file)
    transcode_file = mkPath(task.transcode_file)
    transcode_file.parent.mkdir(exist_ok=True,parents=True)
    shutil.copy(input_file, transcode_file)
    print(
        f"\033[32m[ffmpeg]\033[0m Transcode is False. Copied {task.input_file} instead"
    )
    return 0


def move(task: Task, pipeline: Pipeline):
    move_file(task.transcode_file, task.output_file)
    print(f"\033[36m[sorter]\033[0m Moved: {task.transcode_file} -> {task.output_file}")
    if pipeline.delete_downloads:
        task.input_file.unlink(missing_ok=True)
        print(f"\033[36m[sorter]\033[0m Deleted Download: {task.input_file}")


def transcode_worker(q: Queue, settings: TranscodeSettings, pipeline: Pipeline):
    while True:
        task: Task = q.get()
        if task is None:
            break
        try:
            if pipeline.transcode:
                transcode_status = transcode(task, settings)
            else:
                transcode_status = mimic_transcode(task)
            if transcode_status == 0:
                move(task, pipeline)
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
