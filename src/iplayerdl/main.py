import queue
import threading

import dotenv

from iplayerdl.config_loader import load_config
from iplayerdl.download import download_url
from iplayerdl.fix_perms import fix_perms
from iplayerdl.transcode import transcode_worker


def cli():
    dotenv.load_dotenv()
    config = load_config()
    if (
        config.pipeline.max_non_transcoded is not None
        and config.pipeline.max_non_transcoded < 1
    ):
        raise ValueError("pipeline.max_non_transcoded must be at least 1")
    download_slots = (
        threading.BoundedSemaphore(config.pipeline.max_non_transcoded)
        if config.pipeline.max_non_transcoded is not None
        else None
    )
    task_queue = queue.Queue()
    t = threading.Thread(
        target=transcode_worker,
        args=(task_queue, config.transcode_settings, config.pipeline),
        daemon=True,
    )
    t.start()
    for url in config.urls:
        download_url(
            q=task_queue,
            url=url,
            opts=config.download_settings,
            folders=config.folders,
            overrides=config.title_overrides,
            download_slots=download_slots,
        )
    task_queue.join()
    if config.permission_settings.enable:
        fix_perms(config.folders.media_dir, config.permission_settings)


if __name__ == "__main__":
    cli()
