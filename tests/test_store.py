"""The store a fresh checkout gets, with nothing installed.

These run in the isolation job (`roadmap-core-tests.yml`), which installs only
this package — no backend, no SQLAlchemy, no PyYAML. That is the point: the
claim this file makes is *you can adopt the roadmap without adopting Lucille*,
and it can only be proved somewhere Lucille is absent.

The parity between this schema and the SQLAlchemy models lives in
`backend/tests/`, because asserting it needs both definitions importable. Here
we assert only what the package promises on its own.
"""

from __future__ import annotations

import sqlite3

import pytest

from roadmap_core import store


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "roadmap.db")
    yield conn
    conn.close()


def test_opening_a_store_that_does_not_exist_creates_it(tmp_path):
    """No migration step and no setup command.

    The failure this avoids is a tool that cannot be tried without first
    reading its installation instructions — which is most of the distance
    between "adoptable" and "not".
    """
    path = tmp_path / "nested" / "roadmap.db"
    assert not path.exists()

    conn = store.connect(path)
    try:
        assert path.exists(), "the parent directory must be created too"
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(store.TABLES) <= names
    finally:
        conn.close()


def test_opening_an_existing_store_is_not_destructive(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` on every open is only safe if it really is
    a no-op on the second one. A store that quietly reset itself would lose a
    backlog rather than fail loudly about it."""
    path = tmp_path / "roadmap.db"
    first = store.connect(path)
    first.execute(
        "INSERT INTO roadmap_items (id, key, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (store.new_id(), "an-item", "A title", "2026-01-01T00:00:00Z",
         "2026-01-01T00:00:00Z"),
    )
    first.close()

    second = store.connect(path)
    try:
        rows = second.execute("SELECT key, title FROM roadmap_items").fetchall()
        assert [(r["key"], r["title"]) for r in rows] == [("an-item", "A title")]
    finally:
        second.close()


def test_a_key_cannot_be_claimed_twice_by_two_rows(db):
    """`key` is the identifier every other surface uses — the CLI, the files,
    the audit. Two rows sharing one would make "the item" ambiguous in a system
    whose entire job is deciding who holds which item."""
    for table in ("roadmap_items", "roadmap_arcs"):
        cols = "(id, key, title, created_at, updated_at)"
        values = (store.new_id(), "dup", "t", "2026-01-01T00:00:00Z",
                  "2026-01-01T00:00:00Z")
        db.execute(f"INSERT INTO {table} {cols} VALUES (?,?,?,?,?)", values)  # noqa: S608
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                f"INSERT INTO {table} {cols} VALUES (?,?,?,?,?)",  # noqa: S608
                (store.new_id(), "dup", "t2", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00Z"),
            )


def test_the_defaults_that_let_a_row_be_written_with_four_fields(db):
    """Filing an item is the authoring path, not an API call, so the store has
    to accept a nearly-empty row and fill the rest. If these defaults were
    missing every caller would have to know the full column list."""
    db.execute(
        "INSERT INTO roadmap_items (id, key, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (store.new_id(), "sparse", "Sparse", "2026-01-01T00:00:00Z",
         "2026-01-01T00:00:00Z"),
    )
    row = db.execute("SELECT * FROM roadmap_items WHERE key='sparse'").fetchone()

    assert row["status"] == "ready"
    assert row["evidence"] == ""
    for column in ("blocked_on", "related_to", "artifacts", "refs", "tickets"):
        assert store.loads(row[column], None) == [], column


def test_json_columns_round_trip_through_the_helpers(db):
    """SQLite has no JSON type, so these are TEXT and the encoding is ours.
    A list that comes back as the string `"[1, 2]"` is the bug this prevents."""
    payload = {"blocked_on": ["a", "b"], "tickets": ["11111111-1111-1111-1111-111111111111"]}
    db.execute(
        "INSERT INTO roadmap_items (id, key, title, blocked_on, tickets, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (store.new_id(), "jsonful", "JSON", store.dumps(payload["blocked_on"]),
         store.dumps(payload["tickets"]), "2026-01-01T00:00:00Z",
         "2026-01-01T00:00:00Z"),
    )
    row = db.execute("SELECT * FROM roadmap_items WHERE key='jsonful'").fetchone()

    assert store.loads(row["blocked_on"], None) == payload["blocked_on"]
    assert store.loads(row["tickets"], None) == payload["tickets"]


def test_a_hand_edited_json_column_degrades_instead_of_raising(db):
    """This is a file on somebody's disk, and people edit those. A backlog
    reader should show the rest of the row rather than die on one field."""
    db.execute(
        "INSERT INTO roadmap_items (id, key, title, blocked_on, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (store.new_id(), "mangled", "Mangled", "not json at all",
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    row = db.execute("SELECT * FROM roadmap_items WHERE key='mangled'").fetchone()

    assert store.loads(row["blocked_on"], []) == []
    assert row["title"] == "Mangled", "the readable fields must survive"


def test_the_arc_index_exists_because_the_backlog_is_read_by_arc(db):
    names = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_roadmap_items_arc" in names


def test_write_ahead_logging_is_on(db):
    """The claim path is a read-modify-write that must exclude a concurrent
    one, and several agents in one checkout is the ordinary case here."""
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_columns_are_read_back_from_the_engine_not_the_ddl_string(db):
    """A check that compares the DDL to itself proves nothing; the interest is
    entirely in what a real engine made of it."""
    got = store.columns(db, "roadmap_items")
    assert got["key"] == "TEXT"
    assert "id" in got and "updated_at" in got


def test_the_package_still_needs_nothing_but_the_standard_library():
    """The property the whole extraction rests on, asserted where it can fail.

    `pyproject.toml` calls the empty dependency list load-bearing: a package
    that pulls in an ORM cannot be adopted by another repo without adopting
    Lucille with it. An import added here would keep the backend suite green
    and break only this job.

    Read from the import graph rather than by grepping the text. The first
    version of this grepped for the names and failed on this module's own
    docstring, which *discusses* SQLAlchemy precisely because it is explaining
    why it does not use it — a guard that cannot tell a mention from a
    dependency will be silenced by whoever it stops, and silencing it is
    exactly what it exists to prevent.
    """
    import ast
    import pathlib
    import sys

    import roadmap_core.graph as graph

    stdlib = set(sys.stdlib_module_names)
    for module in (store, graph):
        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

        outside = imported - stdlib - {"roadmap_core"}
        assert not outside, f"{module.__name__} imports non-stdlib {sorted(outside)}"
