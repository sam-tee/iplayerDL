import queue
import threading

import dotenv

from iplayerdl.config_loader import load_config
from iplayerdl.download import download_url
from iplayerdl.ntfy import send_ntfy
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
    try:
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
    except Exception as e:
        error_type = type(e).__name__
        error_message = f"Error Type: {error_type}\nError: {e}"

        send_ntfy(
            message=error_message,
            title="Pipeline Failed",
            priority="high",
            tags="x",
            topic=config.ntfy.topic,
            url_base=config.ntfy.url_base,
        )
        raise
    else:
        send_ntfy(
            message="iplayerDL completed successfully",
            title="iplayerDL Success",
            tags="white_check_mark",
            topic=config.ntfy.topic,
            url_base=config.ntfy.url_base,
        )


if __name__ == "__main__":
    cli()
