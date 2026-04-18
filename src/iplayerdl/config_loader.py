import tomllib
from pathlib import Path

from dacite import Config as DaciteConfig
from dacite import from_dict

from iplayerdl.classes import Config


def get_config_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "config.toml"


def load_config(config_file: Path | None = None) -> Config:
    dacite_config = DaciteConfig(type_hooks={Path: Path})
    if config_file is None:
        config_file = get_config_path()
    with open(config_file, "rb") as f:
        data = tomllib.load(f)
    return from_dict(data_class=Config, data=data, config=dacite_config)


if __name__ == "__main__":
    print(load_config())
