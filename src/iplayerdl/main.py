import argparse
import logging
import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from iplayerdl.classes import Config, DownloadCancelled, Stats, TmdbPin
from iplayerdl.config_loader import apply_environment, load_config
from iplayerdl.download import download_url
from iplayerdl.logging_setup import get_log_level, setup_logging
from iplayerdl.ntfy import send_ntfy
from iplayerdl.queue_db import (
    ALL_STATUSES,
    DEFAULT_HISTORY_LIMIT,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_UNRESOLVED,
    QueueItem,
    get_queue_db,
    media_title,
)
from iplayerdl.tracker import tracker
from iplayerdl.transcode import transcode_worker

logger = logging.getLogger(__name__)


class JobOutcome(NamedTuple):
    """How a queue item settled, as read back from the in-memory tracker."""

    status: str
    error: str | None
    detail: str
    resolved: str


def _job_status(url: str, crash: str | None = None) -> JobOutcome:
    """Map a finished tracker entry onto a queue row's final state.

    ``error`` is a diagnosis for the history; ``detail`` is whatever progress
    text the tracker last held ("3/10 MB", "2/3 done"), which is useful on its
    own but is not a reason for the failure. ``resolved`` is the media path
    metadata matched to, empty when nothing matched.

    ``crash`` is the reason the run ended, used for items the tracker never saw
    settle — the pipeline died underneath them, so "pipeline ended while
    'downloading'" would say nothing about why.
    """
    job = next((j for j in tracker.snapshot() if j["url"] == url), None)
    if job is None:
        return JobOutcome(STATUS_FAILED, crash or "no job state recorded", "", "")
    status = job["status"]
    detail = (job.get("detail") or "").strip()
    resolved = (job.get("title") or "").strip()
    if status == "completed":
        return JobOutcome(STATUS_COMPLETED, None, detail, resolved)
    if status == "unresolved":
        return JobOutcome(STATUS_UNRESOLVED, detail or "no metadata match", "", resolved)
    if status == "cancelled":
        return JobOutcome(STATUS_CANCELLED, detail or "cancelled", "", resolved)
    if status == STATUS_FAILED:
        return JobOutcome(STATUS_FAILED, "download or transcode failed", detail, resolved)
    # Still mid-flight when the pipeline ended: the tracker reports the state
    # before transcodes settle, so report that rather than trusting it.
    return JobOutcome(
        STATUS_FAILED, crash or f"pipeline ended while {status!r}", detail, resolved
    )


def _settle_remaining(items: list[QueueItem], crash: str | None = None) -> None:
    """Give any still-active queue item its final status.

    Items the tracker saw settle keep that verdict, so an item that finished
    before a crash is not overwritten by it. Anything still in flight is failed
    with ``crash`` when the caller supplies one.
    """
    db = get_queue_db()
    for item in items:
        # The run is over for this item either way, so its cancellation has
        # been acted on; drop it or a later retry of the same URL in this
        # process would be discarded without being downloaded.
        tracker.forget(item.url)
        current = db.get(item.id)
        if current is None or not current.active:
            # Already settled, e.g. cancelled through the web UI mid-run, which
            # writes the row directly. Record why if nothing said.
            if (
                current is not None
                and current.status == STATUS_CANCELLED
                and not current.error
            ):
                db.finish(item.id, STATUS_CANCELLED, error="cancelled")
            continue
        out = _job_status(item.url, crash)
        db.finish(
            item.id, out.status, error=out.error, detail=out.detail,
            resolved=out.resolved,
        )


def run_pipeline(
    config: Config,
    items: list[QueueItem],
    more: Callable[[], list[QueueItem]] | None = None,
) -> None:
    """Download and process ``items``, then anything ``more`` hands over.

    ``more`` is called once each batch has been downloaded; returning a
    non-empty list continues the *same* session with those items. Keeping them
    in one session is what lets the next item download while the previous one
    is still transcoding, and it keeps one transcode worker and one
    `max_non_transcoded` semaphore for the whole session, so that cap stays a
    limit on the process rather than on each batch.
    """
    tracker.reset([item.url for item in items])
    db = get_queue_db()
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
    # Everything this session touched, so the settle covers later batches too.
    done: list[QueueItem] = []
    # Everything the session was handed. A crash can land before some of them
    # are reached, so the safety net below needs this wider set.
    claimed: list[QueueItem] = list(items)
    try:
        batch = items
        while batch:
            for item in batch:
                done.append(item)
                if tracker.cancelled(item.url):
                    # Cancelled before the run or while it sat in the queue.
                    db.finish(item.id, STATUS_CANCELLED, error="cancelled")
                    tracker.forget(item.url)
                    continue
                try:
                    download_url(
                        q=task_queue,
                        url=item.url,
                        opts=config.download_settings,
                        folders=config.folders,
                        overrides=config.title_overrides,
                        stats=stats,
                        download_slots=download_slots,
                        allow_adds=config.pipeline.allow_speculative_adds,
                        pin=item.pin,
                    )
                except DownloadCancelled:
                    pass
            # Deliberately not joined here: the transcode worker runs alongside
            # the next download, which is the point of max_non_transcoded.
            batch = more() if more is not None else []
            claimed.extend(batch)
        # Drained once, at the end of the session.
        task_queue.join()
        _settle_remaining(done)
    except Exception as e:
        error_type = type(e).__name__
        crash = f"pipeline failed: {error_type}: {e}"
        # Settle from what the tracker knows first, so an item that finished
        # before the crash keeps its real status; anything still in flight is
        # failed with the crash itself rather than a generic message.
        _settle_remaining(done, crash=crash)
        # Safety net for rows the settle could not reach, so nothing is left
        # `running` — including items claimed but never started before the
        # crash, which have no tracker entry to settle from.
        db.fail_active([i.id for i in claimed], crash)
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
            logger.error(
                "Transcode worker did not terminate within 30s; pipeline may be hung"
            )
            # Everything still unsettled is waiting on the hung worker rather
            # than having failed on its own, so settle before marking the
            # tracker failed — that would replace the real stage ("transcoding")
            # with a generic download failure.
            _settle_remaining(done)
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


def _prompt_selection(query: str) -> TmdbPin | None:
    """Search TMDb for `query` and let the user pick a result.

    Prints a numbered list and reads a choice from stdin. Returns None when
    the user skips, so queueing is never blocked by the picker.
    """
    from iplayerdl.info import search_tmdb

    try:
        results = search_tmdb(query)
    except Exception as e:  # noqa: BLE001 - network/TMDb errors must not abort queueing
        logger.warning("TMDb search failed for %r: %s: %s", query, type(e).__name__, e)
        print(f"TMDb search failed: {type(e).__name__}: {e}")
        return None
    if not results:
        print(f'No TMDb results for "{query}".')
        return None
    print(f'\nTMDb results for "{query}":')
    for i, r in enumerate(results, start=1):
        kind = "TV  " if r["kind"] == "tv" else "FILM"
        line = f"  {i:2d}. [{kind}] {r['title']}"
        if r["overview"]:
            line += f"\n      {r['overview'][:110]}"
        print(line)
    # A piped answer works fine; with no tty and nothing on stdin, read() hits
    # EOF immediately and the except below keeps queueing rather than aborting.
    try:
        raw = input(f"\nChoose 1-{len(results)} to pin (blank to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nNo metadata override was chosen.")
        return None
    if not raw:
        return None
    try:
        choice = int(raw)
    except ValueError:
        print(f'"{raw}" is not a number; queueing without a metadata override.')
        return None
    if not 1 <= choice <= len(results):
        print(f"{choice} is out of range; queueing without a metadata override.")
        return None
    r = results[choice - 1]
    print(f"Pinned metadata to {r['kind']}:{r['id']} ({r['title']})")
    return TmdbPin(kind=r["kind"], id=r["id"], title=r["title"])


def _cmd_queue_add(args: argparse.Namespace) -> None:
    if args.search and len(args.urls) > 1:
        # One TMDb pick applies to every URL enqueued with it, so asking for
        # a pin alongside several URLs would misfile all but one.
        print(
            "error: --search can only pin a single URL; "
            "queue them separately to pin each one",
            file=sys.stderr,
        )
        raise SystemExit(2)
    db = get_queue_db()
    pin = _prompt_selection(args.search) if args.search else None
    inserted, skipped = db.enqueue(args.urls, pin=pin)
    print(
        f"Queued {len(inserted)} item(s)"
        + (f", skipped {skipped} already queued" if skipped else "")
    )
    if args.run:
        _drain()


def _cmd_queue_list(args: argparse.Namespace) -> None:
    db = get_queue_db()
    if getattr(args, "statuses", None):
        items = db.list_items(statuses=args.statuses, limit=args.limit)
        items.reverse()
    else:
        items = [*db.active(), *db.history(args.limit)]
    if not items:
        print("Queue is empty.")
        return
    print(f"DB: {db.path}")
    for item in items:
        pin = f"  pin={item.pin.kind}:{item.pin.id}" if item.pin else ""
        when = item.finished_at or item.started_at or item.queued_at
        note = f"  {item.error}" if item.error else ""
        detail = f" [{item.detail}]" if item.detail else ""
        # The title the item is filed under, which is what the user picked
        # when there is a pin, else what the resolver matched.
        title = media_title(item.resolved)
        label = f"  -> {title}" if title else ""
        print(
            f"{item.id:>5}  {item.status:<11}  {when}  {item.url}"
            f"{pin}{label}{detail}{note}"
        )
    print("\n" + "  ".join(f"{k}={v}" for k, v in db.counts().items()))


def _cmd_queue_cancel(args: argparse.Namespace) -> None:
    db = get_queue_db()
    done = 0
    for item_id in args.ids:
        item = db.get(item_id)
        if item is None:
            print(f"{item_id}: not found")
            continue
        if db.cancel(item_id):
            done += 1
            tracker.cancel(item.url)
            print(f"{item_id}: cancelled {item.url}")
        else:
            print(f"{item_id}: already finished ({item.status})")
    print(f"Cancelled {done} item(s).")


def _cmd_queue_retry(args: argparse.Namespace) -> None:
    db = get_queue_db()
    # A retry undoes a cancellation, so drop any in-memory one the user left
    # behind: tracker.reset() only forgets cancellations for URLs outside the
    # upcoming run, so a stale entry for a requeued URL would cancel it again
    # instead of downloading it.
    for item_id in args.ids:
        item = db.get(item_id)
        if item is not None:
            tracker.forget(item.url)
    print(f"Requeued {db.requeue(args.ids)} item(s).")


def _cmd_queue_remove(args: argparse.Namespace) -> None:
    db = get_queue_db()
    ids: list[int] = list(args.ids)
    if args.all:
        ids += [i.id for i in db.list_items()]
    if not ids:
        print("Nothing to remove.")
        return
    print(f"Removed {db.delete(ids)} item(s) from the queue history.")


def _cmd_queue_clear(args: argparse.Namespace) -> None:
    db = get_queue_db()
    print(f"Cleared {db.clear_history(keep=args.keep)} finished item(s).")


def _cmd_search(args: argparse.Namespace) -> None:
    from iplayerdl.info import search_tmdb

    results = search_tmdb(args.query, limit=args.limit)
    if not results:
        print(f'No TMDb results for "{args.query}".')
        return
    for i, r in enumerate(results, start=1):
        print(f"  {i:2d}. [{r['kind']:<5}] {r['title']}  (id={r['id']})")
        if r["overview"]:
            print(f"       {r['overview'][:110]}")


def _drain() -> None:
    """Claim everything queued and run it."""
    db = get_queue_db()
    items = db.claim()
    if not items:
        print("Nothing queued.")
        return
    config = load_config()
    apply_environment(config)
    setup_logging(get_log_level(config))
    print(f"Processing {len(items)} queued item(s)...")
    run_pipeline(config, items)


def _build_parser() -> argparse.ArgumentParser:
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
    # Shared by every subcommand. SUPPRESS so a subparser that does not see
    # --config does not overwrite a value already given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", type=str, default=argparse.SUPPRESS, help="path to config.toml"
    )
    # Also on the top-level parser: with no subcommand, cli() falls back to
    # "run", and `run` is then a subparser whose attributes do not exist.
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="path to config.toml",
    )

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run",
        parents=[common],
        help="queue the given URLs (if any) and process the whole queue (default)",
    )
    run_parser.add_argument("urls", nargs="*", help="episode/series URLs to queue")
    run_parser.add_argument(
        "--search",
        type=str,
        default=None,
        help="pick a TMDb result to pin as the metadata override",
    )
    run_parser.add_argument(
        "--list",
        action="store_true",
        help="show the queue and its history instead of running",
    )

    web_parser = subparsers.add_parser(
        "web",
        parents=[common],
        help="start the web interface for editing settings and queueing URLs",
    )
    web_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="bind address (overrides [web] host)",
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="bind port (overrides [web] port)",
    )

    queue_parser = subparsers.add_parser(
        "queue", parents=[common], help="add to and inspect the persistent queue"
    )
    # Bare `queue` behaves like `queue list`; set_defaults here is inherited by
    # the subparsers below unless they override the same keys.
    queue_parser.set_defaults(
        func=_cmd_queue_list, statuses=None, limit=DEFAULT_HISTORY_LIMIT
    )
    queue_subs = queue_parser.add_subparsers(dest="queue_command")

    q_add = queue_subs.add_parser(
        "add", parents=[common], help="add URLs to the queue (optionally run them)"
    )
    q_add.add_argument("urls", nargs="+", help="episode/series URLs to queue")
    q_add.add_argument(
        "--search",
        type=str,
        default=None,
        help="TMDb query to search, then choose the metadata override to pin",
    )
    q_add.add_argument(
        "--run", action="store_true", help="process the queue straight away"
    )
    q_add.set_defaults(func=_cmd_queue_add)

    q_list = queue_subs.add_parser(
        "list", parents=[common], help="show queued items and past history"
    )
    q_list.add_argument(
        "--status",
        dest="statuses",
        action="append",
        choices=list(ALL_STATUSES),
        help="only show this status (repeatable)",
    )
    q_list.add_argument(
        "--limit", type=int, default=DEFAULT_HISTORY_LIMIT, help="max history rows"
    )
    q_list.set_defaults(func=_cmd_queue_list)

    q_cancel = queue_subs.add_parser(
        "cancel", parents=[common], help="cancel queued or running items by id"
    )
    q_cancel.add_argument("ids", type=int, nargs="+", help="queue item ids")
    q_cancel.set_defaults(func=_cmd_queue_cancel)

    q_retry = queue_subs.add_parser(
        "retry", parents=[common], help="put finished items back on the queue"
    )
    q_retry.add_argument("ids", type=int, nargs="+", help="queue item ids")
    q_retry.set_defaults(func=_cmd_queue_retry)

    q_remove = queue_subs.add_parser(
        "remove", parents=[common], help="delete items from the queue entirely"
    )
    q_remove.add_argument("ids", type=int, nargs="*", help="queue item ids")
    q_remove.add_argument(
        "--all", action="store_true", help="remove every item, running ones excluded"
    )
    q_remove.set_defaults(func=_cmd_queue_remove)

    q_clear = queue_subs.add_parser(
        "clear", parents=[common], help="delete finished items from the history"
    )
    q_clear.add_argument(
        "--keep", type=int, default=0, help="keep this many most recent items"
    )
    q_clear.set_defaults(func=_cmd_queue_clear)

    search_parser = subparsers.add_parser(
        "search", parents=[common], help="search TMDb without queueing anything"
    )
    search_parser.add_argument("query", help="TMDb search query")
    search_parser.add_argument(
        "--limit", type=int, default=8, help="max results per media type"
    )
    return parser


def cli() -> None:
    parser = _build_parser()
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
        return

    if command == "search":
        _cmd_search(args)
        return

    if command == "queue":
        # `queue` with no subcommand falls back to the default list handler,
        # which queue_parser.set_defaults supplies.
        args.func(args)
        return

    # run: queue any given URLs, then process everything queued.
    db = get_queue_db()
    urls = getattr(args, "urls", None) or []
    if urls:
        search = getattr(args, "search", None)
        if search and len(urls) > 1:
            parser.error(
                "--search can only pin a single URL; "
                "queue them separately to pin each one"
            )
        pin = _prompt_selection(search) if search else None
        inserted, skipped = db.enqueue(urls, pin=pin)
        print(
            f"Queued {len(inserted)} item(s)"
            + (f", skipped {skipped} already queued" if skipped else "")
        )
    if getattr(args, "list", False):
        _cmd_queue_list(argparse.Namespace(statuses=None, limit=DEFAULT_HISTORY_LIMIT))
        return
    if not urls and not db.pending():
        parser.error(
            "nothing queued: pass URLs, use 'iplayerdl queue add', "
            "or add them in the web UI"
        )
    _drain()


if __name__ == "__main__":
    cli()
