import json
import logging
import os
import threading
import tomllib
import traceback
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from iplayerdl.classes import TmdbPin
from iplayerdl.config_loader import (
    apply_environment,
    ensure_config,
    get_config_path,
    load_config,
)
from iplayerdl.main import run_pipeline
from iplayerdl.queue_db import (
    DEFAULT_HISTORY_LIMIT,
    STATUS_QUEUED,
    TERMINAL_STATUSES,
    QueueItem,
    get_queue_db,
    media_title,
)
from iplayerdl.tracker import tracker as job_tracker

logger = logging.getLogger(__name__)


class _RunnerLogHandler(logging.Handler):
    """Forwards log records into the runner's in-memory log."""

    def __init__(self, runner: "PipelineRunner") -> None:
        super().__init__()
        self._runner = runner
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._runner.write(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never break the runner
            self.handleError(record)


class PipelineRunner:
    """Runs the pipeline in a background thread, at most one at a time.

    The queue itself lives in sqlite (see queue_db), so items can be added at
    any time and past items stay visible; this class only owns the run loop
    and the in-memory log tail.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._log: deque[str] = deque(maxlen=500)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(
        self,
        urls: list[str],
        pin: TmdbPin | None = None,
    ) -> tuple[int, int, bool]:
        """Append URLs to the queue, starting a download when one is free.

        Returns (inserted_count, skipped_count, started).
        """
        if pin is not None and len(urls) > 1:
            # One pin applies to every URL enqueued with it, so accepting
            # several here would misfile all but one.
            raise ValueError(
                "'pin' can only be set for a single URL; "
                "queue them separately to pin each one"
            )
        inserted, skipped = get_queue_db().enqueue(urls, pin=pin)
        started = self.start()
        return len(inserted), skipped, started

    def start(self) -> bool:
        """Claim the queue and process it, if the downloader is free.

        "Free" means no run is in progress. The pipeline pulls one URL at a
        time, so an idle pipeline always has a slot for `max_non_transcoded`
        full-quality files; while one is running its slots are taken and
        newly queued items wait for the run to finish. A run that finds
        nothing queued does not start, so this is cheap to call on every add.
        """
        with self._lock:
            if self.running:
                return False
            # Claim up front so a concurrent start cannot double-claim rows.
            items = get_queue_db().claim()
            if not items:
                return False
            self._log.clear()
            self._thread = threading.Thread(target=self._run, args=(items,), daemon=True)
            self._thread.start()
            return True

    def _release_and_drain(self) -> None:
        """Free this run's slot, then pick up anything queued behind it.

        Without the second half, a URL added while a run was in flight would
        sit in the queue with nothing left to start it.
        """
        me = threading.current_thread()
        with self._lock:
            if self._thread is me:
                self._thread = None
        self.start()

    def _run(self, items: list[QueueItem]) -> None:
        from iplayerdl.config_loader import apply_environment, load_config
        from iplayerdl.logging_setup import get_log_level, setup_logging

        root = logging.getLogger()
        log_handler = _RunnerLogHandler(self)
        root.addHandler(log_handler)
        try:
            config = load_config()
            apply_environment(config)
            setup_logging(get_log_level(config))
            with redirect_stdout(self), redirect_stderr(self):
                # Claim again after each batch, so a URL queued while this
                # session is busy downloads as soon as a slot frees rather than
                # waiting for every transcode of the current batch to finish.
                run_pipeline(config, items, more=lambda: get_queue_db().claim())
            self._write("Pipeline finished successfully\n")
        except Exception as e:
            self._write(f"Pipeline failed: {type(e).__name__}: {e}\n")
            self._write(traceback.format_exc() + "\n")
            logger.exception("Pipeline failed")
            # run_pipeline settles its own rows before re-raising, so this only
            # catches what it could not reach (e.g. it never got started).
            get_queue_db().fail_active(
                [i.id for i in items],
                f"pipeline failed: {type(e).__name__}: {e}",
            )
            job_tracker.fail_pending()
        finally:
            root.removeHandler(log_handler)
            self._release_and_drain()

    def _write(self, text: str) -> None:
        with self._log_lock:
            for line in text.splitlines():
                self._log.append(line)

    def write(self, text: str) -> None:  # redirect_stdout target
        self._write(text)

    def flush(self) -> None:
        pass

    def slot_free(self) -> bool:
        """Whether a newly queued URL would start downloading right away."""
        return not self.running

    def status(self, history_limit: int = DEFAULT_HISTORY_LIMIT) -> dict:
        """Live job state merged with the persistent queue.

        The tracker holds byte-level progress for whatever is in flight;
        sqlite holds the durable queue plus finished history. Progress wins
        for active rows so the bars keep moving between polls.
        """
        db = get_queue_db()
        known = {job["url"]: dict(job) for job in job_tracker.snapshot()}
        rows = [*db.active(), *db.history(history_limit)]
        jobs = []
        for item in rows:
            # The tracker keeps a finished job around with its last progress
            # text ("3/10 MB"), which is noise once the item has settled.
            show_live = item.status not in TERMINAL_STATUSES
            live = known.get(item.url, {}) if show_live else {}
            if item.status == STATUS_QUEUED:
                job = {
                    "url": item.url,
                    "status": "pending",
                    "percent": None,
                    "detail": item.detail,
                    "done": 0,
                }
            else:
                job = {
                    "url": item.url,
                    # sqlite only knows queued/running; the tracker knows which
                    # stage an in-flight item is really at, and the UI needs
                    # that to draw a determinate progress bar. Its default
                    # "pending" carries no information for an item that has
                    # already been claimed, so keep the DB's "running".
                    "status": (
                        live.get("status")
                        if live.get("status") not in (None, "pending")
                        else None
                    )
                    or item.status,
                    "percent": live.get("percent"),
                    "detail": live.get("detail") or item.detail,
                    "done": live.get("done", 0),
                }
            if item.pin:
                job["pin"] = {
                    "kind": item.pin.kind,
                    "id": item.pin.id,
                    "title": item.pin.title,
                }
            # What metadata matched to. The tracker learns it as soon as the
            # first episode resolves, well before the row settles; sqlite
            # keeps it so history still shows the match afterwards.
            resolved = live.get("title") or item.resolved
            job["title"] = item.pin.title if item.pin else media_title(resolved)
            job["configured"] = bool(item.pin)
            job["id"] = item.id
            job["error"] = item.error
            job["queued_at"] = item.queued_at
            job["finished_at"] = item.finished_at
            if item.status == STATUS_QUEUED and job_tracker.cancelled(item.url):
                # Cancelled via URL rather than by id, so the row is still
                # 'queued'; the pipeline will settle it as cancelled.
                job["status"] = "cancelled"
            jobs.append(job)
        with self._log_lock:
            log_copy = list(self._log)
        return {
            "running": self.running,
            "slot_free": self.slot_free(),
            "log": log_copy,
            "jobs": jobs,
            "counts": db.counts(),
        }


runner = PipelineRunner()


def _query_param(path: str, name: str) -> str:
    """Read one query-string parameter, returning "" when absent.

    Uses parse_qs so a space arrives as a space whether the client sent it as
    %20 (encodeURIComponent) or + (URLSearchParams / a hand-typed URL).
    """
    _, _, query = path.partition("?")
    values = parse_qs(query).get(name, [])
    return values[0] if values else ""


def _int_field(data: dict, name: str) -> int:
    """Read an integer body field, rejecting bools (which are ints in Python)."""
    value = data.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        # ValueError so do_POST answers 400 rather than 500.
        raise ValueError(  # noqa: TRY004 - bad request body, not a caller type error
            f"'{name}' must be an integer"
        )
    return value


def _int_list(data: dict) -> list[int]:
    """Read a list-of-integers body field under 'ids'."""
    ids = data.get("ids")
    if not isinstance(ids, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) for i in ids
    ):
        raise ValueError("'ids' must be a list of integers")
    return ids


def _parse_pin(raw: object) -> TmdbPin | None:
    """Validate a pin from a JSON body: {kind, id, title} or null/empty."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        # ValueError so do_POST answers 400 rather than 500.
        raise ValueError(  # noqa: TRY004 - bad request body, not a caller type error
            "'pin' must be an object with kind/id/title, or null"
        )
    kind = raw.get("kind")
    pin_id = raw.get("id")
    if (
        kind not in ("tv", "movie")
        or not isinstance(pin_id, int)
        or isinstance(pin_id, bool)
    ):
        raise ValueError("'pin' needs kind ('tv'/'movie') and an integer id")
    try:
        return TmdbPin(kind=kind, id=pin_id, title=str(raw.get("title") or ""))
    except ValueError as e:
        raise ValueError(str(e)) from e


def _read_config_text() -> str:
    return ensure_config(get_config_path()).read_text()


def _validate_toml(text: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Invalid TOML: {e}") from e


def _save_config(text: str) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 5_000_000:
            raise ValueError("Invalid request body size")
        # Requiring application/json blocks cross-site form-style posts: a
        # cross-origin fetch can only send a "simple" content type without a
        # preflight, so no other page can drive these endpoints from a browser
        # the user happens to have open.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype.lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            # ValueError so callers answer 400 rather than 500.
            raise ValueError(  # noqa: TRY004 - bad request body, not a caller type error
                "Expected a JSON object"
            )
        return data

    def do_GET(self) -> None:
        try:
            route = self.path.split("?", 1)[0]
            if route == "/" or route == "/index.html":
                self._send_html(PAGE_HTML)
            elif route == "/api/config":
                self._send_json(
                    {
                        "path": str(get_config_path()),
                        "content": _read_config_text(),
                        "db": str(get_queue_db().path),
                    }
                )
            elif route == "/api/status":
                self._send_json(runner.status())
            elif route == "/api/queue":
                db = get_queue_db()
                self._send_json(
                    {
                        "db": str(db.path),
                        "pending": [i.as_dict() for i in db.active()],
                        "history": [i.as_dict() for i in db.history()],
                        "counts": db.counts(),
                    }
                )
            elif route == "/api/tmdb/search":
                query = _query_param(self.path, "q")
                if not query:
                    raise ValueError("'q' query parameter is required")
                from iplayerdl.info import search_tmdb

                self._send_json({"query": query, "results": search_tmdb(query)})
            else:
                self._send_json({"error": "not found"}, status=404)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001 - any failure becomes a 500, not a crash
            self._send_json({"error": str(e)}, status=500)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/config":
                data = self._read_json_body()
                content = data.get("content")
                if not isinstance(content, str):
                    raise ValueError("'content' must be a string")
                _validate_toml(content)
                _save_config(content)
                self._send_json({"ok": True})
            elif self.path == "/api/urls":
                data = self._read_json_body()
                urls = data.get("urls")
                if not isinstance(urls, list) or not all(
                    isinstance(u, str) for u in urls
                ):
                    raise ValueError("'urls' must be a list of strings")
                # A free slot means the URL starts downloading straight away;
                # otherwise it waits for the run in flight to finish.
                inserted, skipped, started = runner.submit(
                    urls, pin=_parse_pin(data.get("pin"))
                )
                self._send_json(
                    {
                        "ok": True,
                        "inserted": inserted,
                        "skipped": skipped,
                        "started": started,
                        "running": runner.running,
                        "slot_free": runner.slot_free(),
                    }
                )
            elif self.path == "/api/pin":
                data = self._read_json_body()
                item_id = _int_field(data, "id")
                ok = get_queue_db().set_pin(item_id, _parse_pin(data.get("pin")))
                self._send_json({"ok": True, "updated": ok})
            elif self.path == "/api/queue/delete":
                data = self._read_json_body()
                if data.get("all"):
                    # Everything, so any explicitly listed ids are redundant.
                    ids = [i.id for i in get_queue_db().list_items()]
                else:
                    ids = _int_list(data)
                self._send_json({"ok": True, "deleted": get_queue_db().delete(ids)})
            elif self.path == "/api/queue/retry":
                data = self._read_json_body()
                db = get_queue_db()
                ids = _int_list(data)
                # A retry undoes a cancellation, so drop any in-memory one the
                # user left behind: tracker.reset() only forgets cancellations
                # for URLs outside the upcoming run, so a stale entry for a
                # requeued URL would cancel it again instead of downloading it.
                for item in (db.get(i) for i in ids):
                    if item is not None:
                        job_tracker.forget(item.url)
                requeued = db.requeue(ids)
                # Like /api/urls above, a requeued item must start now when
                # the runner is idle; otherwise it would sit at 'queued'
                # with nothing left to pick it up.
                started = runner.start() if requeued else False
                self._send_json(
                    {
                        "ok": True,
                        "requeued": requeued,
                        "started": started,
                        "running": runner.running,
                    }
                )
            elif self.path == "/api/queue/clear":
                data = self._read_json_body()
                keep = data.get("keep", 0)
                if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
                    raise ValueError("'keep' must be a non-negative integer")
                self._send_json(
                    {"ok": True, "cleared": get_queue_db().clear_history(keep)}
                )
            elif self.path == "/api/cancel":
                data = self._read_json_body()
                if "id" in data:
                    item_id = _int_field(data, "id")
                    item = get_queue_db().get(item_id)
                    if item is None:
                        raise ValueError(f"no queue item with id {item_id}")
                    cancelled = get_queue_db().cancel(item_id)
                    if cancelled:
                        # Only steer the in-memory tracker when the row really
                        # was active, so a finished item is not relabelled.
                        job_tracker.cancel(item.url)
                else:
                    url = data.get("url")
                    if not isinstance(url, str) or not url:
                        raise ValueError("'id' or 'url' is required")
                    cancelled = job_tracker.cancel(url)
                self._send_json({"ok": True, "cancelled": cancelled})
            elif self.path == "/api/run":
                started = runner.start()
                self._send_json(
                    {"ok": True, "started": started, "running": runner.running}
                )
            else:
                self._send_json({"error": "not found"}, status=404)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001 - any failure becomes a 500, not a crash
            self._send_json({"error": str(e)}, status=500)


PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iplayerDL</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, sans-serif;
    background: #12141a;
    color: #e4e6eb;
    max-width: 900px;
    margin: 0 auto;
    padding: 1.5rem;
  }
  header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1rem; }
  h1 { margin: 0; }
  h2 { margin-top: 0; font-size: 1.05rem; }
  #gear-btn {
    background: #2a2e3a;
    border: none;
    border-radius: 8px;
    width: 40px; height: 40px;
    cursor: pointer;
    display: grid; place-items: center;
    transition: transform 0.3s ease;
  }
  #gear-btn.open { transform: rotate(90deg); background: #3d6ef5; }
  #gear-btn svg { width: 22px; height: 22px; fill: #e4e6eb; }
  section {
    background: #1b1e26;
    border: 1px solid #2a2e3a;
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin-bottom: 1.25rem;
  }
  textarea, input[type="url"], input[type="text"] {
    width: 100%;
    background: #12141a;
    color: #e4e6eb;
    border: 1px solid #2a2e3a;
    border-radius: 6px;
    padding: 0.6rem;
    font-family: ui-monospace, monospace;
    font-size: 0.85rem;
  }
  textarea { resize: vertical; }
  input:focus, textarea:focus { outline: 2px solid #3d6ef5; outline-offset: -1px; }
  #url-input { font-size: 0.95rem; }
  #pin-query { flex: 1; min-width: 200px; font-family: inherit; }
  #config-editor { min-height: 380px; }
  .row { display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap; }
  .hidden { display: none; }
  .override {
    margin-top: 0.75rem;
    border: 1px solid #2a2e3a;
    border-radius: 8px;
    padding: 0.7rem 0.8rem 0.8rem;
    background: #171a21;
  }
  .override.staged { border-color: #5b4a86; }
  .override-head { margin-bottom: 0.15rem; }
  .override-label { font-size: 0.85rem; font-weight: 600; }
  .pin-chip {
    font-size: 0.75rem;
    padding: 0.18rem 0.6rem;
    border-radius: 99px;
    max-width: 60%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pin-chip.none { background: #2a2e3a; color: #a8adb8; }
  .pin-chip.set { background: #3a2a5c; color: #c9b3ff; }
  button.action {
    background: #3d6ef5;
    border: none;
    border-radius: 6px;
    color: white;
    padding: 0.5rem 1.1rem;
    font-size: 0.9rem;
    cursor: pointer;
  }
  button.secondary { background: #2a2e3a; color: #e4e6eb; border: none; border-radius: 6px; padding: 0.5rem 1.1rem; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  label.inline { display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.85rem; color: #a8adb8; }
  .hint { color: #7a7f8a; font-size: 0.82rem; margin: 0.2rem 0 0.6rem; }
  #msg { font-size: 0.85rem; margin-left: auto; }
  #msg.err { color: #ff6b6b; }
  #msg.ok { color: #6bd66b; }

  .job {
    display: flex;
    align-items: center;
    gap: 0.9rem;
    padding: 0.55rem 0;
    border-bottom: 1px solid #23262f;
  }
  .job:last-child { border-bottom: none; }
  .job .name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.88rem;
  }
  .job .bar-wrap { width: 220px; height: 10px; background: #0c0d11; border-radius: 99px; overflow: hidden; flex-shrink: 0; }
  .job .bar { height: 100%; width: 0%; background: #3d6ef5; border-radius: 99px; transition: width 0.4s ease; }
  .job .bar.indeterminate {
    width: 35%;
    animation: slide 1.1s infinite linear;
    background: #f5a623;
  }
  @keyframes slide { from { margin-left: -35%; } to { margin-left: 105%; } }
  .chip {
    font-size: 0.72rem;
    padding: 0.15rem 0.55rem;
    border-radius: 99px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    width: 96px; text-align: center;
    flex-shrink: 0;
  }
  .chip.pending, .chip.queued { background: #2a2e3a; color: #a8adb8; }
  .chip.resolving, .chip.running { background: #2a4a8a; color: #9db8ff; }
  .chip.downloading { background: #14532d; color: #6ee7a0; }
  .chip.transcoding { background: #5c4a10; color: #ffd66b; }
  .chip.completed { background: #14532d; color: #4ade80; }
  .chip.failed, .chip.unresolved { background: #6b1d1d; color: #ff8f8f; }
  .chip.cancelled { background: #3a3a3a; color: #b8b8b8; }
  .job.finished .name, .job.finished .bar-wrap, .job.finished .detail,
  .job.finished .matched { opacity: 0.62; }
  .job .pin-mark {
    font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.05em;
    padding: 0.14rem 0.42rem; border-radius: 99px;
    background: #3a2a5c; color: #c9b3ff; flex-shrink: 0;
  }
  .job .matched {
    font-size: 0.82rem; color: #a8adb8; flex-shrink: 1;
    min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .job .row-actions { display: flex; gap: 0.35rem; flex-shrink: 0; }
  .job .icon-btn {
    background: #2a2e3a; border: none; border-radius: 6px; color: #a8adb8;
    width: 26px; height: 26px; font-size: 0.8rem; line-height: 1;
    cursor: pointer; display: grid; place-items: center;
  }
  .job .icon-btn:hover { background: #3d6ef5; color: #fff; }
  .pin-result {
    display: flex; gap: 0.7rem; align-items: center;
    padding: 0.45rem 0.5rem; border-bottom: 1px solid #23262f; cursor: pointer;
    border-radius: 6px;
  }
  .pin-result:hover { background: #23262f; }
  .pin-result .kind {
    font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.05em;
    color: #7a7f8a; width: 42px; flex-shrink: 0;
  }
  .pin-result .meta { flex: 1; min-width: 0; }
  .pin-result .meta b { font-size: 0.85rem; font-weight: 600; }
  .pin-result .meta small { display: block; color: #7a7f8a; font-size: 0.74rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #counts { color: #7a7f8a; font-size: 0.72rem; font-weight: normal; margin-left: 0.5rem; }
  .job .cancel-btn {
    background: #2a2e3a;
    border: none;
    border-radius: 6px;
    color: #ff8f8f;
    width: 26px; height: 26px;
    font-size: 0.85rem;
    line-height: 1;
    cursor: pointer;
    flex-shrink: 0;
    display: grid; place-items: center;
  }
  .job .cancel-btn:hover { background: #6b1d1d; color: #fff; }
  .job .detail { font-size: 0.75rem; color: #7a7f8a; min-width: 110px; text-align: right; flex-shrink: 0; }
  #log {
    background: #0c0d11;
    border: 1px solid #2a2e3a;
    border-radius: 6px;
    padding: 0.75rem;
    max-height: 300px;
    overflow-y: auto;
    font-size: 0.78rem;
    white-space: pre-wrap;
    word-break: break-word;
    min-height: 60px;
    margin-top: 0.6rem;
  }
  details summary { cursor: pointer; color: #a8adb8; font-size: 0.9rem; }
</style>
</head>
<body>
<header>
  <h1>iplayerDL <small id="config-path" style="color:#7a7f8a;font-size:0.5em;font-weight:normal"></small></h1>
  <button id="gear-btn" title="Settings">
    <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94s-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.49.49 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94L14.4 2.81a.47.47 0 0 0-.48-.41h-3.84a.47.47 0 0 0-.47.41L9.25 5.35c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 0 0-.59.22L2.73 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.49.49 0 0 0-.12-.61l-2.01-1.58ZM12 15.6A3.61 3.61 0 0 1 8.4 12c0-1.98 1.62-3.6 3.6-3.6s3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6Z"/></svg>
  </button>
</header>

<section>
  <h2>Add to queue</h2>
  <p class="hint">One URL at a time, kept in <span id="db-path">the queue database</span>
    after the run. Queuing starts the download as soon as a download slot is free;
    otherwise it joins the run in flight.</p>
  <input id="url-input" type="url" placeholder="https://www.bbc.co.uk/iplayer/episode/..."
         spellcheck="false" autocomplete="off">

  <div class="override" id="pin-panel">
    <div class="row override-head">
      <span class="override-label" id="pin-label">Metadata override</span>
      <span id="pin-current" class="pin-chip none">None &mdash; matched automatically</span>
    </div>
    <p class="hint" id="pin-help">Optional. Search TMDb and pick the exact show or film to use
      instead of the automatic Sonarr/Radarr match.</p>
    <div class="row">
      <input id="pin-query" type="text" placeholder="e.g. Doctor Who 2005" autocomplete="off">
      <button id="pin-search-btn" class="secondary">Search TMDb</button>
      <button id="pin-clear-btn" class="secondary">Clear</button>
      <button id="pin-cancel-btn" class="secondary hidden">Done</button>
    </div>
    <div id="pin-results"></div>
  </div>

  <div class="row" style="margin-top:0.75rem">
    <button id="add-btn" class="action">Queue</button>
    <span id="slot-state" class="hint" style="margin:0"></span>
    <span id="msg"></span>
  </div>
</section>

<section>
  <h2><span id="dot" class="status-dot" style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:0.4rem;background:#4caf50"></span>Queue &amp; history <span id="counts" style="color:#7a7f8a;font-size:0.72rem;font-weight:normal;margin-left:0.5rem"></span></h2>
  <div id="jobs"><p class="hint">Nothing queued.</p></div>
  <div class="row" style="margin-top:0.75rem">
    <button id="clear-history-btn" class="secondary">Clear finished items</button>
  </div>
  <details id="log-details" style="margin-top:0.75rem">
    <summary>Pipeline output</summary>
    <pre id="log"></pre>
  </details>
</section>

<section id="settings-section" style="display:none">
  <h2>Settings</h2>
  <textarea id="config-editor" spellcheck="false"></textarea>
  <div class="row" style="margin-top:0.6rem">
    <button id="save-btn" class="action">Save settings</button>
    <button id="reload-btn" class="secondary">Reload from disk</button>
  </div>
</section>

<script>
const $ = (id) => document.getElementById(id);
let pollTimer = null;

const STATUS_LABEL = {
  pending: 'Queued', queued: 'Queued', running: 'Running', resolving: 'Resolving',
  downloading: 'Downloading', transcoding: 'Transcoding', completed: 'Done',
  failed: 'Failed', unresolved: 'No match', cancelled: 'Cancelled',
};
const FINISHED = ['completed', 'failed', 'unresolved', 'cancelled'];

// The override in play is either staged for the next URL added, or being
// edited on a row that is already queued.
let pinTarget = null;   // {id, pin} while editing a queued row, else null
let stagedPin = null;   // {kind, id, title} to send with the next add
let currentJobs = [];   // last rendered job list, for the per-row pin button

function activePin() {
  return pinTarget ? pinTarget.pin : stagedPin;
}

function renderPin() {
  const pin = activePin();
  const chip = $('pin-current');
  chip.className = 'pin-chip ' + (pin ? 'set' : 'none');
  chip.textContent = pin
    ? (pin.kind === 'tv' ? 'TV: ' : 'Film: ') + pin.title
    : (pinTarget ? 'None on this item' : 'None — matched automatically');
  $('pin-label').textContent = pinTarget
    ? `Metadata override for item #${pinTarget.id}`
    : 'Metadata override';
  $('pin-help').textContent = pinTarget
    ? 'This replaces the automatic Sonarr/Radarr match for that queued item.'
    : 'Optional. Search TMDb and pick the exact show or film to use instead of '
      + 'the automatic Sonarr/Radarr match. The choice is saved with the URL.';
  $('pin-clear-btn').disabled = !pin;
  $('pin-cancel-btn').classList.toggle('hidden', !pinTarget);
  $('pin-panel').classList.toggle('staged', !pinTarget && !!pin);
}

function showMsg(text, ok) {
  const el = $('msg');
  el.textContent = text;
  el.className = ok ? 'ok' : 'err';
  setTimeout(() => { el.textContent = ''; }, 4000);
}

async function api(path, body) {
  const res = await fetch(path, body ? {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  } : undefined);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function parseUrl() {
  return $('url-input').value.trim();
}

function looksLikeUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch (e) { return false; }
}

async function addUrl() {
  const url = parseUrl();
  if (!url) { showMsg('Enter a URL first', false); $('url-input').focus(); return; }
  if (!looksLikeUrl(url)) {
    showMsg('That is not an http(s) URL', false); $('url-input').focus(); return;
  }
  const wanted = stagedPin;
  $('add-btn').disabled = true;
  try {
    const data = await api('/api/urls', {urls: [url], pin: wanted});
    $('url-input').value = '';
    const parts = [data.inserted ? 'Queued' : 'Already queued'];
    if (wanted) {
      if (data.inserted) {
        parts.push(`override: ${wanted.kind === 'tv' ? 'TV' : 'Film'} ${wanted.title}`);
      } else {
        // Nothing was inserted, so the override would have been thrown away.
        // Attach it to the row that is already queued for this URL instead.
        const existing = (await api('/api/queue')).pending
          .find((i) => i.url === url && i.status === 'queued');
        const saved = existing
          && (await api('/api/pin', {id: existing.id, pin: wanted})).updated;
        parts.push(saved
          ? `override applied to item #${existing.id}`
          : 'override could not be applied (item already started)');
      }
    }
    // A free slot starts the download now. Otherwise the item joins the run in
    // flight, which picks it up as soon as a slot frees — not when the
    // current item's transcode finishes.
    parts.push(data.started
      ? 'downloading now'
      : data.running
        ? 'joins the current run when a slot frees'
        : 'queued');
    showMsg(parts.join(' · '), true);
    stagedPin = null;
    $('pin-query').value = '';
    $('pin-results').innerHTML = '';
    renderPin();
    startPolling();
  } catch (e) {
    showMsg(e.message, false);
  }
  $('add-btn').disabled = false;
}

function openPinFor(id, pin) {
  pinTarget = {id, pin: pin || null};
  $('pin-query').value = '';
  $('pin-results').innerHTML = '';
  renderPin();
  $('pin-query').focus();
}

function closePinFor() {
  pinTarget = null;
  $('pin-query').value = '';
  $('pin-results').innerHTML = '';
  renderPin();
}

async function applyPin(pin) {
  if (pinTarget) {
    try {
      const d = await api('/api/pin', {id: pinTarget.id, pin});
      if (!d.updated) {
        // A pin can only change while the item is still queued; leave the
        // override that is already on the row alone.
        showMsg(`Item #${pinTarget.id} has already started — cancel and requeue it to override`,
          false);
      } else {
        pinTarget.pin = pin;
        showMsg(pin ? `Pinned ${pin.kind}: ${pin.title}` : 'Metadata override cleared', true);
      }
    } catch (e) { showMsg(e.message, false); }
    renderPin();
    startPolling();
    return;
  }
  stagedPin = pin;
  $('pin-results').innerHTML = '';
  renderPin();
  showMsg(pin ? `Override ready: add the URL to save it as ${pin.title}`
    : 'Metadata override cleared', true);
}

async function searchTmdb() {
  const q = $('pin-query').value.trim();
  if (!q) { showMsg('Type something to search TMDb', false); $('pin-query').focus(); return; }
  const el = $('pin-results');
  el.innerHTML = '<p class="hint">Searching TMDb...</p>';
  try {
    const data = await api('/api/tmdb/search?q=' + encodeURIComponent(q));
    if (!data.results.length) {
      el.innerHTML = `<p class="hint">No TMDb results for "${escHtml(q)}".</p>`;
      return;
    }
    el.innerHTML = data.results.map((r, i) => `
      <div class="pin-result" data-i="${i}">
        <span class="kind">${r.kind === 'tv' ? 'TV' : 'Film'}</span>
        <span class="meta">
          <b>${escHtml(r.title)}</b>
          <small>${escHtml(r.overview || 'no overview')}</small>
        </span>
      </div>`).join('');
    el.querySelectorAll('.pin-result').forEach(node => {
      node.onclick = () => {
        const r = data.results[Number(node.dataset.i)];
        return applyPin({kind: r.kind, id: r.id, title: r.title});
      };
    });
  } catch (e) {
    el.innerHTML = `<p class="hint err">${escHtml(e.message)}</p>`;
  }
}

async function loadConfig() {
  try {
    const data = await api('/api/config');
    $('config-editor').value = data.content;
    $('config-path').textContent = data.path;
  } catch (e) { showMsg(e.message, false); }
}

async function saveConfig() {
  $('save-btn').disabled = true;
  try {
    await api('/api/config', {content: $('config-editor').value});
    showMsg('Saved', true);
  } catch (e) { showMsg(e.message, false); }
  $('save-btn').disabled = false;
}

function shortUrl(url) {
  try {
    const u = new URL(url);
    const parts = u.pathname.split('/').filter(Boolean);
    if (u.hostname.includes('bbc.co.uk')
        && (parts[0] === 'episode' || parts[0] === 'episodes')) {
      const label = parts[2] || parts[1];
      if (label) return decodeURIComponent(label).replace(/-/g, ' ');
    }
    return url.replace(/^https?:\\/\\//, '');
  } catch (e) { return url; }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function renderJobs(jobs) {
  currentJobs = jobs;
  const el = $('jobs');
  if (!jobs.length) { el.innerHTML = '<p class="hint">Nothing queued.</p>'; return; }
  el.innerHTML = jobs.map(job => {
    const active = ['resolving', 'downloading', 'transcoding', 'running'].includes(job.status);
    const finished = FINISHED.includes(job.status);
    const cancellable = active || job.status === 'pending' || job.status === 'queued';
    let barClass = 'bar';
    let style = '';
    if (job.percent != null && (job.status === 'downloading' || job.status === 'transcoding')) {
      style = `width:${Math.max(2, job.percent)}%`;
    } else if (active) {
      barClass += ' indeterminate';
    } else if (job.status === 'completed') {
      style = 'width:100%';
    }
    const detail = job.error || job.detail || '';
    const actions = [];
    if (cancellable) {
      actions.push(`<button class="cancel-btn" data-id="${job.id}" data-url="${encodeURIComponent(job.url)}" title="Cancel this item">&times;</button>`);
    }
    if (!cancellable) {
      actions.push(`<button class="icon-btn" data-act="retry" data-id="${job.id}" title="Queue this item again">&#8635;</button>`);
    }
    actions.push(`<button class="icon-btn" data-act="pin" data-id="${job.id}" title="Metadata override (search TMDb)">&#9906;</button>`);
    actions.push(`<button class="icon-btn" data-act="del" data-id="${job.id}" title="Remove from queue">&#128465;</button>`);

    // With a configured override the title is the item's identity, so show it
    // alone; without one the metadata is still being detected, so show the
    // link and, once known, what it matched to.
    const title = job.title || '';
    let identity;
    if (job.configured) {
      identity = `<span class="name" title="${escHtml(job.url)}">${escHtml(title)}</span>`
        + '<span class="pin-mark" title="Metadata configured for this item">pinned</span>';
    } else {
      identity = `<span class="name" title="${escHtml(job.url)}">${escHtml(shortUrl(job.url))}</span>`
        + (title
          ? `<span class="matched" title="Matched automatically">${escHtml(title)}</span>`
          : '');
    }
    return `<div class="job ${finished ? 'finished' : ''}">
      <span class="chip ${escHtml(job.status)}">${escHtml(STATUS_LABEL[job.status] || job.status)}</span>
      ${identity}
      <div class="bar-wrap"><div class="${escHtml(barClass)}" style="${escHtml(style)}"></div></div>
      <span class="detail" title="${escHtml(detail)}">${escHtml(detail)}</span>
      <span class="row-actions">${actions.join('')}</span>
    </div>`;
  }).join('');
}

async function jobAction(btn) {
  const id = Number(btn.dataset.id);
  btn.disabled = true;
  try {
    if (btn.classList.contains('cancel-btn')) {
      await api('/api/cancel', {id});
      showMsg('Cancel requested', true);
    } else if (btn.dataset.act === 'retry') {
      const d = await api('/api/queue/retry', {ids: [id]});
      showMsg(d.requeued
        ? (d.started ? 'Requeued · downloading now'
          : d.running ? 'Requeued · joins the current run' : 'Requeued · queued')
        : 'Could not requeue', !!d.requeued);
    } else if (btn.dataset.act === 'del') {
      const d = await api('/api/queue/delete', {ids: [id]});
      showMsg(d.deleted ? 'Removed' : 'Item is running', !!d.deleted);
    } else if (btn.dataset.act === 'pin') {
      btn.disabled = false;
      const row = (currentJobs || []).find((j) => j.id === id);
      openPinFor(id, row && row.pin);
      return;
    }
  } catch (e) {
    showMsg(e.message, false);
    btn.disabled = false;
  }
  startPolling();
}

$('jobs').addEventListener('click', (e) => {
  const btn = e.target.closest('.cancel-btn, .icon-btn');
  if (btn) jobAction(btn);
});

function renderStatus(data) {
  $('dot').style.background = data.running ? '#f5a623' : '#4caf50';
  $('slot-state').textContent = data.running
    ? 'downloading — a new item joins as soon as a slot frees'
    : 'download slot free';
  renderJobs(data.jobs || []);
  const c = data.counts || {};
  $('counts').textContent = ['queued','running','completed','failed','unresolved','cancelled']
    .filter(k => c[k]).map(k => `${c[k]} ${k}`).join(' · ');
  const logEl = $('log');
  logEl.textContent = data.log.length ? data.log.join('\\n') : '';
  logEl.scrollTop = logEl.scrollHeight;
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try { renderStatus(await api('/api/status')); } catch (e) {}
  }, 1500);
  api('/api/status').then(renderStatus).catch(() => {});
}

$('gear-btn').onclick = () => {
  const open = $('settings-section').style.display === 'none';
  $('settings-section').style.display = open ? '' : 'none';
  $('gear-btn').classList.toggle('open', open);
  if (open) loadConfig();
};
$('add-btn').onclick = () => addUrl();
$('save-btn').onclick = saveConfig;
$('reload-btn').onclick = loadConfig;
$('pin-search-btn').onclick = searchTmdb;
$('pin-clear-btn').onclick = () => applyPin(null);
$('pin-cancel-btn').onclick = closePinFor;
$('url-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') addUrl(); });
$('pin-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchTmdb(); });
$('clear-history-btn').onclick = async () => {
  try {
    const d = await api('/api/queue/clear', {keep: 0});
    showMsg(`Cleared ${d.cleared} finished item(s)`, true);
    startPolling();
  } catch (e) { showMsg(e.message, false); }
};

loadConfig().then(() => {
  $('db-path').textContent = 'sqlite';
  return api('/api/queue');
}).then(d => { $('db-path').textContent = d.db; });
renderPin();
startPolling();
</script>
</body>
</html>"""


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    db = get_queue_db()
    logger.info("Queue database: %s", db.path)
    # The TMDb search endpoint runs in the server process, so the API key from
    # [environment] has to be exported here as well as in the pipeline thread.
    config = load_config()
    apply_environment(config)
    if not os.getenv("TMDB_API_KEY"):
        logger.warning(
            "TMDB_API_KEY is not set; the metadata override search will return "
            "no results until it is added to [environment]."
        )
    if host == "0.0.0.0":
        logger.warning(
            "Web interface bound to 0.0.0.0 — no authentication enabled. "
            "Anyone on the network can edit config and trigger downloads."
        )
    server = ThreadingHTTPServer((host, port), Handler)
    logger.info("iplayerDL web interface listening on http://%s:%s", host, port)
    server.serve_forever()
