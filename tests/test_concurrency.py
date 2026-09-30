"""Cross-process revision race.

Two independent worker *processes* share one SQLite database file and POST a
new revision with the same ``expected_revision``.  A ``threading.Lock`` (the
old implementation) cannot serialise separate processes: both read the stale
head and the loser trips the ``revisions`` primary key, surfacing as an
unmapped 500.  The fix makes the compare-and-set atomic in a cross-process
``BEGIN IMMEDIATE`` transaction.

The first test spawns real OS processes.  Each spawned worker is a fresh
interpreter with its own connection and no shared in-process state; it only
inherits ``VT_DB_PATH`` (set by ``conftest``) and therefore hits the exact
same database file the HTTP tests use.  The HTTP tests verify the routed
201/409 status codes and JSON bodies.
"""
import multiprocessing as mp
import os
import threading


def _append_in_worker(dataset_id, expected, barrier, queue):
    """Body of a spawned worker: private connection, one append attempt."""
    from app import db

    with db.connection() as conn:
        try:
            barrier.wait(30)
            result = db.publish_next_revision(
                conn,
                dataset_id=dataset_id,
                geojson_blob=b'{"type":"FeatureCollection","features":[]}',
                content_hash="worker-hash",
                feature_count=1,
                expected_revision=expected,
            )
            queue.put(("created", result["revision"]))
        except db.RevisionConflict as exc:
            queue.put(("conflict", exc.current_revision))
        except Exception as exc:  # pragma: no cover - the bug under test
            queue.put(("unexpected", f"{type(exc).__name__}: {exc}"))


def test_cross_process_appends_one_created_rest_conflict(client, publish):
    # Seed via the HTTP API so revision 1 exists in the shared test database.
    dataset_id = publish()["id"]

    from app import db

    n_workers = 4
    ctx = mp.get_context("spawn")

    for _trial in range(10):
        with db.connection() as conn:
            head = conn.execute(
                "SELECT latest_revision FROM datasets WHERE id = ?",
                (dataset_id,),
            ).fetchone()[0]

        barrier = ctx.Barrier(n_workers)
        queue = ctx.Queue()
        procs = [
            ctx.Process(
                target=_append_in_worker,
                args=(dataset_id, head, barrier, queue),
            )
            for _ in range(n_workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            assert p.exitcode == 0, f"worker died with exitcode {p.exitcode}"

        outcomes = sorted(queue.get() for _ in range(n_workers))
        kinds = [kind for kind, _ in outcomes]
        # No unmapped IntegrityError: exactly one create, the rest conflicts.
        assert kinds == ["conflict"] * (n_workers - 1) + ["created"], outcomes
        # Winner creates exactly head+1; every loser is told the same new head.
        assert all(revision == head + 1 for _, revision in outcomes), outcomes

        with db.connection() as conn:
            new_head = conn.execute(
                "SELECT latest_revision FROM datasets WHERE id = ?",
                (dataset_id,),
            ).fetchone()[0]
            n_revisions = conn.execute(
                "SELECT count(*) FROM revisions WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()[0]
        # Head advanced by exactly one; no extra or half-committed revisions.
        assert new_head == head + 1
        assert n_revisions == new_head


# ---------------------------------------------------------------------------
# HTTP-level mapping (in-process): the routed status codes and JSON bodies.
# ---------------------------------------------------------------------------


def test_concurrent_appends_over_http(client, publish, sample_collection):
    ds = publish()["id"]

    def post_revision(out, idx, barrier):
        barrier.wait()
        out[idx] = client.post(
            f"/datasets/{ds}/revisions",
            files={"file": ("d.geojson", sample_collection, "application/geo+json")},
            data={"expected_revision": "1"},
        )

    barrier = threading.Barrier(3)
    responses: dict[int, object] = {}
    threads = [
        threading.Thread(target=post_revision, args=(responses, i, barrier))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(r.status_code for r in responses.values())
    assert statuses == [201, 409, 409], [
        (r.status_code, r.text) for r in responses.values()
    ]

    created = [r for r in responses.values() if r.status_code == 201][0]
    assert created.json()["revision"] == 2

    for resp in responses.values():
        if resp.status_code == 409:
            assert resp.json() == {
                "error": "revision conflict: expected_revision does not match current head",
                "dataset_id": ds,
                "expected_revision": 1,
                "current_revision": 2,
            }

    meta = client.get(f"/datasets/{ds}").json()
    assert meta["latest_revision"] == 2
    assert [r["revision"] for r in meta["revisions"]] == [1, 2]


def test_historical_revision_still_readable_after_race(client, publish, sample_collection):
    ds = publish()["id"]

    # Pin a tile of revision 1 before the race.
    old = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt?revision=1")
    assert old.status_code == 200
    old_etag = old.headers["etag"]

    barrier = threading.Barrier(2)
    responses: dict[int, object] = {}

    def post_revision(out, idx):
        barrier.wait()
        out[idx] = client.post(
            f"/datasets/{ds}/revisions",
            files={"file": ("d.geojson", sample_collection, "application/geo+json")},
            data={"expected_revision": "1"},
        )

    threads = [
        threading.Thread(target=post_revision, args=(responses, i)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.status_code for r in responses.values()) == [201, 409]

    # The new head serves revision 2 ...
    head = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt")
    assert head.status_code == 200
    assert head.headers["x-revision"] == "2"

    # ... and the historical revision 1 tile is still byte-identical.
    again = client.get(f"/datasets/{ds}/tiles/0/0/0.mvt?revision=1")
    assert again.status_code == 200
    assert again.headers["x-revision"] == "1"
    assert again.headers["etag"] == old_etag
