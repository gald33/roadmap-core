"""Status derivation and the startable queue.

Synthetic fixtures only, for the reason given in test_arcs.py: these are tests of
the graph, so they belong to the package and must not depend on this repository's
committed items.

Moved out of `backend/tests/test_roadmap.py`, which keeps the tests that are
genuinely ABOUT this repo — that the committed graph is coherent, that the
generated markdown stays mergeable, that the CLI and the store agree. Those
assert facts about Lucille and could not travel with the library.
"""

from __future__ import annotations

from roadmap_core import graph


def test_derived_status_beats_stored_status():
    """The stored value must never win over the edges. An item left at 'ready'
    whose dependency regressed is the drift this whole layer exists to stop."""
    by_key = {
        "a": {"key": "a", "title": "A", "status": "ready", "blocked_on": [], "evidence": "x"},
        "b": {"key": "b", "title": "B", "status": "ready", "blocked_on": ["a"], "evidence": "x"},
    }
    assert graph.derive_status(by_key["b"], by_key) == "blocked"
    by_key["a"]["status"] = "done"
    assert graph.derive_status(by_key["b"], by_key) == "ready"


def test_claim_wins_over_ready():
    by_key = {"a": {"key": "a", "title": "A", "status": "ready", "blocked_on": [],
                    "claimed_by": "some/branch", "evidence": "x"}}
    assert graph.derive_status(by_key["a"], by_key) == "claimed"


def test_deferred_is_derived_from_the_reason():
    by_key = {"a": {"key": "a", "title": "A", "status": "ready", "blocked_on": [],
                    "defer_reason": "spends the user's scarcest resource too early",
                    "evidence": "x"}}
    assert graph.derive_status(by_key["a"], by_key) == "deferred"
    assert graph.ready_items(by_key) == []


def test_blank_defer_reason_is_not_a_deferral():
    """A whitespace-only reason is an authoring slip, and silently withholding
    an item on the strength of one would be indistinguishable from a bug."""
    for blank in (None, "", "   \n "):
        by_key = {"a": {"key": "a", "title": "A", "status": "ready", "blocked_on": [],
                        "defer_reason": blank, "evidence": "x"}}
        assert graph.derive_status(by_key["a"], by_key) == "ready"


def test_blocked_beats_deferred():
    """The harder reason wins: reporting 'deferred' for an item that also cannot
    start would hide the dependency that actually gates it."""
    by_key = {
        "dep": {"key": "dep", "title": "D", "status": "ready", "blocked_on": [],
                "evidence": "x"},
        "a": {"key": "a", "title": "A", "status": "ready", "blocked_on": ["dep"],
              "defer_reason": "not yet", "evidence": "x"},
    }
    assert graph.derive_status(by_key["a"], by_key) == "blocked"


def test_claim_beats_deferred():
    """Someone took it anyway. The queue reports what is true, not the
    preference the claimer already overrode."""
    by_key = {"a": {"key": "a", "title": "A", "status": "ready", "blocked_on": [],
                    "defer_reason": "not yet", "claimed_by": "claude/x", "evidence": "x"}}
    assert graph.derive_status(by_key["a"], by_key) == "claimed"


def test_deferred_item_still_renders_with_its_reason():
    """A deferral whose reasoning is not visible decays into a silent backlog
    graveyard — the failure this state is supposed to prevent, not cause."""
    by_key = {"a": {"key": "a", "title": "A", "status": "ready", "blocked_on": [],
                    "defer_reason": "waiting on the store to be worth asking about",
                    "evidence": "x"}}
    out = graph.render_markdown(by_key)
    assert "Deferred" in out
    assert "waiting on the store to be worth asking about" in out


def test_dangling_dependency_counts_as_unmet():
    """Pointing at something that does not exist is not the same as satisfied."""
    by_key = {"a": {"key": "a", "title": "A", "status": "ready",
                    "blocked_on": ["ghost"], "evidence": "x"}}
    assert graph.unmet_deps(by_key["a"], by_key) == ["ghost"]
    assert graph.derive_status(by_key["a"], by_key) == "blocked"
    assert any("ghost" in p for p in graph.validate_graph(by_key))


def test_cycle_is_reported():
    by_key = {
        "a": {"key": "a", "title": "A", "status": "ready", "blocked_on": ["b"], "evidence": "x"},
        "b": {"key": "b", "title": "B", "status": "ready", "blocked_on": ["a"], "evidence": "x"},
    }
    assert graph.find_cycles(by_key)
    assert any("cycle" in p for p in graph.validate_graph(by_key))


# --- startable items ---------------------------------------------------------


def _plain(key: str, **over):
    base = {"key": key, "title": "T", "status": "ready", "blocked_on": [], "evidence": "x"}
    base.update(over)
    return base


def test_ready_items_excludes_blocked_claimed_and_done():
    """The three things that are not startable, each for a different reason."""
    by_key = {
        "free": _plain("free"),
        "blocked": _plain("blocked", blocked_on=["free"]),
        "held": _plain("held", claimed_by="claude/x"),
        "shipped": _plain("shipped", status="done"),
    }
    assert [i["key"] for i in graph.ready_items(by_key)] == ["free"]


def test_ready_items_are_key_ordered():
    """The markdown, the CLI and the API hand out the same head of the queue —
    an ordering that differs by caller is two sessions taking 'the first one'
    and getting different items."""
    by_key = {k: _plain(k) for k in ("c", "a", "b")}
    assert [i["key"] for i in graph.ready_items(by_key)] == ["a", "b", "c"]


# --- the generated files must be readable in a project that is not this one ---
#
# Both artifacts are written FOR a reader with nothing installed and no network,
# which is exactly the reader who cannot resolve a path that does not exist in
# their checkout. Asserted rather than reviewed: these strings sit in prose
# blocks nobody re-reads, and the failure is silent — the file renders, CI is
# green, and only a human following the instruction finds out it is wrong.

_EXTRACTION_REPO_PATHS = (
    # The shim in the repository this package was extracted from. Adopters
    # install the console script instead and have no `scripts/` entry at all.
    "scripts/roadmap.py",
    # Three hand-maintained docs that only ever existed over there.
    "docs/architecture",
)


def _one_of_everything():
    """A graph exercising every section that carries prose: ready, deferred,
    claimed and blocked all render their own instructions."""
    by_key = {
        "startable": _plain("startable", arc="an-arc"),
        "parked": _plain("parked", defer_reason="not yet", arc="an-arc"),
        "held": _plain("held", claimed_by="claude/x", claimed_at="2020-01-01T00:00:00Z"),
        "waiting": _plain("waiting", blocked_on=["held"]),
    }
    arcs = {"an-arc": {"key": "an-arc", "title": "An arc", "narrative": "why it is open"}}
    return arcs, by_key


def test_generated_markdown_names_no_path_from_the_extraction_repo():
    arcs, by_key = _one_of_everything()
    rendered = {
        "ROADMAP.md": graph.render_markdown(by_key),
        "ARCS.md": graph.render_arcs_markdown(arcs, by_key),
    }
    for name, text in rendered.items():
        for path in _EXTRACTION_REPO_PATHS:
            assert path not in text, (
                f"{name} tells its reader about {path!r}, which exists only in the "
                f"repository this package was extracted from. Use `graph.CLI`."
            )


def test_generated_markdown_names_the_console_script_this_package_installs():
    """The other half: having removed the wrong command, say the right one.

    A guard that only forbids the old string passes just as happily on a file
    that names no command at all, which is the same reader stranded a different
    way."""
    arcs, by_key = _one_of_everything()
    for text in (graph.render_markdown(by_key), graph.render_arcs_markdown(arcs, by_key)):
        assert f"`{graph.CLI} sync`" in text


# --- and the CLI's own messages, which the test above does not reach ---------
#
# `graph.CLI` fixed the headers *inside* the generated files. It did not fix the
# messages the CLI prints when it wants you to regenerate them — and those are
# strictly more likely to be read, because `sync --check` prints one every time
# the drift guard fires. An adopter upgrading to the release that removed
# `scripts/roadmap.py` from the generated files was told, by that same release,
# to run `python scripts/roadmap.py sync`.
#
# Parsed rather than imported: `roadmap_core.cli` is not importable in the
# isolation job, and the docstrings in that module discuss the extraction repo
# legitimately. Only strings on their way to a user are checked.

def _printed_strings(path):
    """Every string constant that reaches a `print(...)` call, f-strings included."""
    import ast

    tree = ast.parse(path.read_text())
    out = []

    def literals(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    out.append(part.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
            for arg in node.args:
                literals(arg)
                for sub in ast.walk(arg):
                    literals(sub)
    return out


def test_cli_messages_name_no_path_from_the_extraction_repo():
    import importlib.util
    from pathlib import Path

    cli_path = Path(importlib.util.find_spec("roadmap_core.cli").origin)
    printed = _printed_strings(cli_path)
    assert printed, "parsed no printed strings — the AST walk stopped working"
    for text in printed:
        for bad in _EXTRACTION_REPO_PATHS:
            assert bad not in text, (
                f"cli.py prints {text!r}, naming {bad!r} — a path that exists only in "
                f"the repository this package was extracted from. Use `graph.CLI`."
            )


# --- a claim that is held on purpose is not a claim that was forgotten -------


def _held(key, *, status, days_ago, now):
    """An item claimed `days_ago` by a branch, in `status`."""
    import datetime as _dt

    return {
        "key": key,
        "title": key,
        "status": status,
        "blocked_on": [],
        "evidence": "x",
        "claimed_by": f"claude/{key}",
        "claimed_at": (now - _dt.timedelta(days=days_ago)).isoformat(),
    }


def _now():
    import datetime as _dt

    return _dt.datetime(2026, 9, 14, 12, 0, tzinfo=_dt.timezone.utc)


def test_a_verifying_claim_is_not_stale_at_the_base_threshold():
    """The bug this package had against itself.

    Setting `verifying` PRINTS "verifying does NOT drop the claim — the branch
    that shipped this still owns confirming it". `ready` then reported that
    same claim as a "likely finished session that never released" and advised
    releasing it. Measured in Lucille 2026-09-14 on
    `closure-tool-fires-but-nothing-is-written`, held 7.8 days; its own claim
    audit said "Nothing to decide" about the identical row the same day.
    Releasing it would have stripped the claim from the branch that owns the
    watch — wrong advice, not merely noisy.
    """
    now = _now()
    by_key = {
        "claimed": _held("claimed", status="claimed", days_ago=7.8, now=now),
        "verifying": _held("verifying", status="verifying", days_ago=7.8, now=now),
    }
    stale = graph.stale_claims(by_key, now=now)
    assert [item["key"] for item in stale] == ["claimed"], (
        "a `verifying` claim held under the verifying threshold must not be "
        "reported as an abandoned hold"
    )


def test_a_verifying_claim_is_still_reported_once_it_outlives_its_own_window():
    """Surfacing threshold, never an expiry — reported later, not never.

    A session can ship and die before the effect lands, and that claim is just
    as abandoned as any other. Excluding `verifying` outright would trade a
    wrong report for a missing one.
    """
    now = _now()
    by_key = {"v": _held("v", status="verifying", days_ago=20.0, now=now)}
    stale = graph.stale_claims(by_key, now=now)
    assert [item["key"] for item in stale] == ["v"]
    assert stale[0]["claim_threshold_days"] == graph.VERIFYING_CLAIM_DAYS


def test_the_verifying_window_is_longer_than_the_base_one():
    """If these ever cross, the override silently becomes a no-op."""
    assert graph.VERIFYING_CLAIM_DAYS > graph.STALE_CLAIM_DAYS


def test_a_widened_threshold_widens_verifying_too():
    """`max`, not override: a caller asking for a laxer bar gets it everywhere.

    Otherwise `threshold_days=30` would report a 20-day `verifying` hold while
    ignoring a 20-day `claimed` one — the laxer request making the stricter
    answer.
    """
    now = _now()
    by_key = {"v": _held("v", status="verifying", days_ago=20.0, now=now)}
    assert graph.stale_claims(by_key, now=now, threshold_days=30) == []
    assert graph.claim_threshold_days(by_key["v"], threshold_days=30) == 30


def test_a_narrowed_threshold_does_not_narrow_verifying():
    """A status that holds on purpose still holds on purpose."""
    now = _now()
    by_key = {"v": _held("v", status="verifying", days_ago=7.8, now=now)}
    assert graph.stale_claims(by_key, now=now, threshold_days=0.1) == []


def test_every_other_status_keeps_the_base_threshold():
    """The override is a named exception, not a general loosening."""
    now = _now()
    for status in graph.STATUSES:
        if status == "verifying":
            continue
        item = _held("k", status=status, days_ago=4.0, now=now)
        assert graph.claim_threshold_days(item) == graph.STALE_CLAIM_DAYS, status
        assert graph.stale_claims({"k": item}, now=now), status


def test_an_unknown_status_keeps_the_base_threshold():
    """A typo must not silently buy an item a two-week hold."""
    now = _now()
    item = _held("k", status="verifyng", days_ago=4.0, now=now)
    assert graph.claim_threshold_days(item) == graph.STALE_CLAIM_DAYS
    assert graph.stale_claims({"k": item}, now=now)
