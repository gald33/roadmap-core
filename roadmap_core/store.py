"""The roadmap's own store: one SQLite file, opened or created on the spot.

**Why this exists at all.** The roadmap is adoptable by another repository only
if you can start using it without provisioning anything — no Postgres, no
migration runner, no server, no token. Today that promise is half-kept: the
read paths fall back to ``roadmap/items/*.yaml``, but every *write* goes through
``scripts/roadmap.py:_api()`` to the Lucille admin API, so the two commands the
tool exists for — ``claim`` and ``release`` — need a running backend. This
module is the storage half of closing that gap.

**Stdlib only, and that is the whole point.** ``pyproject.toml`` calls the
package's empty dependency list load-bearing, in these words: *"a package that
pulls in a web framework or an ORM could not be adopted by another repo without
adopting Lucille with it."* So this is ``sqlite3`` and nothing else. The
SQLAlchemy models in ``backend/app/models.py`` remain the Postgres deployment's
definition of the same tables; the two are held together by an explicit parity
test rather than by hope, for the same reason ``STALE_CLAIM_DAYS`` is duplicated
with a test rather than imported.

**Create, don't migrate.** A fresh checkout gets its schema from ``CREATE TABLE
IF NOT EXISTS`` on first open. Alembic stays a concern of the Postgres path,
where an existing deployment has rows to preserve; it is not a prerequisite for
running the tool, because a tool you must run a migration framework to try is
not one you can adopt in an afternoon.

Column types follow SQLite's actual storage rather than pretending otherwise:
UUIDs and timestamps are TEXT (ISO 8601, UTC), and the JSON columns are TEXT
holding JSON. That is what ``JSON().with_variant(JSONB, "postgresql")`` already
decays to on this dialect, so the two definitions agree on the value seen by a
reader even where they disagree on the declared type.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_PATH",
    "SCHEMA",
    "TABLES",
    "JSON_COLUMNS",
    "connect",
    "columns",
    "new_id",
]

#: Where a standalone checkout keeps its store. Beside the items it projects,
#: so a clone of the repository carries its own backlog and `git status` shows
#: the store as an untracked file rather than hiding it under a home directory
#: nobody thinks to look in.
DEFAULT_PATH = Path("roadmap") / "roadmap.db"

#: The one place the shape is written down for this dialect. Kept in the order
#: the SQLAlchemy models declare, so the parity test's failure output reads as a
#: diff rather than a puzzle.
SCHEMA: dict[str, str] = {
    "roadmap_items": """
        CREATE TABLE IF NOT EXISTS roadmap_items (
            id                  TEXT    PRIMARY KEY,
            key                 TEXT    NOT NULL UNIQUE,
            title               TEXT    NOT NULL,
            status              TEXT    NOT NULL DEFAULT 'ready',
            arc                 TEXT,
            priority            TEXT,
            claimed_by          TEXT,
            claimed_at          TEXT,
            blocked_on          TEXT    NOT NULL DEFAULT '[]',
            defer_reason        TEXT,
            related_to          TEXT    NOT NULL DEFAULT '[]',
            artifacts           TEXT    NOT NULL DEFAULT '[]',
            refs                TEXT    NOT NULL DEFAULT '[]',
            evidence            TEXT    NOT NULL DEFAULT '',
            evidence_checked_at TEXT,
            done_at             TEXT,
            done_version        TEXT,
            tickets             TEXT    NOT NULL DEFAULT '[]',
            created_at          TEXT    NOT NULL,
            updated_at          TEXT    NOT NULL
        )
    """,
    "roadmap_arcs": """
        CREATE TABLE IF NOT EXISTS roadmap_arcs (
            id             TEXT NOT NULL PRIMARY KEY,
            key            TEXT NOT NULL UNIQUE,
            title          TEXT NOT NULL,
            state          TEXT,
            state_evidence TEXT NOT NULL DEFAULT '',
            refs           TEXT NOT NULL DEFAULT '[]',
            narrative      TEXT NOT NULL DEFAULT '',
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        )
    """,
    "roadmap_prunes": """
        CREATE TABLE IF NOT EXISTS roadmap_prunes (
            id        TEXT NOT NULL PRIMARY KEY,
            key       TEXT NOT NULL UNIQUE,
            pruned_at TEXT NOT NULL
        )
    """,
}

TABLES = tuple(SCHEMA)

#: Indexes the Postgres side declares on the column rather than in DDL. Only
#: `arc` is indexed there; `key` is covered by its UNIQUE constraint on both
#: sides, so adding one here would be a second index for the same lookup.
INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_roadmap_items_arc ON roadmap_items (arc)",
)

#: Columns holding JSON. Named rather than inferred so a reader knows which
#: values need decoding without consulting the other definition, and so the
#: parity test can assert both sides agree on which those are.
JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "roadmap_items": (
        "blocked_on", "related_to", "artifacts", "refs", "done_version", "tickets",
    ),
    "roadmap_arcs": ("refs",),
    "roadmap_prunes": (),
}


def new_id() -> str:
    """A fresh row id.

    Python-side, matching the models' ``default=uuid.uuid4`` rather than a
    ``gen_random_uuid()`` server default. That choice looks like a worse version
    of the Postgres one until you try to run on SQLite, which has neither — see
    ``test_roadmap_dialect_portability.py``, which exists to stop it being
    "tidied" back.
    """
    return str(uuid.uuid4())


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the store, creating it and its schema if this is the first time.

    No migration step and no setup command: the failure mode this avoids is a
    tool that cannot be tried without reading its installation instructions.

    WAL because the claim path is a read-modify-write that must exclude a
    concurrent one, and the rollback journal serialises readers against it
    too — several agents in one checkout is the ordinary case here, not the
    exotic one. ``foreign_keys`` is on for correctness even though these three
    tables declare none between them: the roadmap deliberately holds ticket
    references as opaque ids with no constraint, and a future table that does
    declare one should not silently get an unenforced version of it.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    for ddl in SCHEMA.values():
        conn.execute(ddl)
    for ddl in INDEXES:
        conn.execute(ddl)
    return conn


def columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """Column name -> declared type, as the database actually has it.

    Read back from the file rather than parsed out of ``SCHEMA`` above: a test
    that checks the DDL string against itself proves nothing, and the interest
    here is entirely in what a real engine made of it.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608 - fixed names
    return {row["name"]: row["type"] for row in rows}


def loads(value: Any, default: Any) -> Any:
    """Decode a JSON column, tolerating a value that was never encoded.

    A column can hold a bare string in a store somebody edited by hand, and a
    backlog reader should show the rest of the row rather than raise on the one
    field it could not parse.
    """
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> str:
    """Encode a JSON column. Compact and key-sorted, so two writes of the same
    value produce the same bytes and a diff of the store is readable."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
