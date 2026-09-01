"""Test the full resolver: Sonarr find-or-add -> episode fuzzy match ->
Radarr -> TMDb fallback, against all title_overrides plus a movie."""

import dotenv

from iplayerdl.config_loader import load_config
from iplayerdl.resolver import resolve_media_name


def main():
    dotenv.load_dotenv()
    config = load_config()
    titles = list(config.title_overrides.keys()) + ["Chicken Run"]
    matched = 0
    for title in titles:
        try:
            result = resolve_media_name(
                title, config.title_overrides, allow_adds=config.pipeline.allow_speculative_adds
            )
        except Exception as e:  # noqa: BLE001
            print(f"ERROR      | {title}\n           -> {type(e).__name__}: {e}")
            continue
        if result is None:
            print(f"NO MATCH   | {title}")
            continue
        matched += 1
        print(f"MATCHED    | {title}\n           -> {result}")
    print(f"\n{matched}/{len(titles)} matched")


if __name__ == "__main__":
    main()
