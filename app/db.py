"""SQLite persistence for datasets, immutable revisions and the tile cache."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id              TEXT PRIMARY KEY,
    layer           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    latest_revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS revisions (
    dataset_id    TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    feature_count INTEGER NOT NULL,
    geojson       BLOB NOT NULL,
    published_at  TEXT NOT NULL,
    PRIMARY KEY (dataset_id, revision),
    FOREIGN KEY (dataset_id) REFERENCES datasets(id)
);

CREATE TABLE IF NOT EXISTS tile_cache (
    dataset_id TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    z          INTEGER NOT NULL,
    x          INTEGER NOT NULL,
    y          INTEGER NOT NULL,
    etag       TEXT NOT NULL,
    body       BLOB NOT NULL,
    PRIMARY KEY (dataset_id, revision, z, x, y)
);

CREATE INDEX IF NOT EXISTS idx_tile_cache_rev
    ON tile_cache (dataset_id, revision);
"""


class RevisionConflict(Exception):
    """Raised when ``expected_revision`` does not match the stored head."""

    def __init__(self, current_revision: int):
        super().__init__(f"expected revision mismatch, current is {current_revision}")
        self.current_revision = current_revision


class NotFound(Exception):
    """Dataset or revision does not exist."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    # isolation_level=None puts the driver in autocommit mode, so Python never
    # opens a (deferred) transaction behind our backs.  All multi-statement
    # writes are wrapped explicitly in ``write_transaction`` below.
    conn = sqlite3.connect(
        settings.db_path, timeout=30, check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    # busy_timeout must be configured *before* switching journal_mode: on a
    # brand-new database, several processes can race the one-time WAL
    # conversion (which needs an exclusive lock).  Without the timeout the
    # PRAGMA returns SQLITE_BUSY immediately and a second worker crashes at
    # startup.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    import os
    import sqlite3 as _sqlite3

    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    # Multiple workers start concurrently and may race the one-time WAL setup
    # on a fresh volume; busy_timeout already makes them queue, but retry once
    # more on a lock so a cold start never takes a worker down.
    for attempt in range(5):
        try:
            with _connect() as conn:
                conn.executescript(_SCHEMA)
            return
        except _sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Cross-process write transaction.

    ``BEGIN IMMEDIATE`` acquires the database write lock (the only writer lock
    in WAL mode) up front, so the compare-and-set for a new revision is
    serialised against *all* processes sharing this database, not only other
    threads in one worker.  Any concurrent ``BEGIN IMMEDIATE`` blocks here for
    up to ``busy_timeout`` and then runs against the new head, which makes the
    loser re-read the advanced revision and return a clean 409 instead of
    hitting the revisions primary key.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# No in-process lock is needed for revision publishing: write_transaction()
# takes SQLite's cross-process write lock (BEGIN IMMEDIATE), which serialises
# the expected_revision compare-and-set both between threads and between
# separate worker processes / containers sharing the same database file.


def get_dataset(conn: sqlite3.Connection, dataset_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
    ).fetchone()


def get_revision(
    conn: sqlite3.Connection, dataset_id: str, revision: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM revisions WHERE dataset_id = ? AND revision = ?",
        (dataset_id, revision),
    ).fetchone()


def publish_initial_revision(
    conn: sqlite3.Connection,
    dataset_id: str,
    layer: str,
    geojson_blob: bytes,
    content_hash: str,
    feature_count: int,
) -> dict[str, Any]:
    """Create a brand-new dataset at revision 1.

    Raises ``RevisionConflict`` (HTTP 409 at the API layer) if a dataset with
    the generated id already exists; the id is random UUID4 so this is only a
    theoretical safety net.
    """
    now = utc_now_iso()
    with write_transaction(conn):
        existing = conn.execute(
            "SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
        if existing is not None:
            raise RevisionConflict(0)
        conn.execute(
            "INSERT INTO datasets (id, layer, created_at, latest_revision) "
            "VALUES (?, ?, ?, 1)",
            (dataset_id, layer, now),
        )
        conn.execute(
            "INSERT INTO revisions "
            "(dataset_id, revision, content_hash, feature_count, geojson, published_at) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (dataset_id, content_hash, feature_count, geojson_blob, now),
        )
    return {
        "id": dataset_id,
        "layer": layer,
        "revision": 1,
        "content_hash": content_hash,
        "feature_count": feature_count,
        "published_at": now,
    }


def publish_next_revision(
    conn: sqlite3.Connection,
    dataset_id: str,
    geojson_blob: bytes,
    content_hash: str,
    feature_count: int,
    expected_revision: int,
) -> dict[str, Any]:
    """Append an immutable revision using optimistic concurrency control.

    The head comparison, the revision insert and the head update run inside
    one cross-process ``BEGIN IMMEDIATE`` transaction, so concurrent appenders
    are serialised on the SQLite write lock: exactly one caller creates the
    next revision and every other caller gets ``RevisionConflict`` carrying
    the new head.  There is no window for an unmapped primary-key error and no
    possibility of an inserted revision without an updated head.
    """
    now = utc_now_iso()
    with write_transaction(conn):
        row = conn.execute(
            "SELECT latest_revision, layer FROM datasets WHERE id = ?",
            (dataset_id,),
        ).fetchone()
        if row is None:
            raise NotFound(dataset_id)
        current = row["latest_revision"]
        if current != expected_revision:
            raise RevisionConflict(current)
        next_revision = current + 1
        try:
            conn.execute(
                "INSERT INTO revisions "
                "(dataset_id, revision, content_hash, feature_count, geojson, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    dataset_id,
                    next_revision,
                    content_hash,
                    feature_count,
                    geojson_blob,
                    now,
                ),
            )
            cur = conn.execute(
                "UPDATE datasets SET latest_revision = ? "
                "WHERE id = ? AND latest_revision = ?",
                (next_revision, dataset_id, current),
            )
        except sqlite3.IntegrityError:
            # Defensive: the guarded UPDATE + BEGIN IMMEDIATE already make this
            # unreachable, but a duplicate (dataset_id, revision) must never
            # surface as an unmapped 500 -- report the head that won instead.
            winner = conn.execute(
                "SELECT latest_revision FROM datasets WHERE id = ?",
                (dataset_id,),
            ).fetchone()
            raise RevisionConflict(
                winner["latest_revision"] if winner is not None else next_revision - 1
            )
        if cur.rowcount != 1:
            winner = conn.execute(
                "SELECT latest_revision FROM datasets WHERE id = ?",
                (dataset_id,),
            ).fetchone()
            raise RevisionConflict(
                winner["latest_revision"] if winner is not None else next_revision - 1
            )
    return {
        "id": dataset_id,
        "layer": row["layer"],
        "revision": next_revision,
        "content_hash": content_hash,
        "feature_count": feature_count,
        "published_at": now,
    }


def list_revisions(conn: sqlite3.Connection, dataset_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT revision, content_hash, feature_count, published_at "
            "FROM revisions WHERE dataset_id = ? ORDER BY revision",
            (dataset_id,),
        )
    )


# ---------------------------------------------------------------------------
# Tile cache
# ---------------------------------------------------------------------------


def get_cached_tile(
    conn: sqlite3.Connection, dataset_id: str, revision: int, z: int, x: int, y: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT etag, body FROM tile_cache WHERE dataset_id=? AND revision=? "
        "AND z=? AND x=? AND y=?",
        (dataset_id, revision, z, x, y),
    ).fetchone()


def store_cached_tile(
    conn: sqlite3.Connection,
    dataset_id: str,
    revision: int,
    z: int,
    x: int,
    y: int,
    etag: str,
    body: bytes,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tile_cache "
        "(dataset_id, revision, z, x, y, etag, body) VALUES (?,?,?,?,?,?,?)",
        (dataset_id, revision, z, x, y, etag, body),
    )
    conn.commit()
