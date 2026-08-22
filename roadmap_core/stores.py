"""Where the roadmap's writes go — a local SQLite file, or somebody's HTTP API.

**The half of the floor promise that is still unmet.** ``store.py`` gave a fresh
checkout a schema with nothing to provision, but every *write* in
``scripts/roadmap.py`` still goes through ``_api()`` to Lucille's admin API. So
the two commands the tool exists for — ``claim`` and ``release``, the ones
CLAUDE.md §17 calls the thing that stops two sessions colliding — need a running
backend and an admin JWT. A tool whose coordination primitive requires the host
application's auth is not adoptable by another repository; it is a client of
Lucille. This module is where that stops being true.

**One interface, two implementations, and the reason the split is here rather
than in the CLI.** ``LocalStore`` is ``sqlite3`` and this file's own logic.
``ApiStore`` is a thin translation to HTTP paths. Both answer the same questions,
so a caller never branches on which it holds — which is what makes "no server"
a configuration rather than a code path with its own bugs.

**The claim rules live in ONE place, and it is not this file.** ``LocalStore``
reimplements the transaction ``backend/app/crud/roadmap.py:claim_item`` runs,
because SQLAlchemy cannot come along — but the *decisions* (refuse a done item,
refuse somebody else's claim unless forced, derive the status on release rather
than guessing ``ready``) are stated once in ``graph.derive_status`` and once in
the refusal messages below, held to the Postgres path's behaviour by
``test_stores.py``. Duplicated logic with a test that pins the duplication is
this package's established answer to "SQLAlchemy is not stdlib" — the same trade
``store.py`` made for ``SCHEMA`` and ``STALE_CLAIM_DAYS`` made before it.

**What ApiStore deliberately does NOT know.** It is constructed with a
``call(method, path, payload)`` callable and has no idea how that authenticates.
No token, no ``urllib``, no environment variable. The host injects its own
caller, so auth stays entirely a host concern — which is phase 03's subject and
is not settled here. Reshaping how the API path authenticates should not require
touching this module at all; if it does, this seam was drawn wrong.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Protocol

from . import store
from .graph import derive_status

__all__ = [
    "Store",
    "LocalStore",
    "ApiStore",
    "StoreError",
    "ClaimRefused",
    "Pruned",
    "Unsupported",
]


class StoreError(RuntimeError):
    """A store refused an operation, for a reason worth showing the operator."""


class ClaimRefused(StoreError):
    """A claim did not happen: no such item, already done, or held by another.

    Its own type because it is the one failure a caller may want to catch and
    keep going from — losing a race is an ordinary outcome, not a fault.
    """


class Pruned(StoreError):
    """A create was refused because the key is tombstoned.

    Its own type because ``push`` must treat it as *skipped, not failed*: for the
    ~27 hours between a prune and its PR merging, ``main`` still carries the
    files and every hourly push names keys the store has deliberately spent.
    Failing there would turn a correct refusal into an hourly red build, which
    gets learned as noise — an invisible failure traded for a disbelieved one.
    """


class Unsupported(StoreError):
    """This store cannot answer that at all.

    Distinct from a failure: ``impact`` needs Lucille's feedback tickets, and a
    standalone SQLite store does not have them and never will. Saying so beats
    returning an empty report that reads as "no tickets were affected".
    """


class Store(Protocol):
    """What ``scripts/roadmap.py`` needs from a place that holds the roadmap.

    Reads first, then the two writes that define the promise. Deliberately not
    the whole of ``crud/roadmap.py``: ``push``/``pull``/``prune`` are Lucille's
    file-and-store reconciliation machinery, tied to ``origin/main`` and to a
    deployment, and pulling them behind this interface would drag the host's
    workflow into the package. Those stay on the API path for now, and the
    boundary is stated rather than discovered.
    """

    def items(self) -> dict[str, dict[str, Any]]: ...

    def arcs(self) -> dict[str, dict[str, Any]]: ...

    def pruned_keys(self) -> frozenset[str]: ...

    def claim(self, key: str, *, by: str, force: bool = False) -> dict[str, Any]: ...

    def release(self, key: str) -> dict[str, Any] | None: ...


# --- local -------------------------------------------------------------------


def _utcnow_iso() -> str:
    """UTC, ISO 8601, with the offset.

    The offset is not decoration. ``DateTime(timezone=True)`` renders plain
    ``DATETIME`` on SQLite so the Postgres path's aware value comes back naive,
    and ``graph.claim_age_days`` subtracts against ``now()`` — where mixing naive
    and aware raises rather than skewing quietly. Writing the offset means a
    store this module created never depends on the reader's assumption about it.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    """One item row as the graph and the CLI already expect it.

    Field-for-field ``crud.roadmap.to_dict``, including the flat
    ``claimed_by``/``claimed_at`` rather than a nested ``claim`` block — the
    nesting is the *file* format, and converting between the two is
    ``scripts/roadmap.py``'s job, not the store's. Pinned by
    ``test_stores.py::test_local_item_shape_matches_the_api_shape``.
    """
    keys = row.keys()

    def json_col(name: str, default: Any) -> Any:
        return store.loads(row[name], default) if name in keys else default

    return {
        "key": row["key"],
        "title": row["title"],
        "status": row["status"],
        "arc": row["arc"],
        "priority": row["priority"],
        "claimed_by": row["claimed_by"],
        "claimed_at": row["claimed_at"],
        "blocked_on": json_col("blocked_on", []),
        "defer_reason": row["defer_reason"],
        "related_to": json_col("related_to", []),
        "artifacts": json_col("artifacts", []),
        "refs": json_col("refs", []),
        "tickets": json_col("tickets", []),
        "done_at": row["done_at"],
        "done_version": json_col("done_version", None),
        "evidence": row["evidence"] or "",
        "evidence_checked_at": row["evidence_checked_at"],
    }


def _row_to_arc(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "title": row["title"],
        "state": row["state"],
        "state_evidence": row["state_evidence"] or "",
        "refs": store.loads(row["refs"], []),
        "narrative": row["narrative"] or "",
    }


class LocalStore:
    """The roadmap in one SQLite file, with no server anywhere.

    **Claim on the floor.** This store takes a file's `claimed_by`/`claimed_at`
    when it CREATES an item, and ignores them on update. `ApiStore` never does.
    That asymmetry is deliberate and is the ruling recorded in
    `a-claim-cannot-survive-the-floors-ci`: here the store is EPHEMERAL — CI
    deletes it and rebuilds it from `roadmap/items/*.yaml` every run — so the
    file is the only durable record a claim has, and dropping it made a held item
    render as `ready` and `sync --check` fail for as long as anybody was working.
    Against a SERVED store the file is a projection of a store that outlives it,
    and honouring a claim from a stale checkout would recreate one the store had
    already released — the resurrection class `roadmap_prunes` exists to prevent.

    One field, two meanings, decided by which store you are holding. Stated here
    because it is not guessable from the field's name.

    Owns its connection and closes it, so a caller can use it as a context
    manager and a test does not leak file handles into the next test.
    """

    def __init__(self, path: str | Any | None = None) -> None:
        self._conn = store.connect(path)

    # -- lifecycle

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> LocalStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """For the host's own reconciliation code. Named rather than reached for
        through ``_conn`` so that using it shows up as a deliberate step outside
        the interface."""
        return self._conn

    # -- reads

    def items(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM roadmap_items ORDER BY key").fetchall()
        return {row["key"]: _row_to_item(row) for row in rows}

    def arcs(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM roadmap_arcs ORDER BY key").fetchall()
        return {row["key"]: _row_to_arc(row) for row in rows}

    def pruned_keys(self) -> frozenset[str]:
        rows = self._conn.execute("SELECT key FROM roadmap_prunes").fetchall()
        return frozenset(row["key"] for row in rows)

    # -- writes

    def upsert_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Create or update one item from a plain dict.

        ``status`` is honoured on CREATE and left alone on UPDATE, which is not
        an oversight — it is the mitigation the Postgres path took after a push
        from a checkout predating a ``prune`` resurrected finished work as
        ``ready``. Same rule here, or the SQLite floor reintroduces a bug the
        deployed store already fixed.

        A CREATE of a tombstoned key raises ``Pruned``, which is the deeper half
        of the same fix. From inside an upsert, "a key I have never seen" and "a
        key somebody deleted an hour ago" are the same request — nothing in the
        payload separates them — so the store has to be the one that remembers.
        An UPDATE is checked for nothing: an item that exists is updated normally
        whatever its history, or a legitimately refiled item breaks on its second
        push, an hour after it was filed.
        """
        key = item["key"]
        now = _utcnow_iso()
        existing = self._conn.execute(
            "SELECT * FROM roadmap_items WHERE key = ?", (key,)
        ).fetchone()

        payload = {
            "title": item.get("title") or key,
            "arc": item.get("arc"),
            "priority": item.get("priority"),
            "blocked_on": store.dumps(list(item.get("blocked_on") or [])),
            "defer_reason": item.get("defer_reason"),
            "related_to": store.dumps(list(item.get("related_to") or [])),
            "artifacts": store.dumps(list(item.get("artifacts") or [])),
            "refs": store.dumps(list(item.get("refs") or [])),
            "tickets": store.dumps(list(item.get("tickets") or [])),
            "evidence": item.get("evidence") or "",
            "updated_at": now,
        }

        if existing is None:
            if key in self.pruned_keys():
                raise Pruned(
                    f"{key} was pruned and will not be recreated by a push. "
                    "If it is genuinely being refiled, clear the tombstone first."
                )
            payload["key"] = key
            payload["id"] = store.new_id()
            payload["status"] = item.get("status") or "ready"
            payload["created_at"] = now
            # The claim, on CREATE only, for the same reason as `status` and with
            # the same asymmetry — see the class docstring's "claim on the floor".
            payload["claimed_by"] = item.get("claimed_by")
            payload["claimed_at"] = item.get("claimed_at")
            names = ", ".join(payload)
            marks = ", ".join("?" for _ in payload)
            self._conn.execute(
                f"INSERT INTO roadmap_items ({names}) VALUES ({marks})",  # noqa: S608
                tuple(payload.values()),
            )
        else:
            sets = ", ".join(f"{name} = ?" for name in payload)
            self._conn.execute(
                f"UPDATE roadmap_items SET {sets} WHERE key = ?",  # noqa: S608
                (*payload.values(), key),
            )
        return self.get_item(key) or {}

    def get_item(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM roadmap_items WHERE key = ?", (key,)
        ).fetchone()
        return _row_to_item(row) if row is not None else None

    def upsert_arc(self, arc: dict[str, Any]) -> dict[str, Any]:
        key = arc["key"]
        now = _utcnow_iso()
        existing = self._conn.execute(
            "SELECT id FROM roadmap_arcs WHERE key = ?", (key,)
        ).fetchone()
        values = (
            arc.get("title") or key,
            arc.get("state"),
            arc.get("state_evidence") or "",
            store.dumps(list(arc.get("refs") or [])),
            arc.get("narrative") or "",
            now,
        )
        if existing is None:
            self._conn.execute(
                "INSERT INTO roadmap_arcs (id, key, title, state, state_evidence, refs,"
                " narrative, updated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (store.new_id(), key, *values, now),
            )
        else:
            self._conn.execute(
                "UPDATE roadmap_arcs SET title = ?, state = ?, state_evidence = ?,"
                " refs = ?, narrative = ?, updated_at = ? WHERE key = ?",
                (*values, key),
            )
        row = self._conn.execute(
            "SELECT * FROM roadmap_arcs WHERE key = ?", (key,)
        ).fetchone()
        return _row_to_arc(row)

    def set_status(self, key: str, status: str) -> dict[str, Any]:
        """Move an item's status, stamping ``done_at`` the first time it is done.

        First time only: the stamp is the coordinate ``impact`` compares a
        ticket against, so overwriting it on a second transition would move the
        line a ticket is judged against and quietly turn an ``after`` into a
        ``before``.
        """
        row = self._conn.execute(
            "SELECT done_at FROM roadmap_items WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise StoreError(f"no such item: {key}")
        now = _utcnow_iso()
        done_at = row["done_at"] or (now if status == "done" else None)
        if status == "done":
            # Finishing drops the hold, matching the served path — and matching
            # what the CLI already prints ("done also drops the claim"), which it
            # printed here while nothing happened. Finishing is the one moment a
            # hold is definitely over, and releasing separately is the step that
            # never happens because nothing prompts it.
            self._conn.execute(
                "UPDATE roadmap_items SET status = ?, done_at = ?, updated_at = ?,"
                " claimed_by = NULL, claimed_at = NULL WHERE key = ?",
                (status, done_at, now, key),
            )
        else:
            self._conn.execute(
                "UPDATE roadmap_items SET status = ?, done_at = ?, updated_at = ?"
                " WHERE key = ?",
                (status, done_at, now, key),
            )
        return self.get_item(key) or {}

    def delete_item(self, key: str) -> bool:
        """Delete an item and remember that it was deleted.

        The tombstone is the whole point and is written in the same transaction:
        a store that forgets which keys are spent resurrects every pruned item
        on the next push, which is the 2026-08-15 failure reproduced through a
        storage backend instead of a race.
        """
        with _immediate(self._conn):
            cur = self._conn.execute("DELETE FROM roadmap_items WHERE key = ?", (key,))
            deleted = cur.rowcount > 0
            if deleted:
                self._conn.execute(
                    "INSERT OR IGNORE INTO roadmap_prunes (id, key, pruned_at)"
                    " VALUES (?, ?, ?)",
                    (store.new_id(), key, _utcnow_iso()),
                )
        return deleted

    def clear_prune(self, key: str) -> bool:
        """Un-tombstone a key, so a deliberate refile can recreate it."""
        cur = self._conn.execute("DELETE FROM roadmap_prunes WHERE key = ?", (key,))
        return cur.rowcount > 0

    # -- the two that matter

    def claim(self, key: str, *, by: str, force: bool = False) -> dict[str, Any]:
        """Stake an item, atomically.

        ``BEGIN IMMEDIATE`` is the whole mechanism. The check and the write are
        one transaction that takes the write lock on entry, so two processes
        racing the same key cannot both read "unclaimed" and both write — the
        property CLAUDE.md §17 promises of the DB path, kept here without a DB.
        A loser blocks for ``busy_timeout`` and then sees the winner's row.
        """
        with _immediate(self._conn):
            row = self._conn.execute(
                "SELECT * FROM roadmap_items WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                raise ClaimRefused(f"no such item: {key}")
            if row["status"] == "done":
                raise ClaimRefused(f"{key} is already done")
            if row["claimed_by"] and row["claimed_by"] != by and not force:
                raise ClaimRefused(
                    f"{key} is already claimed by {row['claimed_by']} "
                    f"(at {row['claimed_at']}). Pick something else, or force if you "
                    "know that session is gone."
                )
            now = _utcnow_iso()
            self._conn.execute(
                "UPDATE roadmap_items SET claimed_by = ?, claimed_at = ?, status = ?,"
                " updated_at = ? WHERE key = ?",
                (by, now, "claimed", now, key),
            )
        return self.get_item(key) or {}

    def release(self, key: str) -> dict[str, Any] | None:
        """Drop the claim and recompute the status from the graph.

        Recomputed, never set to ``ready``: an item whose dependency regressed
        while it was held is ``blocked``, and writing ``ready`` would offer it to
        the next session as startable. ``derive_status`` is the same function the
        API path calls, so the two cannot drift.
        """
        with _immediate(self._conn):
            row = self._conn.execute(
                "SELECT * FROM roadmap_items WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            by_key = self.items()
            released = dict(by_key[key], claimed_by=None, claimed_at=None)
            by_key[key] = released
            status = derive_status(released, by_key)
            now = _utcnow_iso()
            self._conn.execute(
                "UPDATE roadmap_items SET claimed_by = NULL, claimed_at = NULL,"
                " status = ?, updated_at = ? WHERE key = ?",
                (status, now, key),
            )
        return self.get_item(key)

    def impact(self, key: str) -> dict[str, Any]:
        raise Unsupported(
            "impact needs the host's feedback tickets, which a standalone store "
            "does not have. Point the CLI at the API source for this one command."
        )


class _immediate:
    """``BEGIN IMMEDIATE`` … ``COMMIT``/``ROLLBACK`` as a context manager.

    Hand-rolled because ``store.connect`` opens with ``isolation_level=None`` —
    autocommit — which is what lets this module choose its own transaction
    boundaries instead of inheriting sqlite3's implicit ones. ``IMMEDIATE``
    rather than ``DEFERRED`` so the write lock is taken before the SELECT: a
    deferred transaction upgrades on first write and can lose the race it was
    opened to win.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type: object, *_rest: object) -> None:
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")


# --- api ---------------------------------------------------------------------


class ApiStore:
    """The same questions, asked over HTTP.

    Constructed with the host's own caller, so this class holds no token, no
    base URL and no ``urllib`` import — see the module docstring on why that
    boundary is load-bearing for phase 03.
    """

    def __init__(self, call: Callable[..., Any]) -> None:
        self._call = call

    def __enter__(self) -> ApiStore:
        """A no-op, so a caller can ``with store_for(source)`` and not branch.

        There is nothing to close — but a caller forced to know which
        implementation it holds in order to clean up would be branching on the
        thing this interface exists to hide, and would leak a SQLite handle the
        first time somebody forgot.
        """
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def items(self) -> dict[str, dict[str, Any]]:
        data = self._call("GET", "/admin/roadmap") or {}
        return {item["key"]: item for item in data.get("items", [])}

    def arcs(self) -> dict[str, dict[str, Any]]:
        data = self._call("GET", "/admin/roadmap/arcs") or {}
        return {arc["key"]: arc for arc in data.get("arcs", [])}

    def pruned_keys(self) -> frozenset[str]:
        """Empty rather than fatal when the store cannot answer.

        Every caller uses this to SOFTEN a report — a tombstoned absence is not
        drift — so failing closed here would turn an unreachable backend into a
        wall of false divergence. Carried over from ``load_pruned_keys``, whose
        docstring says the same thing, because the reasoning belongs to the
        operation and not to the CLI that used to own it.
        """
        try:
            data = self._call("GET", "/admin/roadmap/prunes") or {}
        except Exception:  # noqa: BLE001 - the host's caller raises its own type
            return frozenset()
        return frozenset(p["key"] for p in data.get("prunes", []))

    def claim(self, key: str, *, by: str, force: bool = False) -> dict[str, Any]:
        return self._call(
            "POST", f"/admin/roadmap/{key}/claim", {"by": by, "force": force}
        ) or {}

    def release(self, key: str) -> dict[str, Any] | None:
        return self._call("POST", f"/admin/roadmap/{key}/release", {})

    def impact(self, key: str) -> dict[str, Any]:
        return self._call("GET", f"/admin/roadmap/{key}/impact") or {}
