from pathlib import Path

import pysubs2


def convert_file(input_file: Path, output_file: Path):
    subs = pysubs2.load(str(input_file))
    output_file.parent.mkdir(exist_ok=True, parents=True)
    subs.save(str(output_file))
    print(f"\033[36m[sorter]\033[0m Converted: {input_file.name} -> {output_file.name}")
    input_file.unlink()
