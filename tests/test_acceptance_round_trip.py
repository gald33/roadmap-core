"""An item's ``acceptance`` survives push -> store -> pull.

Broken looks like (measured 2026-10-14 against roadmap-core 0.5.0, the served
Lucille store): ``cmd_push`` built its payload field by field with no
``acceptance``; the store's item shape had no such key (0 of 299 items carried
it); so an item filed by ``push`` alone reached ``main`` through the sync's
``pull`` without the criteria it was filed with, and nothing said so.

Every test here that talks to a store talks to a real one -- a real server over
HTTP, or a real SQLite file -- because the loss was at the seams between three
layers, and a unit test of any one of them passes while the round trip does not.
"""

from __future__ import annotations

import argparse
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("yaml", reason="the file paths need roadmap-core[files]")
import yaml  # noqa: E402

from roadmap_core import cli, server, store  # noqa: E402
from roadmap_core.stores import LocalStore  # noqa: E402

PROSE = "- `push` uploads `acceptance`.\n- a pull never erases it.\n"
CHECKLIST = ["push uploads acceptance", "a pull never erases it"]


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A scratch checkout. The ambient environment of a Lucille session carries
    the REAL served store's URL, token and tenant, so every one is removed:
    a test that fell through to them would write to production."""
    for name in ("ROADMAP_API_URL", "ROADMAP_API_TOKEN", "ROADMAP_TENANT",
                 "LUCILLE_ADMIN_JWT", "LUCILLE_BACKEND_URL", "ROADMAP_SOURCE"):
        monkeypatch.delenv(name, raising=False)
    items = tmp_path / "roadmap" / "items"
    items.mkdir(parents=True)
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    monkeypatch.setattr(cli, "ARCS_DIR", tmp_path / "roadmap" / "arcs")
    return items


@pytest.fixture
def served(tmp_path, monkeypatch, checkout):
    srv = server.make_server(tmp_path / "data", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    srv.registry.add_tenant("t")
    token = srv.registry.create_token("t", label="test", scopes=("read", "write"))[1]
    monkeypatch.setenv("ROADMAP_API_URL", base)
    monkeypatch.setenv("ROADMAP_API_TOKEN", token)
    yield srv
    srv.shutdown()
    srv.server_close()


def _write(items: Path, key: str, acceptance: Any = None, **extra: str) -> Path:
    doc: dict[str, Any] = {"id": key, "title": key, "status": "ready", **extra}
    if acceptance is not None:
        doc["acceptance"] = acceptance
    doc["evidence"] = "measured\n"
    path = items / f"{key}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def _push() -> None:
    assert cli.cmd_push(argparse.Namespace(source="db")) == 0


def _pull() -> None:
    assert cli.cmd_pull(argparse.Namespace(source="db")) == 0


# --- push -----------------------------------------------------------------


def test_push_sends_the_acceptance_it_was_filed_with(checkout, monkeypatch):
    _write(checkout, "alpha", PROSE)
    sent: list[dict] = []
    monkeypatch.setattr(cli, "_api", lambda m, p, payload=None: sent.append(payload or {}))
    monkeypatch.setattr(cli, "load_from_db", dict)

    _push()

    payload = next(p for p in sent if p.get("key") == "alpha")
    assert payload.get("acceptance") == PROSE


def test_push_omits_the_key_for_a_file_without_it_so_the_store_keeps_its_own(
        served, checkout):
    """The sync pushes from main before it pulls. An item on main without
    criteria that a branch just pushed with them must not be wiped by that push."""
    path = _write(checkout, "alpha", PROSE)
    _push()
    doc = yaml.safe_load(path.read_text())
    del doc["acceptance"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))

    _push()

    assert cli.load_from_db()["alpha"]["acceptance"] == PROSE


def test_an_explicit_null_in_the_file_does_clear_the_stored_acceptance(served, checkout):
    path = _write(checkout, "alpha", PROSE)
    _push()
    doc = yaml.safe_load(path.read_text())
    doc["acceptance"] = None
    path.write_text(yaml.safe_dump(doc, sort_keys=False))

    _push()

    assert cli.load_from_db()["alpha"]["acceptance"] is None


def test_the_loader_keeps_both_shapes_authored_in_the_wild(checkout):
    """Measured 2026-10-15 on Lucille `origin/main`: 176 of 309 item files carry
    `acceptance` -- a string in 103, a list in 73. A loader that normalised one
    into the other would rewrite a third to a half of them."""
    _write(checkout, "prose", PROSE)
    _write(checkout, "listy", CHECKLIST)
    _write(checkout, "none")
    got = cli.load_from_files()
    assert got["prose"]["acceptance"] == PROSE
    assert got["listy"]["acceptance"] == CHECKLIST
    assert "acceptance" not in got["none"], "a file that says nothing stays silent"


# --- the store keeps it ----------------------------------------------------


@pytest.mark.parametrize("shape", [PROSE, CHECKLIST], ids=["prose", "list"])
def test_a_served_store_returns_what_was_pushed(served, checkout, shape):
    _write(checkout, "alpha", shape)
    _push()
    assert cli.load_from_db()["alpha"]["acceptance"] == shape


def test_the_item_shape_always_names_acceptance_even_when_unset(served, checkout):
    """Absence of the key must not be how "none" is spelled: a reader cannot tell
    a store that predates the field from an item that has no criteria."""
    _write(checkout, "alpha")
    _push()
    assert cli.load_from_db()["alpha"]["acceptance"] is None


def test_a_push_from_a_client_that_does_not_send_it_leaves_the_stored_one(served, checkout):
    """An un-upgraded checkout's full PUT omits the key entirely. Reading
    "absent" as "clear" would let every old client erase what a new one filed."""
    _write(checkout, "alpha", PROSE)
    _push()
    with LocalStore(served.registry.tenants_dir / "t.db") as direct:
        direct.upsert_item({"key": "alpha", "title": "alpha", "evidence": "measured"})
    assert cli.load_from_db()["alpha"]["acceptance"] == PROSE


def test_the_server_refuses_an_acceptance_that_is_neither_text_nor_a_list(served, checkout):
    with pytest.raises(SystemExit, match="acceptance"):
        cli._api("PUT", "/admin/roadmap/alpha", {
            "key": "alpha", "title": "alpha", "acceptance": {"nope": 1}})


def test_a_store_made_before_the_column_existed_gains_it(tmp_path):
    """The served store is a file that outlives every release. CREATE TABLE IF NOT
    EXISTS does not add a column to a table that is already there, so without an
    additive step the first push after the upgrade is an INSERT into a column the
    deployed file does not have."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE roadmap_items (id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, "
        "title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready', arc TEXT, "
        "priority TEXT, claimed_by TEXT, claimed_at TEXT, "
        "blocked_on TEXT NOT NULL DEFAULT '[]', defer_reason TEXT, "
        "related_to TEXT NOT NULL DEFAULT '[]', artifacts TEXT NOT NULL DEFAULT '[]', "
        "refs TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '', "
        "evidence_checked_at TEXT, done_at TEXT, done_version TEXT, "
        "tickets TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO roadmap_items (id, key, title, created_at, updated_at) "
                 "VALUES ('1', 'old', 'Old', 'x', 'x')")
    conn.commit()
    conn.close()

    with LocalStore(path) as local:
        assert local.items()["old"]["acceptance"] is None, "an old row is kept, unset"
        local.upsert_item({"key": "old", "title": "Old", "acceptance": CHECKLIST})
        assert local.items()["old"]["acceptance"] == CHECKLIST
        assert "acceptance" in store.columns(local.connection, "roadmap_items")


# --- pull -------------------------------------------------------------------


@pytest.mark.parametrize("shape", [PROSE, CHECKLIST], ids=["prose", "list"])
def test_pull_writes_acceptance_into_a_file_it_creates(served, checkout, shape):
    """The case the item is about: filed by push alone, reaches main by pull."""
    _write(checkout, "alpha", shape)
    _push()
    (checkout / "alpha.yaml").unlink()

    _pull()

    assert yaml.safe_load((checkout / "alpha.yaml").read_text())["acceptance"] == shape
    assert cli.load_from_files()["alpha"]["acceptance"] == shape, "and it loads back"


def test_pull_fills_an_existing_file_that_lacks_it(served, checkout):
    path = _write(checkout, "alpha", PROSE)
    _push()
    doc = yaml.safe_load(path.read_text())
    del doc["acceptance"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))

    _pull()

    after = path.read_text()
    assert yaml.safe_load(after)["acceptance"] == PROSE
    assert yaml.safe_load(after)["evidence"] == "measured\n", "evidence is not disturbed"
    assert after.count("acceptance:") == 1


def test_pull_never_erases_an_acceptance_the_store_does_not_have(served, checkout):
    """The first sync after the upgrade runs against rows that predate the field:
    all of them. Every file in the checkout that carries one must keep it."""
    path = _write(checkout, "alpha", PROSE)
    cli._api("PUT", "/admin/roadmap/alpha", {"key": "alpha", "title": "alpha"})
    assert cli.load_from_db()["alpha"]["acceptance"] is None
    before = path.read_text()

    _pull()

    assert path.read_text() == before
    assert cli.load_from_files()["alpha"]["acceptance"] == PROSE


def test_pull_does_not_overwrite_an_acceptance_a_person_edited_in_the_file(served, checkout):
    """Same rule as evidence: the file is the authored form, and the store's copy
    is usually the older one."""
    path = _write(checkout, "alpha", "old criteria\n")
    _push()
    _write(checkout, "alpha", "reworded in a PR\n")

    _pull()

    assert cli.load_from_files()["alpha"]["acceptance"] == "reworded in a PR\n"
    assert path.read_text().count("acceptance:") == 1


def test_a_prunes_repair_put_keeps_the_acceptance(served, checkout):
    """`prune` re-PUTs the authored fields of every item that depended on what it
    removed. That payload is a second place that lists fields by hand, so it is
    the next place that would lose this one."""
    _write(checkout, "dep")
    _write(checkout, "alpha", PROSE, blocked_on=["dep"])
    _push()
    (checkout / "dep.yaml").unlink()

    cli._release_dependents(["dep"])

    assert cli.load_from_db()["alpha"]["acceptance"] == PROSE
    assert cli.load_from_files()["alpha"]["acceptance"] == PROSE


@pytest.mark.parametrize("value", [
    "no trailing newline",
    "one trailing newline\n",
    "two trailing newlines\n\n",
    "  leading spaces on the first line\nsecond\n",
    "# starts like a comment\nPR #861 and key: value\n",
    "paragraph\n\nanother, after a blank line\n",
    ["plain", "with: a colon", "PR #861", "- dash", "multi\nline entry"],
], ids=["bare", "one-nl", "two-nl", "indented", "yaml-looking", "blank-line", "list"])
def test_the_rendered_file_loads_back_to_exactly_what_was_stored(checkout, value):
    """`render_item_yaml` is hand-rolled, not PyYAML, so each of these is a way
    for a pulled file to load as something other than what the store held."""
    item = {"key": "alpha", "title": "alpha", "status": "ready", "evidence": "e\n",
            "acceptance": value}
    (checkout / "alpha.yaml").write_text(cli.render_item_yaml(item))
    assert cli.load_from_files()["alpha"]["acceptance"] == value
    assert cli.load_from_files()["alpha"]["evidence"] == "e\n"


def _old_store(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE roadmap_items (id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, "
        "title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready', arc TEXT, "
        "priority TEXT, claimed_by TEXT, claimed_at TEXT, "
        "blocked_on TEXT NOT NULL DEFAULT '[]', defer_reason TEXT, "
        "related_to TEXT NOT NULL DEFAULT '[]', artifacts TEXT NOT NULL DEFAULT '[]', "
        "refs TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '', "
        "evidence_checked_at TEXT, done_at TEXT, done_version TEXT, "
        "tickets TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()


def test_losing_the_race_to_add_the_column_is_not_an_error(tmp_path, monkeypatch):
    """Deterministic form of the race: this opener read the columns BEFORE another
    one added `acceptance`, so its ALTER hits "duplicate column name". Without the
    handler in `_add_missing_columns` that propagates and the open fails."""
    path = tmp_path / "raced.db"
    with LocalStore(path):
        pass                                   # the "winner": column now exists
    real = store.columns
    calls: list[str] = []

    def stale_first(conn, table):
        calls.append(table)
        got = real(conn, table)
        if len(calls) == 1:                    # the loser's stale read
            got = {k: v for k, v in got.items() if k != "acceptance"}
        return got

    monkeypatch.setattr(store, "columns", stale_first)
    with LocalStore(path) as local:            # must not raise
        assert "acceptance" in real(local.connection, "roadmap_items")


def test_many_openers_of_one_old_file_all_succeed(tmp_path):
    """The same race for real. Measured by the reviewer on PR #23 with the handler
    removed: 244 of 320 concurrent opens failed with 'duplicate column name'."""
    failures: list[BaseException] = []
    for round_no in range(5):
        path = tmp_path / f"old{round_no}.db"
        _old_store(path)
        barrier = threading.Barrier(16)

        def opener(path=path, barrier=barrier):
            try:
                barrier.wait()
                with LocalStore(path):
                    pass
            except BaseException as exc:  # noqa: BLE001 - the point is to collect them
                failures.append(exc)

        threads = [threading.Thread(target=opener) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert not failures, f"{len(failures)} of 80 concurrent opens failed: {failures[:2]}"


# --- follow-ups from review of #23 -------------------------------------------


def test_the_loader_and_validate_reject_a_file_with_a_repeated_top_level_key(checkout):
    """A branch pushes acceptance for an item already on main; the main sync
    appends the store's copy; a clean git merge leaves BOTH. PyYAML keeps the
    last, which can be the store's stale one, and says nothing."""
    path = _write(checkout, "alpha", "the branch's criteria\n")
    path.write_text(path.read_text() + "acceptance: |\n  the stale store copy\n")
    assert yaml.safe_load(path.read_text())["acceptance"] == "the stale store copy\n", (
        "premise: PyYAML really does take the later one without complaint")

    with pytest.raises(SystemExit) as loaded:
        cli.load_from_files()
    message = str(loaded.value)
    assert "alpha.yaml" in message and "'acceptance'" in message and "duplicate" in message

    with pytest.raises(SystemExit, match="duplicate top-level key"):
        cli.cmd_validate(argparse.Namespace(source="files"))


def test_a_nested_repeat_is_not_this_checks_business(checkout):
    """Top level only: a `related_to` list repeats `key:` once per entry, each in
    its own mapping, and that is not a duplicate."""
    (checkout / "alpha.yaml").write_text(
        "id: alpha\ntitle: alpha\nstatus: ready\nrelated_to:\n- key: a\n- key: b\n"
        "evidence: |\n  e\n")
    assert "alpha" in cli.load_from_files()


def test_a_prunes_repair_put_is_silent_for_a_file_without_the_key(served, checkout):
    """The silent-file rule (a file with no `acceptance` key must not clear the
    store's) holds in `push` and, separately, in the prune repair PUT, which
    builds its own payload by hand."""
    _write(checkout, "dep")
    path = _write(checkout, "alpha", PROSE, blocked_on=["dep"])
    _push()
    doc = yaml.safe_load(path.read_text())
    del doc["acceptance"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    (checkout / "dep.yaml").unlink()

    cli._release_dependents(["dep"])

    assert "acceptance" not in yaml.safe_load(path.read_text())
    assert cli.load_from_db()["alpha"]["acceptance"] == PROSE
