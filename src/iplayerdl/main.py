import queue
import threading

import dotenv

from iplayerdl.config_loader import load_config
from iplayerdl.download import download_url
from iplayerdl.fix_perms import fix_perms
from iplayerdl.transcode import transcode_worker


def main():
    dotenv.load_dotenv()
    config = load_config()
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
        )
    task_queue.join()
    if config.permission_settings.enable:
        fix_perms(config.folders.media_dir, config.permission_settings)


if __name__ == "__main__":
    main()
