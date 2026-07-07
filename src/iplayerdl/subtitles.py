from pathlib import Path

import pysubs2

from iplayerdl.file_move import move_file


def convert_file(input_file: Path, output_file: Path):
    subs = pysubs2.load(str(input_file))
    converted_file = input_file.with_name(f"{input_file.name}.converted.srt")
    subs.save(str(converted_file))
    move_file(converted_file, output_file)
    print(f"\033[36m[sorter]\033[0m Converted: {input_file.name} -> {output_file.name}")
    input_file.unlink()
