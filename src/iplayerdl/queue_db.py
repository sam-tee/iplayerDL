"""Persistent, sqlite-backed download queue.

The queue used to be a list of URLs held in memory by the web runner and
drained on the next run, so nothing could be queued ahead of time and
nothing was ever visible after the fact. This module keeps the queue in
sqlite instead:

- items can be added at any time, from the CLI or the web UI, and
  survive restarts
- draining marks items in-flight rather than deleting them, so past
  queued items stay visible with their final status
- each item may carry a TMDb pin (see ``TmdbPin``) chosen when it was
  queued, which overrides the automatic Sonarr/Radarr/TMDb resolution

Concurrency: the web UI serves requests on ``ThreadingHTTPServer`` while
a background thread drains the queue, so every operation takes a lock and
connections are per-thread (``check_same_thread=False`` is deliberately
avoided). WAL plus a busy timeout keeps readers from blocking the
writer.
"""

import logging
import os
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from iplayerdl.classes import TmdbPin

logger = logging.getLogger(__name__)

# Item lifecycle: queued -> running -> (completed|failed|unresolved|cancelled).
# These mirror the tracker's vocabulary so the web UI renders one set of chips.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_UNRESOLVED = "unresolved"
STATUS_CANCELLED = "cancelled"

# Statuses a drained item can settle in; anything else means "still going".
TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_UNRESOLVED, STATUS_CANCELLED}
)

ALL_STATUSES = (STATUS_QUEUED, STATUS_RUNNING) + tuple(sorted(TERMINAL_STATUSES))

DB_ENV_VAR = "IPLAYERDL_DB"
DB_FILE_NAME = "queue.db"

# How many finished items the web UI asks for by default.
DEFAULT_HISTORY_LIMIT = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'queued',
    detail      TEXT    NOT NULL DEFAULT '',
    error       TEXT,
    -- TMDb pin chosen at queue time; NULL means resolve automatically.
    tmdb_kind   TEXT,
    tmdb_id     INTEGER,
    tmdb_title  TEXT,
    -- Media path metadata resolved to, e.g. tv/Show (2005)/Season 01/...
    resolved    TEXT,
    -- pid that claimed the row, so an interrupted run can be spotted later.
    owner_pid   INTEGER,
    queued_at   TEXT    NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS items_status_id ON items (status, id);

-- At most one in-flight item per URL, so the same page is never downloaded
-- twice concurrently. Finished rows keep their duplicates for history.
CREATE UNIQUE INDEX IF NOT EXISTS items_active_url
    ON items (url) WHERE status IN ('queued', 'running');
"""

# Columns added after the first release of the schema. CREATE TABLE IF NOT
# EXISTS will not add them to a database that already exists, and this app
# keeps its queue next to the user's config, so migrate in place.
_ADDED_COLUMNS = (
    ("owner_pid", "owner_pid INTEGER"),
    ("resolved", "resolved TEXT"),
)


@dataclass
class QueueItem:
    """One row of the queue."""

    id: int
    url: str
    status: str
    detail: str = ""
    error: str | None = None
    queued_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    pin: TmdbPin | None = None
    resolved: str = ""

    @property
    def active(self) -> bool:
        return self.status in (STATUS_QUEUED, STATUS_RUNNING)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "detail": self.detail,
            "error": self.error,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "resolved": self.resolved,
            "pin": (
                {
                    "kind": self.pin.kind,
                    "id": self.pin.id,
                    "title": self.pin.title,
                }
                if self.pin
                else None
            ),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def media_title(resolved: str) -> str:
    """Turn a resolved media path into a human label.

    The resolver produces Jellyfin-style paths such as
    ``tv/Doctor Who (2005)/Season 02/Doctor Who (2005) - S02E08 - Rose``; the
    leaf is the part worth showing. Lives here rather than in the renderers so
    the web UI and the CLI label an item identically.
    """
    trimmed = resolved.strip().rstrip("/")
    if not trimmed:
        return ""
    return trimmed.rsplit("/", 1)[-1] or trimmed


def _pid_alive(pid: int | None) -> bool:
    """Whether the process that claimed a row is still running.

    A missing pid means the row predates ownership tracking, so treat it as
    orphaned rather than guessing.
    """
    if pid is None:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just owned by another user
    except OSError:
        return True
    return True


def _row_to_item(row: sqlite3.Row) -> QueueItem:
    pin = None
    # Both fields are written together, so a half-filled row means bad data
    # rather than a real pin. Drop it instead of raising, which would turn a
    # single odd row into a 500 for everything that reads the queue.
    if row["tmdb_id"] is not None and row["tmdb_kind"] in ("tv", "movie"):
        pin = TmdbPin(
            kind=row["tmdb_kind"],
            id=int(row["tmdb_id"]),
            title=row["tmdb_title"] or "",
        )
    return QueueItem(
        id=int(row["id"]),
        url=row["url"],
        status=row["status"],
        detail=row["detail"] or "",
        error=row["error"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        pin=pin,
        resolved=row["resolved"] or "",
    )


class QueueDB:
    """Thread-safe sqlite wrapper around the download queue."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) == ":memory:":
            # Connections are per-thread, and every sqlite3.connect(":memory:")
            # creates a separate empty database, so the schema would only exist
            # on whichever thread opened the store first.
            raise ValueError("':memory:' is not supported; pass a file path")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._init_schema()

    # -- connection handling --------------------------------------------
    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.path),
                timeout=30.0,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            existing = {
                r["name"] for r in self._conn.execute("PRAGMA table_info(items)")
            }
            for name, ddl in _ADDED_COLUMNS:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE items ADD COLUMN {ddl}")
                    logger.info("Added missing column %s to the queue database", name)

    # -- writing ---------------------------------------------------------
    def enqueue(
        self,
        urls: Iterable[str],
        pin: TmdbPin | None = None,
    ) -> tuple[list[int], int]:
        """Append URLs to the queue.

        Blank URLs are dropped and duplicates *within* the call are ignored.
        A URL that is already queued or running is skipped and counted in the
        second return value, so repeated submissions do not stack up.

        Returns ``(inserted_ids, skipped)``.
        """
        inserted: list[int] = []
        skipped = 0
        now = _now()
        with self._lock:
            conn = self._conn
            seen: set[str] = set()
            try:
                conn.execute("BEGIN")
                for raw in urls:
                    url = (raw or "").strip()
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    try:
                        cur = conn.execute(
                            "INSERT INTO items (url, status, queued_at, "
                            "tmdb_kind, tmdb_id, tmdb_title) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                url,
                                STATUS_QUEUED,
                                now,
                                pin.kind if pin else None,
                                pin.id if pin else None,
                                pin.title if pin else None,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        # Already queued or running; leave the existing item be.
                        skipped += 1
                        continue
                    inserted.append(int(cur.lastrowid))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return inserted, skipped

    def claim(self, limit: int | None = None) -> list[QueueItem]:
        """Mark queued items as running and return them in queue order.

        Only the rows this call claimed are returned, so a second caller (or a
        second call) never picks up work another run already owns. The write
        transaction is IMMEDIATE, so concurrent callers serialise here.
        """
        now = _now()
        with self._lock:
            # A row left running by a dead process would otherwise be invisible
            # here and block re-queueing its URL via the partial unique index.
            self._recover_orphans_locked()
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                select = "SELECT id FROM items WHERE status = ? ORDER BY id"
                params: list = [STATUS_QUEUED]
                if limit is not None:
                    select += " LIMIT ?"
                    params.append(max(0, int(limit)))
                ids = [int(r["id"]) for r in conn.execute(select, params).fetchall()]
                if ids:
                    conn.executemany(
                        "UPDATE items SET status = ?, started_at = ?, owner_pid = ? "
                        "WHERE id = ?",
                        [(STATUS_RUNNING, now, os.getpid(), i) for i in ids],
                    )
                    placeholders = ", ".join("?" for _ in ids)
                    rows = conn.execute(
                        f"SELECT * FROM items WHERE id IN ({placeholders}) ORDER BY id",
                        ids,
                    ).fetchall()
                else:
                    rows = []
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return [_row_to_item(r) for r in rows]

    def finish(
        self,
        item_id: int,
        status: str,
        error: str | None = None,
        detail: str | None = None,
        resolved: str | None = None,
    ) -> None:
        """Move an item to a terminal status.

        ``error`` and ``detail`` overwrite the stored values, so a row that
        fails and then succeeds on retry does not keep the old failure text.

        ``resolved`` is the media path metadata matched to. It is only written
        when given, so a run that resolved nothing cannot blank the title an
        earlier attempt found.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"{status!r} is not a terminal status")
        with self._lock:
            self._conn.execute(
                "UPDATE items SET status = ?, finished_at = ?, error = ?, "
                "detail = COALESCE(?, ''), resolved = COALESCE(?, resolved), "
                "owner_pid = NULL WHERE id = ?",
                (status, _now(), error, detail, resolved, item_id),
            )

    def fail_active(self, item_ids: Sequence[int], reason: str) -> int:
        """Fail the still-active subset of ``item_ids``.

        Items that already settled keep their status, so an early failure in a
        batch does not overwrite items that finished successfully.
        """
        ids = [int(i) for i in item_ids]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE items SET status = ?, error = ?, detail = '', "
                f"owner_pid = NULL, finished_at = ? "
                f"WHERE id IN ({placeholders}) AND status IN (?, ?)",
                (STATUS_FAILED, reason, _now(), *ids, STATUS_QUEUED, STATUS_RUNNING),
            )
            return cur.rowcount

    def cancel(self, item_id: int) -> bool:
        """Cancel a queued or running item. False if already finished."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE items SET status = ?, detail = '', owner_pid = NULL, "
                "finished_at = ? WHERE id = ? AND status IN (?, ?)",
                (STATUS_CANCELLED, _now(), item_id, STATUS_QUEUED, STATUS_RUNNING),
            )
            return cur.rowcount > 0

    def set_pin(self, item_id: int, pin: TmdbPin | None) -> bool:
        """Attach (or clear) the TMDb pin on a queued item."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE items SET tmdb_kind = ?, tmdb_id = ?, tmdb_title = ? "
                "WHERE id = ? AND status = ?",
                (
                    pin.kind if pin else None,
                    pin.id if pin else None,
                    pin.title if pin else None,
                    item_id,
                    STATUS_QUEUED,
                ),
            )
            return cur.rowcount > 0

    def requeue(self, item_ids: Sequence[int]) -> int:
        """Move terminal items back to queued so they run again.

        Skips any item whose URL is already queued or running on another row:
        that would break the partial unique index, and silently doing nothing
        is better than a 500 from the caller.
        """
        ids = [int(i) for i in item_ids]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    f"SELECT id, url FROM items WHERE id IN ({placeholders}) "
                    f"AND status IN (?, ?, ?, ?) "
                    f"AND NOT EXISTS ("
                    f"  SELECT 1 FROM items o WHERE o.url = items.url "
                    f"  AND o.id != items.id AND o.status IN (?, ?)"
                    f") ORDER BY id",
                    (*ids, *sorted(TERMINAL_STATUSES), STATUS_QUEUED, STATUS_RUNNING),
                ).fetchall()
                # History can hold several finished rows for the same URL (the
                # partial index only guards active rows), and requeueing two of
                # them at once would set both to 'queued' and trip the index
                # mid-transaction. Keep the oldest row per URL.
                seen_urls: set[str] = set()
                wanted: list[int] = []
                for r in rows:
                    url = r["url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    wanted.append(int(r["id"]))
                if wanted:
                    conn.executemany(
                        "UPDATE items SET status = ?, detail = '', error = NULL, "
                        "started_at = NULL, finished_at = NULL, owner_pid = NULL, "
                        "queued_at = ? WHERE id = ?",
                        [(STATUS_QUEUED, _now(), i) for i in wanted],
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return len(wanted)

    def delete(self, item_ids: Sequence[int]) -> int:
        """Delete items outright. Only non-running rows can be removed."""
        ids = [int(i) for i in item_ids]
        if not ids:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "DELETE FROM items WHERE id = ? AND status != ?",
                [(i, STATUS_RUNNING) for i in ids],
            )
            return cur.rowcount

    def clear_history(self, keep: int = 0) -> int:
        """Drop the oldest finished items, optionally keeping the newest few."""
        if keep < 0:
            raise ValueError("keep must not be negative")
        with self._lock:
            if keep == 0:
                cur = self._conn.execute(
                    "DELETE FROM items WHERE status IN (?, ?, ?, ?)",
                    tuple(sorted(TERMINAL_STATUSES)),
                )
                return cur.rowcount
            placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
            cur = self._conn.execute(
                f"DELETE FROM items WHERE id NOT IN ("
                f"  SELECT id FROM items WHERE status IN ({placeholders}) "
                f"  ORDER BY id DESC LIMIT ?"
                f") AND status IN ({placeholders})",
                (*sorted(TERMINAL_STATUSES), keep, *sorted(TERMINAL_STATUSES)),
            )
            return cur.rowcount

    def recover_orphans(self) -> int:
        """Fail items left ``running`` by a process that is no longer alive.

        Without this a killed process would leave rows ``running`` forever, and
        the unique partial index would then block re-queueing those URLs.

        Ownership is tracked by pid, so a second iplayerDL sharing the database
        (for example the CLI alongside the web service) leaves the other
        process's in-flight items alone. Rows with no recorded pid predate this
        column and are treated as orphans.

        Cheap enough to call before each claim, which is what lets a long-lived
        process notice rows orphaned by some *other* process.
        """
        with self._lock:
            return self._recover_orphans_locked()

    def _recover_orphans_locked(self) -> int:
        """recover_orphans for callers that already hold the lock."""
        rows = self._conn.execute(
            "SELECT id, owner_pid FROM items WHERE status = ?", (STATUS_RUNNING,)
        ).fetchall()
        stale = [int(r["id"]) for r in rows if not _pid_alive(r["owner_pid"])]
        if not stale:
            return 0
        placeholders = ", ".join("?" for _ in stale)
        cur = self._conn.execute(
            f"UPDATE items SET status = ?, finished_at = ?, detail = '', "
            f"owner_pid = NULL, error = ? WHERE id IN ({placeholders})",
            (
                STATUS_FAILED,
                _now(),
                "interrupted: iplayerDL stopped while this was running",
                *stale,
            ),
        )
        logger.info("Marked %d interrupted queue item(s) as failed", cur.rowcount)
        return cur.rowcount

    # -- reading ---------------------------------------------------------
    def get(self, item_id: int) -> QueueItem | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM items WHERE id = ?", (int(item_id),)
            ).fetchone()
        return _row_to_item(row) if row else None

    def pending(self) -> list[QueueItem]:
        """Queued items, oldest first."""
        return self.list_items(statuses=(STATUS_QUEUED,))

    def running(self) -> list[QueueItem]:
        return self.list_items(statuses=(STATUS_RUNNING,))

    def active(self) -> list[QueueItem]:
        return self.list_items(statuses=(STATUS_QUEUED, STATUS_RUNNING))

    def history(self, limit: int = DEFAULT_HISTORY_LIMIT) -> list[QueueItem]:
        """Most recently finished items, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM items WHERE status IN (?, ?, ?, ?) "
                "ORDER BY COALESCE(finished_at, queued_at) DESC, id DESC LIMIT ?",
                (*sorted(TERMINAL_STATUSES), max(0, int(limit))),
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def list_items(
        self,
        statuses: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> list[QueueItem]:
        sql = "SELECT * FROM items"
        params: list = []
        if statuses:
            sql += " WHERE status IN (" + ", ".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_item(r) for r in rows]

    def counts(self) -> dict[str, int]:
        """Row count per status (every status is present, zero-filled)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM items GROUP BY status"
            ).fetchall()
        counts = dict.fromkeys(ALL_STATUSES, 0)
        for row in rows:
            counts[row["status"]] = int(row["n"])
        return counts


_db: QueueDB | None = None
_db_lock = threading.Lock()


def get_db_path() -> Path:
    """Locate the queue database.

    Sits next to config.toml by default so everything the app owns lives in
    one place; ``$IPLAYERDL_DB`` overrides it (the NixOS module points this at
    the systemd state directory, since a Nix store config is read-only).
    """
    override = os.getenv(DB_ENV_VAR)
    if override:
        return Path(override).expanduser()
    # Imported lazily: config_loader pulls in dacite and friends.
    from iplayerdl.config_loader import get_config_path

    return get_config_path().parent / DB_FILE_NAME


def get_queue_db(path: Path | str | None = None) -> QueueDB:
    """Return the process-wide queue store, opening it on first use."""
    global _db
    with _db_lock:
        if _db is None or (path is not None and Path(path) != _db.path):
            _db = QueueDB(path if path is not None else get_db_path())
            _db.recover_orphans()
        return _db
