import argparse
import logging
import queue
import threading
from pathlib import Path

from iplayerdl.classes import Config, DownloadCancelled, Stats
from iplayerdl.config_loader import apply_environment, load_config
from iplayerdl.download import download_url
from iplayerdl.logging_setup import get_log_level, setup_logging
from iplayerdl.ntfy import send_ntfy
from iplayerdl.tracker import tracker
from iplayerdl.transcode import transcode_worker

logger = logging.getLogger(__name__)


def run_pipeline(config: Config, urls: list[str]) -> None:
    tracker.reset(urls)
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
        for url in urls:
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


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="iplayerdl",
        description="yt-dlp/ffmpeg wrapper for BBC iPlayer",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging (overrides config logging.level)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        help="console log level (overrides config logging.level)",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run", help="download and process the given URLs (default)"
    )
    run_parser.add_argument("--config", type=str, help="path to config.toml")
    run_parser.add_argument(
        "urls",
        nargs="*",
        help="episode/series URLs to process (queue is ephemeral, never stored)",
    )

    web_parser = subparsers.add_parser(
        "web", help="start the web interface for editing settings and queueing URLs"
    )
    web_parser.add_argument("--config", type=str, help="path to config.toml")
    web_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="bind address (overrides [web] host)",
    )
    web_parser.add_argument(
        "--port", type=int, default=None, help="bind port (overrides [web] port)"
    )

    args = parser.parse_args()
    command = args.command or "run"
    # Early setup so config-load errors are visible; re-applied from config below.
    setup_logging("DEBUG" if args.verbose else (args.log_level or "AUTO"))

    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)
    apply_environment(config)
    if args.verbose:
        setup_logging("DEBUG")
    elif args.log_level:
        setup_logging(args.log_level)
    else:
        setup_logging(get_log_level(config))

    if command == "web":
        from iplayerdl.web import serve

        serve(
            host=args.host or config.web.host,
            port=args.port or config.web.port,
        )
    else:
        if not args.urls:
            parser.error("at least one URL is required (queue is ephemeral, never stored)")
        run_pipeline(config, args.urls)


if __name__ == "__main__":
    cli()
