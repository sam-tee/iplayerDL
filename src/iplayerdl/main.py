import argparse
import logging
import queue
import sys
import threading
from pathlib import Path

from iplayerdl.classes import Config, DownloadCancelled, Stats
from iplayerdl.config_loader import apply_environment, load_config
from iplayerdl.download import download_url
from iplayerdl.ntfy import send_ntfy
from iplayerdl.tracker import tracker
from iplayerdl.transcode import transcode_worker

logger = logging.getLogger(__name__)


def run_pipeline(config: Config) -> None:
    tracker.reset(config.urls)
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
    stats = Stats()
    t = threading.Thread(
        target=transcode_worker,
        args=(task_queue, config.transcode_settings, config.pipeline, stats),
        daemon=True,
    )
    t.start()
    try:
        for url in config.urls:
            if tracker.cancelled(url):
                continue
            try:
                download_url(
                    q=task_queue,
                    url=url,
                    opts=config.download_settings,
                    folders=config.folders,
                    overrides=config.title_overrides,
                    stats=stats,
                    download_slots=download_slots,
                    allow_adds=config.pipeline.allow_speculative_adds,
                )
            except DownloadCancelled:
                pass
        task_queue.join()
    except Exception as e:
        tracker.fail_pending()
        error_type = type(e).__name__
        error_message = (
            f"Error Type: {error_type}\nError: {e}\n\nProgress before failure:\n"
            f"{stats.summary()}"
        )

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
        # Shut the worker down cleanly now that the queue is drained.
        task_queue.put(None)
        t.join(timeout=30)
        if t.is_alive():
            logger.error("Transcode worker did not terminate within 30s; pipeline may be hung")
            tracker.fail_pending()
            send_ntfy(
                message=f"Pipeline timed out waiting for transcodes\n{stats.summary()}",
                title="Pipeline Failed",
                priority="high",
                tags="x",
                topic=config.ntfy.topic,
                url_base=config.ntfy.url_base,
            )
            return
        send_ntfy(
            message=f"iplayerDL completed successfully\n{stats.summary()}",
            title="iplayerDL Success",
            tags="white_check_mark",
            topic=config.ntfy.topic,
            url_base=config.ntfy.url_base,
        )


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="iplayerdl",
        description="yt-dlp/ffmpeg wrapper for BBC iPlayer",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run", help="process every URL in config.toml (default)"
    )
    run_parser.add_argument("--config", type=str, help="path to config.toml")

    web_parser = subparsers.add_parser(
        "web", help="start the web interface for editing settings and queueing URLs"
    )
    web_parser.add_argument("--config", type=str, help="path to config.toml")
    web_parser.add_argument(
        "--host", type=str, default="127.0.0.1", help="bind address"
    )
    web_parser.add_argument("--port", type=int, default=8080, help="bind port")

    args = parser.parse_args()
    command = args.command or "run"
    setup_logging(args.verbose)

    config = load_config(Path(args.config) if args.config else None)
    apply_environment(config)

    if command == "web":
        from iplayerdl.web import serve

        serve(host=args.host, port=args.port)
    else:
        run_pipeline(config)


if __name__ == "__main__":
    cli()
