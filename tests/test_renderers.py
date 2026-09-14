"""What the rendered surfaces say about an item's status — and what `prune`
refuses to destroy on the way out.

These three assertions exist because of one incident, on 2026-09-14, in the
repository that consumes this package. A session ran `roadmap list`, read a line
reading `now  engine-topic-id-populate  Populate topic_id on engine rows`, and
dispatched a worker session to do the work. The item had shipped weeks earlier.
~150k tokens went into re-fixing it.

Nothing was broken in the graph: `derive_status` returned `done`, the band header
said `DONE (18)`, and the committed `ROADMAP.md` listed it under Items with
`- **status:** done`. The defect was entirely in the *rendering* — status lived
somewhere the reader's eye and every mechanical reader (a grep, a truncated read,
a line pasted into a dispatch message) did not have to pass through.

So these are tests of rendered output, asserted as strings, which is normally a
smell. Here it is the point: the thing that failed was the string. A test of
`derive_status` would have been green throughout.

Synthetic fixtures only, per the rule the rest of the suite follows — no
committed `roadmap/items/`, no YAML, no store, nothing from any consumer.
"""

from __future__ import annotations

import argparse

import pytest

from roadmap_core import cli, graph


def _item(key: str, **over):
    base = {"key": key, "title": f"Title of {key}", "status": "ready",
            "blocked_on": [], "evidence": "x"}
    base.update(over)
    return base


# --- 1. `roadmap list`: status on the row, not only in the band header -------


@pytest.fixture
def listed(monkeypatch, capsys):
    """Run `cmd_list` over a fixture and hand back its stdout lines."""

    def run(by_key):
        monkeypatch.setattr(cli, "load", lambda source: by_key)
        monkeypatch.setattr(cli, "_offline_notice", lambda args: None)
        monkeypatch.setattr(cli, "_stale_claim_notice", lambda by_key: None)
        capsys.readouterr()
        cli.cmd_list(argparse.Namespace(source="files", json=False))
        return capsys.readouterr().out.splitlines()

    return run


def _row_for(lines: list[str], key: str) -> str:
    """The one output row for ``key``.

    Matched on the title rather than the key, because a key also appears in
    *another* item's row as the `← <dep>` blocked-on suffix.
    """
    rows = [ln for ln in lines if f"Title of {key}" in ln]
    assert len(rows) == 1, f"expected exactly one row for {key}, got {rows}"
    return rows[0]


def test_a_list_row_states_its_own_status(listed):
    """THE INCIDENT, as an assertion.

    The row for a done item must carry the word `done`. Band headers are read
    once and scrolled past; this line has to survive being read alone, because
    that is how it reached the session that acted on it.
    """
    shipped = _item("shipped", status="done", priority="now")
    lines = listed({"shipped": shipped})
    assert "done" in _row_for(lines, "shipped")


def test_a_done_row_is_not_confusable_with_the_top_of_the_queue(listed):
    """The specific collision: a `done` item and a `now` item rendered the same.

    10 of the consumer's 18 done items carried a `now`/`next` priority, so they
    rendered as `now  <key>  <present-tense defect title>` — the exact shape of
    the highest-priority startable work. Strip the keys and titles, which differ
    for unrelated reasons, and what is left must still differ.
    """
    by_key = {
        "shipped": _item("shipped", status="done", priority="now"),
        "startable": _item("startable", priority="now"),
    }
    lines = listed(by_key)
    done_row = _row_for(lines, "shipped")
    ready_row = _row_for(lines, "startable")

    def shape(row: str, key: str) -> str:
        return row.replace(key, "").replace(f"Title of {key}", "")

    assert shape(done_row, "shipped") != shape(ready_row, "startable"), (
        "a done row and a top-priority ready row are distinguishable only by "
        "their key and title — which is what let a session act on a finished "
        "item"
    )


def test_every_row_carries_a_status_not_only_the_finished_ones(listed):
    """A marker that appears conditionally makes ABSENCE carry meaning.

    That is the bug's own shape: an unmarked line read as open work. So the
    token is on every row, for every status the graph can derive.
    """
    by_key = {
        "ready-one": _item("ready-one"),
        "deferred-one": _item("deferred-one", defer_reason="too early"),
        "blocked-one": _item("blocked-one", blocked_on=["ready-one"]),
        "claimed-one": _item("claimed-one", claimed_by="claude/x"),
        "verifying-one": _item("verifying-one", status="verifying"),
        "done-one": _item("done-one", status="done"),
    }
    lines = listed(by_key)
    for key in by_key:
        statename = graph.derive_status(by_key[key], by_key)
        assert statename in _row_for(lines, key), (
            f"the row for {key} does not state its status ({statename})"
        )


def test_the_status_column_is_wide_enough_for_every_status():
    """`STATUS_WIDTH` is derived, so a longer status added later cannot silently
    unalign every surface that prints one."""
    for statename in graph.STATUSES:
        assert len(statename) <= graph.STATUS_WIDTH


# --- 2. the mermaid graph: a node states its own status ----------------------


def _mermaid(by_key) -> list[str]:
    block = graph.render_markdown(by_key).split("```mermaid")[1].split("```")[0]
    return [ln.strip() for ln in block.splitlines() if ln.strip()]


def _node_line(lines: list[str], key: str) -> str:
    node = key.replace("-", "_")
    rows = [ln for ln in lines if ln.startswith(f"{node}[")]
    assert len(rows) == 1, f"expected one node line for {key}, got {rows}"
    return rows[0]


def test_a_done_node_is_not_drawn_like_an_open_one():
    """Before this, all 82 nodes of the consumer's committed graph rendered
    identically whether `ready` or `done` — in the one artifact a session reads
    to decide what to pick up."""
    by_key = {
        "shipped": _item("shipped", status="done"),
        "startable": _item("startable"),
    }
    lines = _mermaid(by_key)
    done_line = _node_line(lines, "shipped")
    ready_line = _node_line(lines, "startable")

    def shape(line: str, key: str) -> str:
        return line.replace(key.replace("-", "_"), "").replace(f"Title of {key}", "")

    assert shape(done_line, "shipped") != shape(ready_line, "startable")
    assert "done" in done_line, "the done node's own line does not say so"


def test_every_node_line_carries_its_derived_status():
    """On the node's own line, so a grep or a one-line quote keeps it — the same
    property `cmd_list` needs, for the same reason."""
    by_key = {
        "ready-one": _item("ready-one"),
        "deferred-one": _item("deferred-one", defer_reason="too early"),
        "blocked-one": _item("blocked-one", blocked_on=["ready-one"]),
        "claimed-one": _item("claimed-one", claimed_by="claude/x"),
        "verifying-one": _item("verifying-one", status="verifying"),
        "done-one": _item("done-one", status="done"),
    }
    lines = _mermaid(by_key)
    for key in by_key:
        statename = graph.derive_status(by_key[key], by_key)
        assert _node_line(lines, key).endswith(f":::{statename}"), (
            f"the node line for {key} does not carry its status"
        )


def test_the_status_marking_is_readable_in_the_picture_too():
    """`:::done` serves whoever reads the source. The rendered diagram needs its
    own marker, because a stroke colour is a distinction a reader can miss and a
    glyph in the label is not."""
    lines = _mermaid({"shipped": _item("shipped", status="done")})
    assert "✓" in _node_line(lines, "shipped")


def test_every_status_has_a_style():
    """An unstyled node renders as the default, which reads as open — the same
    failure with a new status name on it."""
    lines = _mermaid({"a": _item("a")})
    for statename in graph.STATUSES:
        assert any(ln.startswith(f"classDef {statename} ") for ln in lines), (
            f"no classDef for {statename}"
        )


def test_status_marking_adds_no_clock_and_no_graph_wide_aggregate():
    """``render_markdown``'s standing constraint, checked against the thing just
    added to it.

    The file is regenerated wholesale and committed, so any line derived from a
    total differs between two branches that finished different items and
    conflicts on merge while saying nothing about what either changed. That is
    why the classes are attached PER NODE and the `classDef` lines are constant,
    rather than a single `class a,b,c done` roll-up — which would have been
    exactly that aggregate.

    Asserted by rendering two graphs that differ by one added item and checking
    that no line about styling moved.
    """
    base = {
        "one": _item("one", status="done"),
        "two": _item("two"),
    }
    grown = dict(base, three=_item("three", status="done"))

    def styling_lines(by_key):
        return [ln for ln in _mermaid(by_key) if ln.startswith(("classDef ", "class "))]

    assert styling_lines(base) == styling_lines(grown), (
        "a styling line changed when an unrelated item was added — that line is "
        "a graph-wide aggregate and will conflict between branches"
    )
    # And nothing anywhere in the block is a roll-up naming several nodes.
    assert not [ln for ln in _mermaid(base) if ln.startswith("class ")], (
        "a `class a,b,... done` roll-up is a single line that every branch "
        "finishing an item has to edit"
    )


# --- 3. `prune` withholds the ticket edge ------------------------------------


@pytest.fixture
def pruner(monkeypatch, capsys):
    """Run `cmd_prune` over a fixture, with everything that touches a store, a
    checkout or the network stubbed out. Returns (deleted keys, stdout, stderr).
    """

    def run(files, *, yes=True, include_ticket_linked=False):
        deleted: list[str] = []
        monkeypatch.setattr(cli, "prune_staleness_blockers", lambda: [])
        monkeypatch.setattr(cli, "load_from_files", lambda: {k: dict(v) for k, v in files.items()})
        monkeypatch.setattr(cli, "load_from_db", dict)
        monkeypatch.setattr(cli, "_api_delete_tolerant", lambda key: deleted.append(key) or "ok")
        monkeypatch.setattr(cli, "item_path", lambda key: _MissingPath())
        monkeypatch.setattr(cli, "_release_dependents", lambda pruned: None)
        capsys.readouterr()
        cli.cmd_prune(argparse.Namespace(
            yes=yes, allow_stale=False, include_ticket_linked=include_ticket_linked
        ))
        captured = capsys.readouterr()
        return deleted, captured.out, captured.err

    return run


class _MissingPath:
    """Stands in for an item file that is not on disk, so the prune never
    unlinks anything real."""

    def exists(self) -> bool:
        return False


def test_prune_withholds_an_item_that_carries_ticket_links(pruner):
    """The ticket edge has ONE writer and no second copy.

    It is the only record of the link from shipped work back to the person who
    reported it — `impact` reads it to ask whether anyone is still complaining,
    and the consumer's lifecycle walks it the other way to tell that person the
    thing they reported is handled. Hygiene loses to evidence: the done pile is
    cosmetic, this is not recoverable.
    """
    deleted, out, err = pruner({
        "plain": _item("plain", status="done"),
        "reported": _item("reported", status="done",
                          tickets=["6f1b0f6e-0000-4000-8000-000000000001"]),
    })
    assert deleted == ["plain"]
    assert "reported" not in deleted


def test_the_withholding_is_loud_and_says_it_is_conservative(pruner):
    """A silent skip is how an operator ends up believing the pile was cleared.

    It must also admit WHY it withheld: this package cannot see ticket status —
    that lives in the consuming application — so it cannot tell a loop that is
    still open from one already closed, and holds both. Saying so is what keeps
    the gap from reading as the rule.
    """
    _, _, err = pruner({
        "reported": _item("reported", status="done",
                          tickets=["6f1b0f6e-0000-4000-8000-000000000001"]),
    })
    assert "reported" in err
    assert "6f1b0f6e-0000-4000-8000-000000000001" in err, "name the tickets, not just a count"
    assert "--include-ticket-linked" in err, "an operator needs the way past it"
    assert "cannot see ticket status" in err, (
        "the skip must state that it is broader than the rule it implements, or "
        "the gap reads as the rule"
    )


def test_the_operator_can_override_once_they_have_checked(pruner):
    """The judgement this code cannot make, a human can. Withholding forever
    would rebuild the done pile `prune` exists to clear."""
    deleted, _, err = pruner(
        {"reported": _item("reported", status="done",
                           tickets=["6f1b0f6e-0000-4000-8000-000000000001"])},
        include_ticket_linked=True,
    )
    assert deleted == ["reported"]
    assert "reported" in err, "destroying the edge deliberately is still worth saying out loud"


def test_an_empty_tickets_list_is_not_a_link(pruner):
    """The predicate is "is this edge load-bearing", and a field somebody emptied
    carries no edge. Treating `[]` as a link would withhold every item a schema
    default touched."""
    deleted, _, _ = pruner({"empty": _item("empty", status="done", tickets=[])})
    assert deleted == ["empty"]


def test_nothing_left_to_prune_reports_why(pruner):
    """`no done items — nothing to prune` would be a lie when there are done
    items and all of them were withheld."""
    deleted, out, _ = pruner({
        "reported": _item("reported", status="done",
                          tickets=["6f1b0f6e-0000-4000-8000-000000000001"]),
    })
    assert deleted == []
    assert "withheld" in out


def test_a_withheld_item_is_absent_from_the_dry_run_too(pruner):
    """The dry run is what an operator reads before passing `--yes`. Listing an
    item there that the real run will not touch teaches them to distrust it."""
    _, out, _ = pruner(
        {
            "plain": _item("plain", status="done"),
            "reported": _item("reported", status="done",
                              tickets=["6f1b0f6e-0000-4000-8000-000000000001"]),
        },
        yes=False,
    )
    assert "plain" in out
    assert "reported" not in out
