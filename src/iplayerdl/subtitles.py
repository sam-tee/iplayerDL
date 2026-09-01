import logging
from pathlib import Path

import pysubs2

from iplayerdl.file_move import move_file

logger = logging.getLogger(__name__)


def convert_file(input_file: Path, output_file: Path):
    subs = pysubs2.load(str(input_file))
    temp_dir = input_file.parent / ".iplayerdl-subtitles"
    temp_dir.mkdir(exist_ok=True)
    try:
        converted_file = temp_dir / f"{input_file.name}.converted.srt"
        subs.save(str(converted_file))
        move_file(converted_file, output_file)
        logger.info("Converted: %s -> %s", input_file.name, output_file.name)
        input_file.unlink()
    finally:
        # Remove the staging dir if we emptied it (ignore stale files).
        try:
            temp_dir.rmdir()
        except OSError:
            pass
