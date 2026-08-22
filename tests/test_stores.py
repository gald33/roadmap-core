"""The store interface, and the SQLite implementation of it.

Written against a real file on disk rather than ``:memory:``, because two of the
properties under test are not observable in a single in-memory connection: the
claim transaction has to exclude a *second process's* claim, and the store has to
be created on first open by whoever gets there first. An in-memory database is
private to its connection, so it would pass both by having no second party.

Nothing here imports Lucille. That is the point of the package, and
``roadmap-core-tests.yml`` asserts it by failing if ``app``, ``fastapi``,
``sqlalchemy`` or ``yaml`` can be imported at all in the job that runs these.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from roadmap_core import store
from roadmap_core.stores import (
    ApiStore,
    ClaimRefused,
    LocalStore,
    Pruned,
    StoreError,
    Unsupported,
)

#: The item dict every caller downstream is written against. Stated as a literal
#: rather than derived from anything, so that a field quietly disappearing from
#: ``_row_to_item`` fails here — and so that the corresponding backend test can
#: assert ``crud.roadmap.to_dict`` produces this same set without either file
#: importing the other.
ITEM_FIELDS = {
    "key", "title", "status", "arc", "priority", "claimed_by", "claimed_at",
    "blocked_on", "defer_reason", "related_to", "artifacts", "refs", "tickets",
    "done_at", "done_version", "evidence", "evidence_checked_at",
}


@pytest.fixture()
def db(tmp_path):
    with LocalStore(tmp_path / "roadmap.db") as local:
        yield local


def _file(tmp_path):
    return tmp_path / "roadmap.db"


# --- the floor promise -------------------------------------------------------


def test_a_store_appears_where_there_was_no_server(tmp_path):
    """The claim that makes the package adoptable: nothing to provision.

    No migration, no connection string, no token — a path that does not exist
    yet, and afterwards a file that holds a claimed item.
    """
    path = _file(tmp_path)
    assert not path.exists()

    with LocalStore(path) as local:
        local.upsert_item({"key": "alpha", "title": "Alpha"})
        row = local.claim("alpha", by="claude/some-branch")

    assert path.exists()
    assert row["claimed_by"] == "claude/some-branch"
    assert row["status"] == "claimed"


def test_local_item_shape_is_the_documented_shape(db):
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    assert set(db.items()["alpha"]) == ITEM_FIELDS


def test_lists_and_dicts_survive_the_round_trip(db):
    db.upsert_item(
        {
            "key": "alpha",
            "title": "Alpha",
            "blocked_on": ["beta"],
            "related_to": [{"key": "gamma", "note": "read first"}],
            "artifacts": ["alembic"],
            "refs": ["scripts/roadmap.py"],
            "tickets": ["11111111-1111-1111-1111-111111111111"],
        }
    )
    item = db.items()["alpha"]
    assert item["blocked_on"] == ["beta"]
    assert item["related_to"] == [{"key": "gamma", "note": "read first"}]
    assert item["tickets"] == ["11111111-1111-1111-1111-111111111111"]


def test_an_omitted_list_reads_back_as_a_list_not_a_string(db):
    """The JSON columns default to the string ``'[]'`` in DDL. A reader that got
    that string instead of a list would still be truthy, so every ``if
    item["blocked_on"]`` in the graph would silently invert."""
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    item = db.items()["alpha"]
    for field in ("blocked_on", "related_to", "artifacts", "refs", "tickets"):
        assert item[field] == [], field
        assert isinstance(item[field], list), field


# --- claim ------------------------------------------------------------------


def test_claim_refuses_an_unknown_key(db):
    with pytest.raises(ClaimRefused, match="no such item"):
        db.claim("nope", by="claude/x")


def test_claim_refuses_a_done_item(db):
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.set_status("alpha", "done")
    with pytest.raises(ClaimRefused, match="already done"):
        db.claim("alpha", by="claude/x")


def test_claim_refuses_somebody_elses_and_names_them(db):
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.claim("alpha", by="claude/first")
    with pytest.raises(ClaimRefused, match="claimed by claude/first"):
        db.claim("alpha", by="claude/second")


def test_force_takes_a_held_claim(db):
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.claim("alpha", by="claude/first")
    row = db.claim("alpha", by="claude/second", force=True)
    assert row["claimed_by"] == "claude/second"


def test_reclaiming_your_own_item_is_not_a_race_you_lose(db):
    """A session that claims twice — a retry, a resumed loop — must not be told
    it is competing with itself."""
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.claim("alpha", by="claude/same")
    row = db.claim("alpha", by="claude/same")
    assert row["claimed_by"] == "claude/same"


def test_two_processes_racing_one_item_produce_exactly_one_winner(tmp_path):
    """The property the whole module exists for, tested by actually racing.

    Two connections, two threads, one barrier so they collide on purpose. The
    guarantee is not "the right one wins" — either may — it is that they do not
    BOTH win, which is the failure two file writes could produce and the reason
    CLAUDE.md §17 moved claims into a transaction in the first place.
    """
    path = _file(tmp_path)
    with LocalStore(path) as setup:
        setup.upsert_item({"key": "alpha", "title": "Alpha"})

    ready = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def contend(who: str) -> None:
        with LocalStore(path) as local:
            ready.wait()
            try:
                local.claim("alpha", by=who)
                result = ("won", who)
            except ClaimRefused:
                result = ("refused", who)
            except sqlite3.OperationalError as exc:  # pragma: no cover - a real bug
                result = (f"locked: {exc}", who)
        with lock:
            outcomes.append(result)

    threads = [
        threading.Thread(target=contend, args=(f"claude/{n}",)) for n in ("one", "two")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    won = [who for verdict, who in outcomes if verdict == "won"]
    refused = [who for verdict, who in outcomes if verdict == "refused"]
    assert len(outcomes) == 2, outcomes
    assert len(won) == 1, f"expected exactly one winner, got {outcomes}"
    assert len(refused) == 1, f"the loser must be told, got {outcomes}"

    with LocalStore(path) as check:
        assert check.items()["alpha"]["claimed_by"] == won[0]


# --- release ----------------------------------------------------------------


def test_release_clears_the_claim(db):
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.claim("alpha", by="claude/x")
    row = db.release("alpha")
    assert row is not None
    assert row["claimed_by"] is None
    assert row["claimed_at"] is None


def test_release_recomputes_the_status_instead_of_guessing_ready(db):
    """An item whose dependency is unfinished is ``blocked`` when the claim comes
    off. Writing ``ready`` would offer it to the next session as startable — the
    reason the API path calls ``derive_status`` here rather than assigning."""
    db.upsert_item({"key": "dep", "title": "Dependency"})
    db.upsert_item({"key": "alpha", "title": "Alpha", "blocked_on": ["dep"]})
    db.claim("alpha", by="claude/x")

    assert db.release("alpha")["status"] == "blocked"

    db.set_status("dep", "done")
    db.claim("alpha", by="claude/x")
    assert db.release("alpha")["status"] == "ready"


def test_releasing_an_unknown_key_is_none_not_an_error(db):
    assert db.release("nope") is None


# --- status, tombstones -----------------------------------------------------


def test_done_at_is_stamped_once_and_never_moved(db):
    """``done_at`` is the coordinate ``impact`` judges a linked ticket against.
    Re-stamping it on a second transition would move the line and turn an
    ``after`` — somebody reporting the bug again on a build that has the fix —
    into a ``before``, which reads as the fix having worked."""
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    first = db.set_status("alpha", "done")["done_at"]
    assert first is not None

    db.set_status("alpha", "verifying")
    again = db.set_status("alpha", "done")
    assert again["done_at"] == first


def test_set_status_on_an_unknown_key_is_an_error(db):
    with pytest.raises(StoreError, match="no such item"):
        db.set_status("nope", "done")


def test_delete_writes_a_tombstone_in_the_same_transaction(db):
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    assert db.delete_item("alpha") is True
    assert "alpha" not in db.items()
    assert db.pruned_keys() == frozenset({"alpha"})


def test_deleting_a_key_that_is_not_there_leaves_no_tombstone(db):
    assert db.delete_item("never-existed") is False
    assert db.pruned_keys() == frozenset()


def test_a_refile_clears_the_tombstone(db):
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.delete_item("alpha")
    assert db.clear_prune("alpha") is True
    assert db.pruned_keys() == frozenset()


def test_update_does_not_move_a_status_the_store_owns(db):
    """The mitigation the Postgres path took after a push from a stale checkout
    resurrected finished work as ``ready``. Status is store-owned on update, or
    the SQLite floor reintroduces a bug the deployed store already fixed."""
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.set_status("alpha", "done")
    db.upsert_item({"key": "alpha", "title": "Alpha", "status": "ready"})
    assert db.items()["alpha"]["status"] == "done"


def test_a_push_cannot_recreate_a_pruned_key(db):
    """The deeper half of the resurrection fix, on the SQLite side.

    A guard the API path has and this one lacked would mean adopting the floor
    silently reopens 2026-08-15: the files stay on ``main`` for as long as the
    prune's PR is open, and every push would refile what a prune spent.
    """
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.delete_item("alpha")
    with pytest.raises(Pruned, match="was pruned"):
        db.upsert_item({"key": "alpha", "title": "Alpha"})


def test_an_existing_item_is_updated_whatever_its_history(db):
    """Checked on CREATE only. Refusing an update would break a legitimately
    refiled item on its second push, one hour after it was filed."""
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.delete_item("alpha")
    db.clear_prune("alpha")
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.upsert_item({"key": "alpha", "title": "Alpha, retitled"})
    assert db.items()["alpha"]["title"] == "Alpha, retitled"


def test_an_unseen_key_still_creates(db):
    """The refusal is by name. A store that refused every create because some
    other key was pruned would have no authoring path at all — and writing
    ``roadmap/items/<key>.yaml`` and pushing IS the authoring path; there is no
    ``roadmap.py new``."""
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.delete_item("alpha")
    db.upsert_item({"key": "beta", "title": "Beta"})
    assert "beta" in db.items()


def test_create_honours_the_status_it_is_given(db):
    """The other half: the YAML files are the authoring format, so a brand-new
    item's declared status is the only one there is."""
    db.upsert_item({"key": "alpha", "title": "Alpha", "status": "deferred"})
    assert db.items()["alpha"]["status"] == "deferred"


# --- arcs -------------------------------------------------------------------


def test_arcs_round_trip_with_their_declared_state(db):
    db.upsert_arc(
        {
            "key": "tooling",
            "title": "Tooling",
            "state": "blocked",
            "state_evidence": "declared 2026-08-16, blocker is external",
            "refs": ["roadmap/README.md"],
            "narrative": "prose\nacross lines",
        }
    )
    arc = db.arcs()["tooling"]
    assert arc["state"] == "blocked"
    assert arc["refs"] == ["roadmap/README.md"]
    assert arc["narrative"] == "prose\nacross lines"


def test_an_arc_with_no_declared_state_reads_back_as_none(db):
    """``None`` and not ``"open"``: ``open`` is *derived* from the arc's items,
    and storing it would turn a fallback into a declaration nobody made."""
    db.upsert_arc({"key": "tooling", "title": "Tooling"})
    arc = db.arcs()["tooling"]
    assert arc["state"] is None
    assert arc["refs"] == []


def test_upsert_arc_updates_in_place(db):
    db.upsert_arc({"key": "tooling", "title": "Tooling"})
    db.upsert_arc({"key": "tooling", "title": "Tooling", "state": "closed"})
    assert len(db.arcs()) == 1
    assert db.arcs()["tooling"]["state"] == "closed"


# --- what the local store will not pretend to answer ------------------------


def test_impact_says_it_cannot_rather_than_returning_nothing(db):
    """An empty impact report reads as "no linked ticket was affected", which is
    a claim about the world. Refusing is the honest answer."""
    with pytest.raises(Unsupported, match="feedback tickets"):
        db.impact("alpha")


# --- api store --------------------------------------------------------------


class Recorder:
    """The host's caller, stubbed. Records rather than asserts, so each test
    states its own expectation."""

    def __init__(self, replies=None, raises=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self._replies = replies or {}
        self._raises = raises or ()

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path in self._raises:
            raise RuntimeError("backend unreachable")
        return self._replies.get(path)


def test_api_store_reads_items_by_key():
    call = Recorder({"/admin/roadmap": {"items": [{"key": "alpha", "title": "A"}]}})
    assert set(ApiStore(call).items()) == {"alpha"}


def test_api_store_reads_arcs_by_key():
    call = Recorder({"/admin/roadmap/arcs": {"arcs": [{"key": "tooling"}]}})
    assert set(ApiStore(call).arcs()) == {"tooling"}


def test_api_store_claim_sends_by_and_force():
    call = Recorder()
    ApiStore(call).claim("alpha", by="claude/x", force=True)
    assert call.calls == [
        ("POST", "/admin/roadmap/alpha/claim", {"by": "claude/x", "force": True})
    ]


def test_api_store_release_posts_and_returns_the_row():
    call = Recorder({"/admin/roadmap/alpha/release": {"key": "alpha", "status": "ready"}})
    assert ApiStore(call).release("alpha")["status"] == "ready"


def test_api_store_softens_an_unreachable_prune_read():
    """Failing closed here would turn a backend outage into a wall of false
    drift, because every caller uses this to EXEMPT a tombstoned absence."""
    call = Recorder(raises=("/admin/roadmap/prunes",))
    assert ApiStore(call).pruned_keys() == frozenset()


def test_api_store_holds_no_credentials():
    """The phase-03 boundary, asserted rather than described. If this class ever
    grows a token or a URL, auth has leaked out of the host and into the
    package, and reshaping it stops being a host-local change."""
    call = Recorder()
    api = ApiStore(call)
    leaked = [
        name
        for name in vars(api)
        if any(word in name.lower() for word in ("token", "jwt", "url", "auth", "key"))
    ]
    assert leaked == [], f"credentials leaked into ApiStore: {leaked}"


# --- the store module's own contract ----------------------------------------


def test_the_store_is_created_where_it_was_asked_for(tmp_path):
    """Not in a home directory nobody thinks to look in — beside the items it
    projects, so ``git status`` shows it."""
    path = tmp_path / "nested" / "roadmap.db"
    with LocalStore(path):
        pass
    assert path.exists()


def test_two_opens_of_one_path_see_the_same_rows(tmp_path):
    path = _file(tmp_path)
    with LocalStore(path) as first:
        first.upsert_item({"key": "alpha", "title": "Alpha"})
    with LocalStore(path) as second:
        assert "alpha" in second.items()


def test_json_columns_named_by_the_schema_are_the_ones_decoded(db):
    """``store.JSON_COLUMNS`` is the declared list; this asserts the reader
    actually decodes every item column on it, so adding a JSON column to the
    schema without teaching ``_row_to_item`` fails here rather than handing a
    caller a raw string."""
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    item = db.items()["alpha"]
    for column in store.JSON_COLUMNS["roadmap_items"]:
        assert not isinstance(item[column], str), f"{column} came back as raw JSON text"


# --- claim on the floor -------------------------------------------------------
#
# The store here is EPHEMERAL: CI deletes it and rebuilds it from the committed
# YAML every run. So a claim that a file records has to survive being pushed, or
# a held item renders as `ready` and the committed markdown can never match what
# CI generates. That is `a-claim-cannot-survive-the-floors-ci`, and these pin the
# ruling: authoritative on the floor, CREATE only, exactly like `status`.


def test_a_claim_in_the_file_survives_a_rebuild_of_the_store(db):
    db.upsert_item({
        "key": "alpha",
        "title": "Alpha",
        "status": "claimed",
        "claimed_by": "claude/some-branch",
        "claimed_at": "2026-08-22T10:00:00+00:00",
    })
    item = db.items()["alpha"]
    assert item["claimed_by"] == "claude/some-branch"
    assert item["claimed_at"] == "2026-08-22T10:00:00+00:00"
    assert item["status"] == "claimed"


def test_an_update_cannot_move_a_claim(db):
    """CREATE only, for the same reason `status` is.

    On update the store is the live record of who holds what — a push from a
    checkout that predates a release would otherwise hand the item back to a
    session that has already let go.
    """
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.claim("alpha", by="claude/live")

    db.upsert_item({
        "key": "alpha",
        "title": "Alpha",
        "claimed_by": "claude/stale-checkout",
        "claimed_at": "2026-01-01T00:00:00+00:00",
    })

    assert db.items()["alpha"]["claimed_by"] == "claude/live"


def test_an_unclaimed_file_creates_an_unclaimed_item(db):
    """The ordinary case: no claim block, no claim. Asserted because the create
    path now writes those columns explicitly rather than leaving them to the
    schema default, and `None` is the value that has to arrive."""
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    item = db.items()["alpha"]
    assert item["claimed_by"] is None
    assert item["claimed_at"] is None


def test_done_drops_the_claim(db):
    """The CLI has always printed "done also drops the claim". On the floor it
    did not — the served path clears the hold and this one did not, and nobody
    could see the difference because a claim never survived a rebuild anyway.

    Fixing claims-survive-a-rebuild is what made this visible: the notice about
    an unreleased hold started firing on an item that had just been finished.
    """
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.claim("alpha", by="claude/holder")

    row = db.set_status("alpha", "done")

    assert row["status"] == "done"
    assert row["claimed_by"] is None
    assert row["claimed_at"] is None


def test_a_status_that_is_not_done_leaves_the_claim_alone(db):
    """`verifying` explicitly does NOT drop it: the branch that shipped the work
    still owns confirming it landed."""
    db.upsert_item({"key": "alpha", "title": "Alpha"})
    db.claim("alpha", by="claude/holder")

    row = db.set_status("alpha", "verifying")

    assert row["claimed_by"] == "claude/holder"
