import queue
import threading

from iplayerdl.config_loader import load_config
from iplayerdl.download import download_url
from iplayerdl.transcode import transcode_worker


def main():
    config = load_config()
    task_queue = queue.Queue()
    t = threading.Thread(
        target=transcode_worker,
        args=(task_queue, config.transcode_settings),
        daemon=True,
    )
    t.start()
    for url in config.urls:
        download_url(
            q=task_queue,
            url=url,
            opts=config.download_settings,
            folders=config.folders,
        )
    task_queue.join()


if __name__ == "__main__":
    main()
