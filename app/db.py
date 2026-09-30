"""SQLite persistence for datasets, immutable revisions and the tile cache."""
from __future__ import annotations

import sqlite3
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
    # isolation_level=None puts the driver in autocommit mode so that every
    # multi-statement write is wrapped in an *explicit* transaction.  Publish
    # paths use BEGIN IMMEDIATE (see ``immediate_transaction``), which acquires
    # the SQLite RESERVED lock up front; this serialises the
    # compare-head / insert-revision / update-head sequence across separate
    # OS processes (e.g. multiple uvicorn workers sharing one DB file),
    # whereas a process-local threading lock cannot.
    conn = sqlite3.connect(
        settings.db_path,
        timeout=30,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    import os

    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in a cross-process write transaction.

    ``BEGIN IMMEDIATE`` takes the RESERVED lock immediately, so two concurrent
    publishers cannot both read the old head inside overlapping transactions:
    the second one blocks (up to ``busy_timeout``) until the first commits,
    then re-reads the new head and fails the revision check with 409 instead
    of colliding on the revisions primary key.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


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
    try:
        with immediate_transaction(conn):
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
    except sqlite3.IntegrityError:
        # Safety net: BEGIN IMMEDIATE serialises creators, so a duplicate id can
        # only happen if two callers generated the same random UUID4.
        raise RevisionConflict(0)
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

    The head comparison, the ``revisions`` insert and the ``datasets`` head
    update run in one BEGIN IMMEDIATE transaction, so the compare-and-set is
    atomic across processes sharing this database: exactly one concurrent
    publisher with the same ``expected_revision`` wins; the rest get
    ``RevisionConflict`` with the new head.
    """
    now = utc_now_iso()
    try:
        with immediate_transaction(conn):
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
            conn.execute(
                "UPDATE datasets SET latest_revision = ? WHERE id = ?",
                (next_revision, dataset_id),
            )
    except sqlite3.IntegrityError:
        # Defence in depth: with the IMMEDIATE transaction above the head
        # check already guarantees a free (dataset_id, revision) key, so this
        # only triggers if a writer bypassed that path.  Re-read the head and
        # report a normal 409 instead of leaking the database error.
        head = conn.execute(
            "SELECT latest_revision FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
        raise RevisionConflict(head["latest_revision"] if head else 0)
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
