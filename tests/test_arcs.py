"""The arc layer: derived state, the checks, and the split between them.

Lives with the package rather than with the backend because these test the
GRAPH, not this repository. Every fixture here is synthetic — no DB, no app, no
committed `roadmap/items/`. That is what makes them travel: a repo adopting
`roadmap-core` gets the proof that its own copy behaves, rather than trusting a
suite that only ever ran inside Lucille.

WHAT THEY GUARD. `ARCS.md` was 1449 hand-written lines that nothing executed and
nothing checked, above a work-item layer that had a store, a schema and a
validator. The measured failure (2026-08-16): nine of eighteen arcs had zero
items and seven of those read "open tail", so the narrative asserted pending work
the queue could not offer one item for — and no artifact could say so.

The subtlety worth testing rather than commenting is WHY the emptiness check is
keyed on the arc's declared state instead of on a count of items. A healthy arc's
population empties every time its work finishes: measured across the 2026-08-16
prune, `entity-graph` went to zero items *because its only item completed*. A
count-based check calls that success drift, on every prune, forever.
"""

from __future__ import annotations

import pytest

from roadmap_core import graph


def item(key: str, **kw):
    base = {
        "key": key,
        "title": key,
        "status": "ready",
        "arc": None,
        "priority": None,
        "blocked_on": [],
        "related_to": [],
        "artifacts": [],
        "refs": [],
        "tickets": [],
        "evidence": "measured somewhere",
        "claimed_by": None,
    }
    base.update(kw)
    return base


def arc(key: str, **kw):
    base = {"key": key, "title": key.title(), "state": None, "state_evidence": "",
            "refs": [], "narrative": ""}
    base.update(kw)
    return base


def graphs(items, arcs):
    return {i["key"]: i for i in items}, {a["key"]: a for a in arcs}


# --- derived state ----------------------------------------------------------


def test_arc_with_only_done_items_derives_closed():
    by_key, arcs = graphs([item("a", arc="x", status="done")], [arc("x")])
    assert graph.derive_arc_state(arcs["x"], by_key) == "closed"


def test_arc_with_all_items_blocked_derives_blocked():
    by_key, arcs = graphs(
        [item("dep", arc="x"), item("a", arc="x", blocked_on=["dep"])], [arc("x")]
    )
    # `dep` is startable, so the arc is open — not blocked.
    assert graph.derive_arc_state(arcs["x"], by_key) == "open"

    by_key, arcs = graphs([item("a", arc="x", blocked_on=["missing"])], [arc("x")])
    assert graph.derive_arc_state(arcs["x"], by_key) == "blocked"


def test_verifying_items_keep_an_arc_open_rather_than_closing_it():
    """`verifying` is not finished, and the distinction is load-bearing.

    It means the code shipped and nothing has confirmed the effect in prod —
    exactly the state CLAUDE.md's "deployed != doing anything" pitfall exists to
    keep out of the done column. An arc whose every item is `verifying` still has
    open questions, so treating it as closed would launder unconfirmed work into
    a finished narrative.
    """
    by_key, arcs = graphs([item("a", arc="x", status="verifying")], [arc("x")])
    assert graph.derive_arc_state(arcs["x"], by_key) == "open"


def test_empty_arc_derives_open_so_the_gap_is_visible():
    by_key, arcs = graphs([], [arc("x")])
    assert graph.derive_arc_state(arcs["x"], by_key) == "open"


@pytest.mark.parametrize("declared", graph.DECLARABLE_ARC_STATES)
def test_a_declaration_wins_over_derivation(declared):
    """Including when it disagrees — the disagreement is a finding, not a bug to
    silently correct. An arc called closed while items are open is a statement
    somebody should have to withdraw."""
    by_key, arcs = graphs(
        [item("a", arc="x")], [arc("x", state=declared, state_evidence="checked")]
    )
    assert graph.derive_arc_state(arcs["x"], by_key) == declared


# --- the checks -------------------------------------------------------------


def test_empty_open_arc_is_a_finding_not_a_validation_error():
    """The split that keeps both useful.

    A thin backlog is a coherent graph. Failing hourly CI on it would make red
    the normal state, which is how a guard stops being read.
    """
    by_key, arcs = graphs([], [arc("x")])
    assert graph.validate_arcs(arcs, by_key) == []
    findings = graph.arc_findings(arcs, by_key)
    assert len(findings) == 1
    assert "NO items at all" in findings[0]


def test_a_finished_arc_produces_no_finding_once_declared_closed():
    """The whole reason closure is declared rather than derived.

    After `prune`, a finished arc and an arc nobody filed work for are the same
    empty set. Declaring closure is what distinguishes them — and this test is
    what stops a future author "simplifying" the check back to a count.
    """
    by_key, arcs = graphs([], [arc("x", state="closed", state_evidence="shipped, verified")])
    assert graph.arc_findings(arcs, by_key) == []
    assert graph.validate_arcs(arcs, by_key) == []


def test_an_arc_that_derives_blocked_produces_no_finding():
    """It is not claiming an open tail, so there is nothing to report.

    The parked-wording case is covered separately below; this pins the boundary —
    an arc whose every unfinished item is blocked derives `blocked` and drops out
    of the findings pass entirely, rather than being reported and then explained.
    """
    by_key, arcs = graphs([item("a", arc="x", blocked_on=["gone"])], [arc("x")])
    assert graph.derive_arc_state(arcs["x"], by_key) == "blocked"
    assert graph.arc_findings(arcs, by_key) == []


def test_an_arc_awaiting_confirmation_is_not_reported_as_idle():
    """The false-positive class that would have made the report unreadable.

    `verifying` means the code shipped and nobody has confirmed the effect in prod
    yet, so there is *supposed* to be nothing startable — the next move is an
    observation, not a commit. "File the work, or declare it closed" is wrong on
    both counts.

    Measured on the first real run, 2026-08-16: 4 of 13 findings were this case,
    including `world-model` with four items in flight. A 31% false-positive rate on
    a report whose only job is to be believed is how a check gets skimmed and then
    ignored.
    """
    by_key, arcs = graphs([item("a", arc="x", status="verifying")], [arc("x")])
    assert graph.derive_arc_state(arcs["x"], by_key) == "open"
    assert graph.startable_in_arc("x", by_key) == []
    assert graph.arc_findings(arcs, by_key) == []


def test_a_fully_parked_arc_is_reported_with_the_parked_wording():
    """Distinct message, because the fix is distinct.

    The work is named and somebody parked it, so "declare it closed" would be
    wrong — the items exist and are unfinished. The question is whether the
    parking was deliberate.
    """
    by_key, arcs = graphs(
        [
            item("a", arc="x", status="deferred", defer_reason="not worth it yet"),
            item("b", arc="x", blocked_on=["a"]),
        ],
        [arc("x")],
    )
    findings = graph.arc_findings(arcs, by_key)
    assert len(findings) == 1
    assert "nothing in flight, nothing startable" in findings[0]
    assert "closed" not in findings[0], "closure is the wrong advice when items exist"


def test_item_pointing_at_a_missing_arc_is_a_validation_error():
    """Structural, so it fails the build.

    This is the check that earned its place on its first run:
    `cog-plan-richness-intervention` arrived declaring `arc: cognition` against an
    ARCS.md with no such section, hours after a survey concluded no item was
    wrong.
    """
    by_key, arcs = graphs([item("a", arc="nope")], [arc("x")])
    problems = graph.validate_arcs(arcs, by_key)
    assert any("arc 'nope' does not exist" in p for p in problems)


def test_declaring_a_derived_state_is_rejected():
    by_key, arcs = graphs([], [arc("x", state="open", state_evidence="whatever")])
    problems = graph.validate_arcs(arcs, by_key)
    assert any("is derived, not declared" in p for p in problems)


def test_a_declared_state_without_evidence_is_rejected():
    by_key, arcs = graphs([], [arc("x", state="closed")])
    problems = graph.validate_arcs(arcs, by_key)
    assert any("no `state_evidence`" in p for p in problems)


def test_closed_arc_with_unfinished_items_is_rejected():
    by_key, arcs = graphs(
        [item("a", arc="x")], [arc("x", state="closed", state_evidence="checked")]
    )
    problems = graph.validate_arcs(arcs, by_key)
    assert any("declared `closed` but 1 item(s) are unfinished" in p for p in problems)


def test_items_without_an_arc_are_reported_but_not_errors():
    by_key, arcs = graphs([item("a"), item("b", arc="x")], [arc("x")])
    assert graph.orphan_items(by_key) == ["a"]
    assert graph.validate_arcs(arcs, by_key) == []


# --- rendering --------------------------------------------------------------


def test_editing_one_arc_does_not_change_another_arcs_rendered_lines():
    """The #1103 lesson, applied to the second generated file — as a property.

    A timestamp or a whole-graph count differs between any two branches that ran
    `sync`, so it conflicts on merge while saying nothing about what either
    branch changed. ROADMAP.md was rebased four times in one afternoon on exactly
    that.

    Asserted structurally rather than by scanning for the word "total", which is
    what a first draft of this test did — and it failed on the render's own prose
    explaining that it carries no total. The property that actually matters is
    that a branch touching arc `b` cannot move a single line belonging to arc
    `a`; anything clock- or aggregate-derived breaks it, whatever it is called.
    """
    by_key, arcs = graphs([item("i", arc="a")], [arc("a", narrative="prose for a")])
    before = graph.render_arcs_markdown(arcs, by_key)

    by_key2, arcs2 = graphs(
        [item("i", arc="a"), item("j", arc="b")],
        [arc("a", narrative="prose for a"), arc("b", narrative="prose for b")],
    )
    after = graph.render_arcs_markdown(arcs2, by_key2)

    def section(text: str, key: str) -> str:
        start = text.index(f"`{key}` · ")
        rest = text[start:]
        nxt = rest.find("\n### ")
        return rest[: nxt if nxt != -1 else len(rest)]

    assert section(before, "a") == section(after, "a"), (
        "adding arc `b` moved lines inside arc `a` — something in the render is "
        "derived from a graph-wide value and will conflict on every merge"
    )
    assert "Generated" not in before


def test_render_is_deterministic_and_puts_findings_high():
    by_key, arcs = graphs([], [arc("x"), arc("a")])
    first = graph.render_arcs_markdown(arcs, by_key)
    assert first == graph.render_arcs_markdown(arcs, by_key)
    assert first.index("claiming work the queue cannot offer") < first.index("## Legend")


def test_render_includes_narrative_and_items():
    by_key, arcs = graphs(
        [item("a", arc="x", priority="now")], [arc("x", narrative="why this is open")]
    )
    out = graph.render_arcs_markdown(arcs, by_key)
    assert "why this is open" in out
    assert "`a`" in out
    assert "now" in out


# --- store vs files ---------------------------------------------------------


def test_arc_drift_between_store_and_files_is_detectable():
    """The gap the arc layer shipped with, found on its first reconciliation.

    `compare_sources` covers items only, so arc drift was undetectable by
    construction — and it happened immediately: `pull` creates a missing arc file
    but never UPDATES an existing one, so a state declared against the store
    reached no checkout, and the hourly sync's `push` then overwrote the store
    from main's (undeclared) files. Same shape as
    `roadmap-prune-races-the-hourly-sync`, one layer up, and invisible without
    this.
    """
    db = {"x": arc("x", state="blocked", state_evidence="external")}
    files = {"x": arc("x")}
    problems = graph.compare_arc_sources(db, files)
    assert any("declared state differs" in p for p in problems)

    # Presence, both directions.
    assert any(
        "in the DB but NOT in roadmap/arcs/" in p
        for p in graph.compare_arc_sources({"y": arc("y")}, {})
    )
    assert any(
        "in roadmap/arcs/ but NOT in the DB" in p
        for p in graph.compare_arc_sources({}, {"y": arc("y")})
    )

    # Agreement is silence.
    same = {"x": arc("x", state="dark", state_evidence="checked")}
    assert graph.compare_arc_sources(same, dict(same)) == []


def test_arc_narrative_churn_is_not_reported_as_drift():
    """Same reasoning `compare_sources` applies to `evidence`.

    Prose changes on every reword and is not something another session collides
    on. Comparing it would cry wolf on every edit and train readers to ignore the
    report.
    """
    db = {"x": arc("x", narrative="one wording")}
    files = {"x": arc("x", narrative="a different wording entirely")}
    assert graph.compare_arc_sources(db, files) == []
