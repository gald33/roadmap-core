"""Pure graph + markdown rendering for the roadmap work-item store.

**Stdlib only, by design.** Two callers need this logic: the backend
(``app.crud.roadmap``, which has the DB) and ``scripts/roadmap.py`` (which runs
in any checkout with no app dependencies and no DB). If each kept its own copy
they would drift, and a status derived one way in the API and another way in the
generated markdown is precisely the silent-divergence failure this repo has been
bitten by before. So: one module, no imports beyond stdlib, loaded by path from
the script and imported normally by the backend.

Items are plain dicts keyed by ``key`` so this serves DB rows, API payloads, and
parsed YAML with no adapter.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

STATUSES = ("ready", "deferred", "blocked", "claimed", "verifying", "done")

#: How the generated markdown tells a reader to re-run this tool.
#:
#: The name of the console script this package installs, and DELIBERATELY A
#: CONSTANT rather than anything derived from ``sys.argv`` or the environment.
#: ``ROADMAP.md`` and ``ARCS.md`` are generated, committed, and compared
#: byte-for-byte by ``sync --check``, so a header that varied with how the
#: command happened to be invoked would make the file disagree with itself
#: between two developers and flip back and forth on every run — the same
#: failure the no-clock, no-graph-wide-total rule in ``render_markdown`` exists
#: to prevent, arriving through the environment instead of through time.
#:
#: It used to read ``python scripts/roadmap.py``, which is the shim in the
#: repository this package was extracted from and exists in no other checkout.
#: Every adopter's generated files therefore opened by naming a file they do not
#: have — in the one artifact written specifically for a reader with nothing
#: installed, who has no way to discover the real command.
CLI = "roadmap"

#: ``tickets`` entries are ``feedback_tickets.id`` values. Matched here rather
#: than parsed with ``uuid.UUID`` because this module is stdlib-only *and*
#: import-light on purpose — ``scripts/roadmap.py`` loads it by path with no app
#: dependencies — and because a report is wanted, not an exception.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: What a human said this item is worth, most-urgent first. ``None`` — nobody
#: said — is deliberately NOT a member: it is the absence of a judgement, not a
#: fourth grade of one, and it is the normal state of an item.
#:
#: Three values rather than an integer or a score. An integer invites "is this a
#: 40 or a 45", which is a scoring rubric by another name, and a rubric is a way
#: of not deciding — you can always adjust a weight instead of saying what comes
#: first. Three buckets force the statement the queue actually needs.
#:
#: Distinct from ``defer_reason``, which is the *other* half of the same
#: judgement and must not be confused with it: a deferral WITHHOLDS the item
#: from the queue entirely and owes the next reader a reason. ``later`` keeps it
#: in the queue, at the bottom. "Not the thing to start, and here is why" is a
#: deferral; "startable, just not first" is ``later``.
PRIORITIES = ("now", "next", "later")

#: Where each priority sorts. ``None`` sits between ``next`` and ``later`` on
#: purpose: an item somebody explicitly pushed down ranks BELOW one nobody has
#: looked at yet. Putting unjudged items last would mean stating a preference
#: for three items silently buried the other forty-five.
_PRIORITY_RANK: dict[str | None, int] = {"now": 0, "next": 1, None: 2, "later": 3}

#: An unrecognised value ranks with the unjudged rather than crashing the queue.
#: ``validate_graph`` is what complains about it; a typo in one item must not
#: take out the ordering of every other one.
_UNKNOWN_PRIORITY_RANK = _PRIORITY_RANK[None]

#: A claim held longer than this reads as *suspect*, not as expired.
#:
#: Sessions are short-lived — an agent claims, works, opens a PR and merges,
#: usually inside a day. A hold that outlives a long weekend is far more likely
#: to be a session that ended than work still in flight. The worked example:
#: ``engine-topic-id-populate`` was claimed on 2026-08-01 by
#: ``claude/roadmap-task-endpoint-899r4r`` — the session *building the claim
#: endpoint*, which staked a real item to exercise it, merged, and never
#: released. Nothing could see that, because a claim's only trace is a row.
#:
#: Deliberately a surfacing threshold and never an expiry: nothing auto-releases.
#: The store is the race authority, and silently yanking an item out from under
#: a live long-running session is a worse failure than showing a stale hold to
#: the next reader, who can check the branch and decide.
STALE_CLAIM_DAYS = 3

#: The second edge type, and the only non-blocking one.
#:
#: ``blocked_on`` says "do not start X until Y is done" — a claim about
#: correctness, which ``derive_status`` turns into ``blocked`` and removes from
#: the queue. There was no way to say the weaker and far more common thing:
#: **these two are not independent**. Read one before starting the other, land
#: them in an order, do not conflate them — but either is startable today.
#:
#: That statement was being made, constantly, in ``evidence`` prose. A survey of
#: the 37-item graph on 2026-08-01 found ten such pairs written down in six
#: different spellings — ``COORDINATION:``, ``Related:``, ``ADJACENT, DO NOT
#: CONFLATE:``, ``WATCH THE ADJACENT ARC:``, ``bears on``, ``Depends on … in
#: practice though not in code order`` — none greppable as a set, none rendered
#: anywhere ``ready`` looks. A session claims the head of the queue and finds
#: out at review time.
#:
#: Deliberately NOT modelled as ``blocked_on``. Encoding "read this first" as a
#: dependency would serialize work that does not need serializing and make the
#: ready queue lie about what is startable — the queue would shrink to protect
#: against a conflict that a sentence prevents just as well, as long as somebody
#: reads the sentence. So the edge changes nothing about status and everything
#: about what a session is *told* when it picks the item up.
#:
#: **Symmetric on read, one-sided on write.** Only one of the pair has to
#: declare it — whichever author noticed — and both surface it. A relation that
#: had to be written twice would be half-written most of the time, and the half
#: that went missing would be on the item somebody was about to start.
RELATION_FIELD = "related_to"


def normalize_relations(item: dict[str, Any]) -> list[dict[str, str]]:
    """This item's own outgoing relations as ``{key, note}``, deduped by key.

    Tolerates a bare string in place of a mapping so a hand-authored file can
    say ``related_to: [some-key]`` — a relation with no note is worth strictly
    more than no relation, and refusing it would push the author back to prose,
    which is the thing this field exists to replace. ``validate_graph`` is what
    asks for the note.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in item.get(RELATION_FIELD) or []:
        if isinstance(raw, str):
            key, note = raw.strip(), ""
        elif isinstance(raw, dict):
            key = str(raw.get("key") or "").strip()
            note = " ".join(str(raw.get("note") or "").split())
        else:
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "note": note})
    return sorted(out, key=lambda rel: rel["key"])


def relations_for(key: str, by_key: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Everything related to ``key``, from both directions, deduped and sorted.

    An edge declared by the *other* item is reported here with that item's note,
    because the note explains the pair and not the declarer. Where both sides
    declared the edge, this item's own note wins — the author who is looking at
    this item wrote it for this reader.

    Self-edges are dropped rather than reported: an item related to itself is a
    typo, ``validate_graph`` says so, and echoing it into every surface would
    make the typo look like content.
    """
    merged: dict[str, str] = {}
    for other_key in sorted(by_key):
        if other_key == key:
            continue
        for rel in normalize_relations(by_key[other_key]):
            if rel["key"] == key:
                merged.setdefault(other_key, rel["note"])
    # Own edges last so they overwrite the inbound note on a doubly-declared pair.
    for rel in normalize_relations(by_key.get(key, {})):
        if rel["key"] != key:
            merged[rel["key"]] = rel["note"] or merged.get(rel["key"], "")
    return [{"key": k, "note": merged[k]} for k in sorted(merged)]


def relation_pairs(by_key: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Every relation once, as an ordered pair, for a symmetric render.

    Both halves of a doubly-declared edge collapse to the same pair, so the
    mermaid graph draws one line rather than two overlapping ones.
    """
    pairs: set[tuple[str, str]] = set()
    for key in by_key:
        for rel in normalize_relations(by_key[key]):
            if rel["key"] != key and rel["key"] in by_key:
                pairs.add((key, rel["key"]) if key < rel["key"] else (rel["key"], key))
    return sorted(pairs)


#: Shared mutable artifacts this item will touch — the *merge* axis, which is
#: neither of the other two edges.
#:
#: ``blocked_on`` is a claim about correctness. ``related_to`` is a claim about
#: understanding. Neither describes the third thing that actually went wrong
#: across one day of parallel sessions on 2026-08-01/02: two items that are
#: genuinely independent, correctly run in parallel, and then cannot MERGE in
#: parallel because they both rewrite a file with a single global counter.
#:
#: The dependency layer worked that day — nothing semantically conflicting ran
#: together. What collided was:
#:
#: * ``modules/<name>/VERSION`` + the SPECS table + the hash manifest — one
#:   counter per module. #1103 had to move 0.3.1 -> 0.3.2 mid-review because
#:   #1102 took 0.3.1 while it was open.
#: * Alembic migration numbers — same shape, and already documented in CLAUDE.md
#:   as a known pitfall ("two open PRs each numbering their migration 0134").
#: * ``roadmap/ROADMAP.md`` — six conflicts, now dissolved by CI regenerating it
#:   on merge, which is why this vocabulary is mostly about the counters.
#:
#: Deliberately NOT ``blocked_on``: these items can run in parallel perfectly
#: well. Encoding a merge-order problem as a dependency would serialize work
#: that does not need serializing. And deliberately not ``refs`` either — the
#: files that collide (VERSION, the SPECS table, the migration directory) are
#: exactly the ones nobody lists in refs, because they are not where the work
#: is.
#:
#: So contention **warns and never blocks**, on the surfaces where a session is
#: about to create one: ``ready``, ``claim``, and the API's ready payload. The
#: system already knows who holds what; the missing half was only a vocabulary
#: for what they are holding.
ARTIFACT_FIELD = "artifacts"

#: Recognised artifact namespaces. A token is ``prefix`` or ``prefix:detail``.
#:
#: Open by design — an unknown prefix is *reported*, never rejected, because a
#: vocabulary that has to be edited before an item can name its own collision is
#: a vocabulary sessions route around. The check exists to catch ``alembic-``
#: for ``alembic``, not to police the namespace.
ARTIFACT_PREFIXES = (
    "alembic",  # the migration number sequence
    "module",   # module:<name> — VERSION + SPECS row + hash manifest, one counter
    "roadmap",  # the roadmap store, its CLI and its generated markdown
    "web",      # web/ — shared route tables and generated types
    "docs",     # a single doc two items both rewrite
    "config",   # backend settings / feature-flag tables
    "script",   # a shared script under scripts/
)


def normalize_artifacts(item: dict[str, Any]) -> list[str]:
    """This item's artifact tokens, lowercased, deduped, sorted."""
    seen: list[str] = []
    for raw in item.get(ARTIFACT_FIELD) or []:
        token = str(raw or "").strip().lower()
        if token and token not in seen:
            seen.append(token)
    return sorted(seen)


def artifact_contention(
    key: str, by_key: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Live claims that will collide with ``key`` on merge, not on meaning.

    Only *claimed* items count. An unclaimed item sharing an artifact is not a
    collision, it is a queue — nobody is writing to that counter yet, and
    warning about every pair that could someday overlap is how a warning stops
    being read. ``done`` items are likewise silent: their counter move already
    landed.

    Returns one row per colliding item, with the tokens it shares, so the caller
    can say *what* will conflict and not merely that something will.
    """
    mine = set(normalize_artifacts(by_key.get(key, {})))
    if not mine:
        return []
    out: list[dict[str, Any]] = []
    for other_key in sorted(by_key):
        other = by_key[other_key]
        if other_key == key or not other.get("claimed_by"):
            continue
        if other.get("status") == "done":
            continue
        shared = sorted(mine & set(normalize_artifacts(other)))
        if shared:
            out.append(
                {
                    "key": other_key,
                    "claimed_by": other.get("claimed_by"),
                    "artifacts": shared,
                }
            )
    return out


def _parse_moment(value: Any) -> _dt.datetime | None:
    """UTC-aware datetime, or ``None`` when the value is not a usable instant.

    Shared with ``fmt_timestamp`` on purpose: a second timestamp parser in this
    module is precisely the silent divergence the module docstring is about.
    """
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        moment = value
    elif isinstance(value, _dt.date):
        moment = _dt.datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            moment = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc)


def claim_age_days(item: dict[str, Any], *, now: _dt.datetime | None = None) -> float | None:
    """Days since this item's claim was stamped.

    ``None`` when nobody holds it, or when the hold carries no readable
    timestamp — an unstamped claim cannot be aged, and guessing an age for it
    would invent the very fact the caller is asking about.
    """
    if not item.get("claimed_by"):
        return None
    moment = _parse_moment(item.get("claimed_at"))
    if moment is None:
        return None
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return (now - moment).total_seconds() / 86400.0


def stale_claims(
    by_key: dict[str, dict[str, Any]],
    *,
    now: _dt.datetime | None = None,
    threshold_days: float = STALE_CLAIM_DAYS,
) -> list[dict[str, Any]]:
    """Held items whose claim has outlived a plausible session, oldest first.

    Copies rather than mutating, with ``claim_age_days`` added, so the CLI, the
    API and the generated markdown all report the same age from the same
    computation instead of each re-deriving it against its own clock.
    """
    aged = []
    for key in sorted(by_key):
        age = claim_age_days(by_key[key], now=now)
        if age is not None and age >= threshold_days:
            aged.append({**by_key[key], "claim_age_days": round(age, 1)})
    return sorted(aged, key=lambda item: item["claim_age_days"], reverse=True)


def fmt_timestamp(value: Any) -> str | None:
    """One canonical spelling for a timestamp, whatever the source handed us.

    The two sources disagree on type for the same instant, and interpolating
    either one raw is how the generated markdown ends up source-dependent:

    * files — PyYAML resolves an unquoted ISO timestamp to a ``datetime``, and
      ``str()`` on that yields ``2026-07-31 12:17:02+00:00`` (space, offset).
    * db — the admin API serialises to JSON, so it arrives as the *string*
      ``2026-07-31T12:17:02.569294+00:00`` (T, microseconds).

    With everything else about the two renders identical, that one line made
    ``sync --source db`` produce a ROADMAP.md that fails
    ``test_committed_markdown_is_in_sync`` — i.e. the db path and the CI guard
    were mutually exclusive. Normalising here rather than at each load keeps the
    fix in the one place both callers already share.

    Unparseable input is returned as-is: a hand-written value should render
    exactly as written, not vanish or crash the render.
    """
    if value is None:
        return None
    # A bare date carries no time to normalise, and rendering it as midnight
    # would assert a precision the source never had.
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value.isoformat()
    moment = _parse_moment(value)
    if moment is None:
        return str(value).strip() or None
    # Second precision: microseconds are noise on a "who holds this, since
    # when" line, and they are a difference between the sources by themselves.
    return moment.replace(microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")


def unmet_deps(item: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> list[str]:
    """Dependencies that are not ``done``. A dangling edge counts as unmet —
    pointing at something that does not exist is not the same as satisfied."""
    return [
        dep
        for dep in (item.get("blocked_on") or [])
        if dep not in by_key or by_key[dep].get("status") != "done"
    ]


def find_cycles(by_key: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Every dependency cycle as a node list. A cycle means nothing in it can
    ever become ready, so callers treat this as an error, not a warning."""
    cycles: list[list[str]] = []
    settled: set[str] = set()

    def walk(node: str, stack: list[str]) -> None:
        if node in stack:
            cycle = stack[stack.index(node):] + [node]
            if sorted(set(cycle)) not in [sorted(set(c)) for c in cycles]:
                cycles.append(cycle)
            return
        if node in settled or node not in by_key:
            return
        stack.append(node)
        for dep in by_key[node].get("blocked_on") or []:
            walk(dep, stack)
        stack.pop()
        settled.add(node)

    for key in by_key:
        walk(key, [])
    return cycles


def derive_status(item: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> str:
    """The status the graph implies, which wins over the stored one.

    Stored status drifts: an item marked ``ready`` whose dependency regressed is
    lying to the next session that reads it. Only ``done``, ``verifying``, an
    active claim and a deferral are facts the graph cannot derive; everything
    else follows from the edges.

    Precedence, and why it is this way round:

    * ``done``, ``verifying`` and ``claimed`` first — all three are facts about
      the world, not opinions about ordering. ``verifying`` sits between the
      other two: the code has landed (further along than a claim, which may
      never ship) but its effect has not been confirmed in prod yet (less
      settled than ``done``, which asserts the effect itself). Set it, never
      derive it — nothing else can tell "shipped, awaiting its post-merge
      check" from "shipped, confirmed" or "not shipped at all".
    * ``blocked`` before ``deferred`` — an unmet dependency is a hard reason it
      cannot start, which strictly dominates a judgement that it should not
      start yet. Reporting the softer reason would hide the harder one.
    * ``deferred`` before ``ready`` — the whole point is that it stops being
      offered. A deferral that still read as ``ready`` would change nothing.

    Note a claim beats a deferral deliberately: if a session went and took a
    deferred item anyway, the queue must report what is true, not re-assert the
    preference the claimer already overrode.
    """
    if item.get("status") == "done":
        return "done"
    if item.get("status") == "verifying":
        return "verifying"
    if item.get("claimed_by"):
        return "claimed"
    if unmet_deps(item, by_key):
        return "blocked"
    if (item.get("defer_reason") or "").strip():
        return "deferred"
    return "ready"


def priority_of(item: dict[str, Any]) -> str | None:
    """The item's stated priority, or ``None`` when nobody has stated one.

    Blank strings normalise to ``None`` for the same reason ``derive_status``
    treats a blank ``defer_reason`` as no deferral: a field somebody emptied is
    a field nobody set, and the two must not sort differently.
    """
    value = (item.get("priority") or "").strip().lower()
    return value or None


def priority_rank(item: dict[str, Any]) -> int:
    """How far up the queue this item's stated priority puts it."""
    return _PRIORITY_RANK.get(priority_of(item), _UNKNOWN_PRIORITY_RANK)


def queue_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    """The one ordering every surface hands work out in: priority, then key.

    Key is the tie-break rather than, say, ``created_at``, so the ordering stays
    **total and reproducible from the graph alone** — a checkout with no DB, the
    CLI and the API sort identically, which is the property the whole queue rests
    on. Without a tie-break, forty-five equally-unprioritised items would come
    back in whatever order their source happened to iterate.
    """
    return (priority_rank(item), item.get("key") or "")


def ready_items(by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything startable right now, most important first.

    "Startable" is the *derived* status, not the stored one: no unmet
    dependencies, nobody holding a claim, not deferred, not already done.

    Ordered by ``queue_sort_key`` — stated priority, then key — rather than
    insertion or update order, because ``ROADMAP.md``'s Ready section, the CLI
    and the API must all offer the same item first. A session that reads the
    markdown and a session that calls the endpoint are picking from one queue,
    and an ordering that differs by caller is how two of them end up on different
    items each believing it took the head.

    Priority leads because key order alone is alphabetical, i.e. arbitrary with
    respect to value: it could say what was startable but never what was worth
    starting, so with several sessions pulling from one queue the next pick was
    effectively decided by an item key's first letter. With nothing prioritised
    this is byte-for-byte the old key ordering, which is the intended default —
    the queue only reorders once a human has actually said something.
    """
    return sorted(
        (by_key[key] for key in by_key if derive_status(by_key[key], by_key) == "ready"),
        key=queue_sort_key,
    )


def validate_graph(by_key: dict[str, dict[str, Any]]) -> list[str]:
    """Human-readable problems. Empty == coherent."""
    problems: list[str] = []
    for key, item in sorted(by_key.items()):
        for dep in item.get("blocked_on") or []:
            if dep not in by_key:
                problems.append(f"{key}: blocked_on unknown item {dep!r}")
        if key in (item.get("blocked_on") or []):
            problems.append(f"{key}: blocked_on itself")
        if item.get("status") not in STATUSES:
            problems.append(f"{key}: unknown status {item.get('status')!r}")
        # Reported rather than raised: an unrecognised value already sorts with
        # the unjudged, so the queue keeps working — but silently ignoring a
        # priority somebody meant to set is how a stated ordering evaporates,
        # which is the exact failure the field exists to fix.
        if priority_of(item) is not None and priority_of(item) not in PRIORITIES:
            problems.append(
                f"{key}: unknown priority {item.get('priority')!r} — "
                f"one of {', '.join(PRIORITIES)}, or omit it"
            )
        for rel in normalize_relations(item):
            if rel["key"] == key:
                problems.append(f"{key}: {RELATION_FIELD} itself")
            elif rel["key"] not in by_key:
                problems.append(f"{key}: {RELATION_FIELD} unknown item {rel['key']!r}")
            # Reported, never rejected: the edge still surfaces the pair, which
            # is most of the value. But a bare key tells the next session that
            # two items touch and not what to do about it, and "figure out why
            # these are related" is the work this field was added to stop.
            elif not rel["note"]:
                problems.append(
                    f"{key}: {RELATION_FIELD} {rel['key']!r} has no note — say what "
                    f"the next session has to know before starting either one"
                )
        for token in normalize_artifacts(item):
            prefix = token.split(":", 1)[0]
            # Reported, never rejected — see ARTIFACT_PREFIXES. A typo'd token
            # silently matches nothing, which is the one failure mode of a
            # warn-only field: it looks declared and warns about nothing.
            if prefix not in ARTIFACT_PREFIXES:
                problems.append(
                    f"{key}: unknown artifact namespace {prefix!r} in {token!r} — "
                    f"one of {', '.join(ARTIFACT_PREFIXES)}, or add it deliberately"
                )
        for ticket_id in item.get("tickets") or []:
            # Reported, never rejected, for the same reason as the artifact
            # namespaces above: a malformed id looks declared and answers
            # nothing. `impact` would quietly report one fewer ticket than the
            # file claims, which is the one way this link can lie.
            if not _UUID_RE.match(str(ticket_id).strip()):
                problems.append(
                    f"{key}: tickets entry {ticket_id!r} is not a feedback_tickets "
                    "uuid — `impact` cannot resolve it"
                )
        if not (item.get("evidence") or "").strip():
            problems.append(f"{key}: no evidence — see roadmap/README.md rule 2")
    for cycle in find_cycles(by_key):
        problems.append(f"dependency cycle: {' -> '.join(cycle)}")
    return problems


def _node(key: str) -> str:
    """mermaid node ids cannot contain '-'."""
    return key.replace("-", "_")


def render_markdown(by_key: dict[str, dict[str, Any]]) -> str:
    """The agent-facing projection of the graph.

    Written for a coding agent reading a checkout with no DB access, so it must
    be self-contained and answer, in order: what can I start, who holds what,
    what is blocked and behind what, and what is the evidence. READY comes first
    because that is the only section a session picking up work needs.

    **Nothing rendered here may depend on the clock or on a graph-wide
    aggregate.** This file is regenerated wholesale and committed, so any such
    line differs between any two branches that ran ``sync`` and conflicts on
    merge — while saying nothing about what either branch changed. It made every
    roadmap PR block every other one: #1103 carried a single ``claim:`` line and
    was rebased four times in one afternoon on this file alone, until the
    workaround of dropping its roadmap commit entirely was found.

    Two lines used to do it, and both are gone:

    * ``Generated <ISO timestamp> from **files**.`` — a clock reading, so it
      differed between any two branches, forever, on the same line. It was also
      the one line ``sync --check`` and ``test_committed_markdown_is_in_sync``
      both stripped before comparing: the single line guaranteeing the conflict
      was the single line the drift guard had already declared meaningless. Git
      answers the freshness question better anyway — ``git log -1 --format=%cI
      -- roadmap/ROADMAP.md`` is when the CONTENT last changed, not when someone
      last ran the command. Dropping ``source`` with it also makes the db-sourced
      and files-sourced renders byte-identical, which is what
      ``test_both_sources_render_the_same_claim_identically`` wants.
    * The Summary count table (``| ready | 28 |``). Two branches moving a count
      in different directions conflict — a claim (-1 ready) against an item-add
      (+1 ready) is exactly that, and exactly the #1103 shape. The sections below
      list their own items; the count was never worth a guaranteed conflict.

    What remains conflictable is real content: two branches touching
    alphabetically adjacent keys collide in the Ready list, the mermaid node
    list, and the Items section. That conflict names what actually changed, and
    the resolution is to re-run ``roadmap.py sync``.
    """
    derived = {}
    for key, item in by_key.items():
        row = dict(item)
        row["status"] = derive_status(item, by_key)
        derived[key] = row

    problems = validate_graph(by_key)
    lines: list[str] = []
    add = lines.append

    add("# ROADMAP.md — open work items")
    add("")
    add(
        "<!-- GENERATED FILE — DO NOT EDIT BY HAND. "
        f"Regenerate with `{CLI} sync`. -->"
    )
    add("")
    add(
        "This is the agent-readable projection of the roadmap graph; the store "
        "is the `roadmap_items` table (see `roadmap/README.md`). For when it was "
        "last regenerated ask git — `git log -1 --format=%cI -- "
        "roadmap/ROADMAP.md` — because nothing in this file is derived from the "
        "clock or from a graph-wide total, so that two branches editing "
        "different items merge cleanly. Do not add one back."
    )
    add("")
    add(
        "`ARCS.md` is the narrative layer — *why* an arc is open. This file is "
        "the work-item layer — *what* is claimable right now, and who holds it."
    )
    add("")

    if problems:
        add("## ⚠️ Graph problems")
        add("")
        add("These make the graph untrustworthy; fix before relying on it.")
        add("")
        for problem in problems:
            add(f"- {problem}")
        add("")

    # Queue order, not key order: the Ready section is what a session acts on,
    # and if it listed alphabetically while `ready_items` sorted by priority the
    # markdown and the API would disagree about which item is the head — the one
    # thing every surface here exists to agree on. Applied to every bucket so a
    # reader does not have to learn which sections are ordered which way. With
    # nothing prioritised this is exactly the old key order.
    buckets: dict[str, list[dict[str, Any]]] = {s: [] for s in STATUSES}
    for item in sorted(derived.values(), key=queue_sort_key):
        buckets[item["status"]].append(item)

    ready = buckets["ready"]
    add("## ▶ Ready — startable now")
    add("")
    if not ready:
        add("_Nothing ready: everything is claimed, blocked, or done._")
    else:
        add(f"Claim before starting: `{CLI} claim <key>`")
        add("")
        add(
            "**In priority order, most important first.** An item with no marker "
            "carries no stated priority — take it as unjudged, not as low. The "
            "order within a band is alphabetical and means nothing."
        )
        add("")
        for item in ready:
            marker = f"`{priority_of(item)}` " if priority_of(item) else ""
            add(f"- {marker}**`{item['key']}`** — {item['title']}")
            # Rendered *here*, in the queue, not only in the Items section far
            # below. A relation nobody reads before claiming is the state this
            # edge replaced — it was already written down, in prose, in exactly
            # the place a session picking up work does not look.
            for rel in relations_for(item["key"], by_key):
                note = f" — {rel['note']}" if rel["note"] else ""
                add(f"  - ↔ related: **`{rel['key']}`**{note}")
            # A merge warning, not a reason it is unlisted. The item stays in
            # the queue: this is an ordering cost, and pulling it would
            # serialize work that runs in parallel perfectly well.
            for clash in artifact_contention(item["key"], by_key):
                tokens = ", ".join(f"`{t}`" for t in clash["artifacts"])
                add(
                    f"  - ⚠ merge contention with **`{clash['key']}`** "
                    f"({clash['claimed_by']}) on {tokens} — startable, but one "
                    f"of you rebases"
                )
    add("")

    deferred = buckets["deferred"]
    add("## ⏸ Deferred — startable, deliberately not now")
    add("")
    if not deferred:
        add("_Nothing deferred._")
    else:
        # The reason is rendered in full, not summarised. A deferral with its
        # reasoning stripped is indistinguishable from an item nobody got to,
        # which is how a deliberate sequencing call decays into a silent
        # backlog graveyard — the failure this section exists to prevent.
        add(
            "These have no unmet dependency; a session judged them the wrong "
            "thing to pick up *yet*. Disagreeing is allowed — read the reason "
            "first, and if it no longer holds, drop `defer_reason` and say so."
        )
        add("")
        for item in deferred:
            reason = " ".join((item.get("defer_reason") or "").split())
            add(f"- **`{item['key']}`** — {item['title']}  \n  deferred: {reason}")
    add("")

    claimed = buckets["claimed"]
    add("## 🔒 Claimed — someone is on these")
    add("")
    if not claimed:
        add("_Nothing claimed._")
    else:
        add("| item | held by | since |")
        add("|---|---|---|")
        for item in claimed:
            add(
                f"| `{item['key']}` | {item.get('claimed_by') or '?'} "
                f"| {fmt_timestamp(item.get('claimed_at')) or '?'} |"
            )
        add("")
        # Static text, not a computed age. This file is committed and CI fails
        # when it drifts from the graph, so anything here that varies with the
        # clock would re-stale the artifact every midnight and train everyone to
        # regenerate on red. The live surfaces (`roadmap.py ready|list|show`,
        # `GET /admin/roadmap/ready`) do the arithmetic against a real now.
        add(
            f"A claim's only trace is this row, so a session that merged or died "
            f"leaves its hold behind forever and the item silently stops being offered "
            f"to anyone. If **since** is more than {STALE_CLAIM_DAYS} days ago, check "
            f"whether that branch still has work in flight before assuming the hold is "
            f"live — if it is merged or gone, `{CLI} release <key>`."
        )
    add("")

    blocked = buckets["blocked"]
    add("## ⛔ Blocked")
    add("")
    if not blocked:
        add("_Nothing blocked._")
    else:
        for item in blocked:
            deps = ", ".join(f"`{d}`" for d in unmet_deps(item, by_key)) or "?"
            add(f"- **`{item['key']}`** — {item['title']}  \n  waiting on {deps}")
    add("")

    if any(len(b) for b in buckets.values()):
        add("## Dependency graph")
        add("")
        add("```mermaid")
        add("graph TD")
        for key in sorted(derived):
            label = derived[key]["title"].replace('"', "'")
            add(f'  {_node(key)}["{label}"]')
        for key in sorted(derived):
            for dep in derived[key].get("blocked_on") or []:
                add(f"  {_node(dep)} --> {_node(key)}")
        # Dashed and undirected, so the two edge types cannot be misread for
        # each other at a glance: an arrow means "wait", a dashed line means
        # "these two touch". Drawn once per pair — see ``relation_pairs``.
        for left, right in relation_pairs(derived):
            add(f"  {_node(left)} -.- {_node(right)}")
        add("```")
        add("")

    add("## Items")
    add("")
    for key in sorted(derived):
        item = derived[key]
        add(f"### `{key}`")
        add("")
        add(f"- **title:** {item['title']}")
        add(f"- **status:** {item['status']}")
        if item.get("arc"):
            add(f"- **arc:** {item['arc']}")
        if priority_of(item):
            add(f"- **priority:** {priority_of(item)}")
        if item.get("claimed_by"):
            since = fmt_timestamp(item.get("claimed_at")) or "?"
            add(f"- **claimed by:** {item['claimed_by']} (since {since})")
        if item.get("blocked_on"):
            add(f"- **blocked on:** {', '.join(f'`{d}`' for d in item['blocked_on'])}")
        if (item.get("defer_reason") or "").strip():
            add(f"- **deferred:** {' '.join(item['defer_reason'].split())}")
        blocks = sorted(
            k for k, other in by_key.items() if key in (other.get("blocked_on") or [])
        )
        if blocks:
            add(f"- **blocks:** {', '.join(f'`{b}`' for b in blocks)}")
        # Both directions, so an item never has to be read alongside the one
        # that happened to declare the edge to know it is not independent.
        related = relations_for(key, by_key)
        if related:
            add("- **related to** (not a dependency — both are startable):")
            for rel in related:
                note = f" — {rel['note']}" if rel["note"] else ""
                add(f"  - `{rel['key']}`{note}")
        artifacts = normalize_artifacts(item)
        if artifacts:
            add(f"- **artifacts:** {', '.join(f'`{a}`' for a in artifacts)}")
        if item.get("refs"):
            add("- **refs:**")
            for ref in item["refs"]:
                add(f"  - `{ref}`")
        if item.get("evidence_checked_at"):
            add(f"- **evidence checked:** {fmt_timestamp(item['evidence_checked_at'])}")
        evidence = (item.get("evidence") or "").strip()
        if evidence:
            add("")
            add("<details><summary>evidence</summary>")
            add("")
            for line in evidence.splitlines():
                add(f"> {line}" if line.strip() else ">")
            add("")
            add("</details>")
        add("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Arcs — the narrative layer, with a schema
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. `ARCS.md` was 1449 lines of hand-written prose that nothing
# executed and nothing checked, sitting directly above a work-item layer that
# had a store, a schema and a validator. The two drifted in the only direction
# that is invisible: measured 2026-08-16, ten of eighteen arcs had zero items
# and seven of those were marked "open tail" — the narrative claimed pending
# work the queue could not offer a single item for, and nothing could say so.
#
# Arcs are dicts keyed by `key`, exactly like items, for the same reason (one
# shape serves DB rows, API payloads and parsed YAML with no adapter).

#: The four states an arc can be in, and what each one asserts.
#:
#: Deliberately the same four the hand-written legend already used, because the
#: legend was not the problem — the absence of anything checking it was.
ARC_STATES = ("open", "blocked", "dark", "closed")

#: Which states a human may DECLARE, and why `open` is not one of them.
#:
#: `open` is the fallback — what an arc reads when nothing else applies — so
#: declaring it would state the default and suppress every check that fires on
#: it. The other three are each about something the items cannot show:
#:
#: * `dark` — "code merged, flag off **in prod env**" is a fact about the
#:   environment, not about the graph. No amount of item-reading finds it.
#: * `closed` — subtle, and discovered by measurement rather than by design.
#:   "All items done" looks derivable, and is, right up until `prune` deletes the
#:   done items: then a finished arc and an arc nobody ever filed items for are
#:   the same empty set. Measured across the 2026-08-16 prune, the empty-arc
#:   count went 9 -> 10 and `entity-graph` joined it *because its only item
#:   finished*. So closure is stated once, by whoever knows, rather than
#:   re-inferred from a population that legitimately empties.
#: * `blocked` — derivable ONLY for blockage inside the graph (every unfinished
#:   item is itself blocked). Blockage on something outside it — an approval, a
#:   third party, a decision nobody has taken — has no items to read, and the
#:   distinction is not cosmetic: `billing` sat 🔵 in the hand-written file
#:   waiting on Paddle with zero items filed, and derivation alone called it
#:   `open` and advised "file the work, or declare it closed", which is wrong
#:   advice for an arc whose blocker is a company. Declared `blocked` says the
#:   thing derivation cannot.
DECLARABLE_ARC_STATES = ("dark", "closed", "blocked")

#: Rendered marker per state. Matches the legend ARCS.md always carried, so a
#: reader of the generated file sees what they saw before.
ARC_STATE_MARKERS = {
    "open": "🟠",
    "blocked": "🔵",
    "dark": "🟡",
    "closed": "🟢",
}

#: One line each, rendered into the generated legend so the file explains itself
#: without a hand-maintained table drifting from the code that assigns them.
ARC_STATE_MEANINGS = {
    "open": "Open tail — unfinished items, or a stated unresolved decision.",
    "blocked": (
        "Nothing startable — every unfinished item is blocked, or the "
        "blocker is outside the graph and was declared."
    ),
    "dark": "Code merged, flag off **in prod env**. Declared, never derived.",
    "closed": "Tail is empty and somebody said so. Declared, never derived.",
}

#: Statuses that mean "this item is finished". Kept next to the arc logic
#: because closure questions are asked in terms of it, and spelling it twice is
#: how the two answers diverge.
#:
#: `verifying` is NOT here, on purpose: it means the code shipped and nothing has
#: confirmed the effect in prod, which is precisely the state CLAUDE.md's
#: "deployed != doing anything" pitfall exists to keep out of the done column.
#: An arc whose every item is `verifying` still has open questions.
_FINISHED_STATUSES = ("done",)


def arc_of(item: dict[str, Any]) -> str | None:
    """The arc key an item belongs to, or ``None``.

    Normalised through one accessor so a blank string and a missing key are the
    same thing everywhere — three of 41 items carried no arc when this landed,
    and two spellings of "no arc" would each need handling at every call site.
    """
    value = (item.get("arc") or "").strip()
    return value or None


def items_in_arc(arc_key: str, by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every item declaring this arc, key-ordered."""
    return [by_key[k] for k in sorted(by_key) if arc_of(by_key[k]) == arc_key]


def declared_arc_state(arc: dict[str, Any]) -> str | None:
    """What a human said this arc's state is, or ``None`` for "derive it"."""
    value = (arc.get("state") or "").strip()
    return value or None


def derive_arc_state(arc: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> str:
    """The arc's state: declared where it must be, derived where it can be.

    A declaration wins outright. That is not laziness about validation — a
    declared state that disagrees with the items is a *finding*, reported by
    ``validate_arcs``, and silently overriding it here would hide the very
    disagreement worth surfacing (an arc called closed while items are still
    open is a statement somebody should have to withdraw, not a value quietly
    corrected).

    Otherwise: every item finished reads `closed`; no startable item reads
    `blocked`; anything else reads `open`. An arc with no items at all reads
    `open`, which is the whole point — an arc nobody has filed work for while
    claiming a tail is the drift this layer was built to expose, and it is only
    *not* drift when somebody declares closure.
    """
    declared = declared_arc_state(arc)
    if declared in DECLARABLE_ARC_STATES:
        return declared

    items = items_in_arc(arc.get("key") or arc.get("id") or "", by_key)
    if not items:
        return "open"

    unfinished = [i for i in items if derive_status(i, by_key) not in _FINISHED_STATUSES]
    if not unfinished:
        return "closed"
    if all(derive_status(i, by_key) == "blocked" for i in unfinished):
        return "blocked"
    return "open"


def startable_in_arc(arc_key: str, by_key: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Items in this arc a session could pick up right now.

    Same definition ``ready_items`` uses, restricted to the arc — so "this arc
    offers nothing" means exactly what `ready` would tell a session, rather than
    a second nearly-identical notion of startable.
    """
    ready_keys = {i["key"] for i in ready_items(by_key)}
    return [i for i in items_in_arc(arc_key, by_key) if i["key"] in ready_keys]


def validate_arcs(
    arcs: dict[str, dict[str, Any]], by_key: dict[str, dict[str, Any]]
) -> list[str]:
    """Ways the arc layer is MALFORMED. Empty == coherent.

    Deliberately separate from ``arc_findings`` below, and the split is the whole
    reason both are useful.

    What lives here is a graph that does not hold together: an item pointing at
    an arc that does not exist, a state declared that is only ever derived, a
    closure contradicted by its own items, a declaration with no evidence. Each
    is a mistake with a single correct fix, so each is worth failing a build for
    — ``validate`` exits non-zero on these and ``roadmap-sync.yml`` goes red.

    What does NOT live here is "this arc has no startable work". That is a
    coherent graph describing a thin backlog, and failing CI hourly on it would
    make a permanent red the normal state — which is how a guard gets learned as
    noise and then ignored, the same failure mode this repo documents for
    warnings on destructive commands. It is a finding, surfaced where somebody
    reads it, not an error.
    """
    problems: list[str] = []

    for key in sorted(arcs):
        arc = arcs[key]
        declared = declared_arc_state(arc)
        items = items_in_arc(key, by_key)
        unfinished = [i for i in items if derive_status(i, by_key) not in _FINISHED_STATUSES]

        if declared is not None and declared not in DECLARABLE_ARC_STATES:
            problems.append(
                f"arc {key}: state {declared!r} is derived, not declared — "
                f"only {', '.join(DECLARABLE_ARC_STATES)} may be stated; omit it "
                f"and the items decide"
            )

        # A declaration the items contradict. Reported rather than corrected in
        # `derive_arc_state`, so somebody has to withdraw the claim instead of it
        # being quietly overwritten.
        if declared == "closed" and unfinished:
            problems.append(
                f"arc {key}: declared `closed` but {len(unfinished)} item(s) are "
                f"unfinished ({', '.join(i['key'] for i in unfinished[:3])}"
                f"{'…' if len(unfinished) > 3 else ''})"
            )

        # A declared state is a claim about the world, and README rule 2 applies
        # to it for the same reason it applies to an item: `dark`, `closed` and
        # `blocked` are all statements about prod or about a pending decision,
        # and all three go stale silently.
        if declared is not None and not (arc.get("state_evidence") or "").strip():
            problems.append(
                f"arc {key}: state {declared!r} is declared with no "
                f"`state_evidence` — say what was checked and when "
                f"(roadmap/README.md rule 2)"
            )

        if not (arc.get("title") or "").strip():
            problems.append(f"arc {key}: no title")

    # An item pointing at an arc that does not exist. Before this layer, `arc`
    # was unvalidated free text that nothing read; no item happened to be wrong,
    # which was luck rather than a guarantee — and it stopped being true within
    # hours: `cog-plan-richness-intervention` arrived pointing at `cognition`,
    # an arc ARCS.md had no section for.
    for key in sorted(by_key):
        arc_key = arc_of(by_key[key])
        if arc_key is not None and arc_key not in arcs:
            problems.append(
                f"{key}: arc {arc_key!r} does not exist — add roadmap/arcs/"
                f"{arc_key}.yaml, or point the item at an arc that is there"
            )

    return problems


def arc_findings(
    arcs: dict[str, dict[str, Any]], by_key: dict[str, dict[str, Any]]
) -> list[str]:
    """Arcs claiming a tail the queue cannot offer anything for.

    THE MEASURED FAILURE THIS LAYER EXISTS FOR. On 2026-08-16, nine of eighteen
    arcs had zero items and seven of those read "open tail" — the narrative said
    work was pending and no session could be handed any of it, with no artifact
    able to say so.

    Keyed on the arc's own state, never on a count of items. A healthy arc's
    population empties every time its work finishes — measured across the
    2026-08-16 prune, `entity-graph` went to zero items *because its only item
    completed* — so a count-based check reports success as drift, every prune,
    forever. Only an arc that still reads `open` while offering nothing is
    saying something contradictory.

    Reported, never failed: see ``validate_arcs``.
    """
    findings: list[str] = []
    for key in sorted(arcs):
        if derive_arc_state(arcs[key], by_key) != "open":
            continue
        if startable_in_arc(key, by_key):
            continue
        items = items_in_arc(key, by_key)
        unfinished = [i for i in items if derive_status(i, by_key) not in _FINISHED_STATUSES]
        statuses = [derive_status(i, by_key) for i in unfinished]

        # AN ARC AWAITING CONFIRMATION IS NOT AN IDLE ARC, and this exclusion is
        # what keeps the check worth reading. `verifying` means the code shipped
        # and nobody has confirmed the effect in prod yet — there is *supposed* to
        # be nothing startable, because the next move is an observation, not a
        # commit. Telling such an arc to "file the work or declare it closed" is
        # wrong on both counts.
        #
        # Measured on the first real run, 2026-08-16: 4 of 13 findings were this
        # case (`assistant-voice`, `cognition`, `self-revising-preferences`,
        # `world-model` — the last with four items in flight). A 31% false-positive
        # rate on a report whose whole job is to be believed is how a check gets
        # skimmed and then ignored, which costs more than the check ever earned.
        if "verifying" in statuses:
            continue

        if not items:
            findings.append(
                f"arc {key}: reads `open` with NO items at all — file the work it "
                f"claims is pending, or declare it `closed`/`blocked` with "
                f"evidence. Nothing here can be started, or even named."
            )
        else:
            # Distinct message from the empty case, because the fix is different:
            # the work is named, somebody parked it, and the question is whether
            # that was deliberate. "Declare it closed" would be wrong advice — the
            # items exist and are unfinished.
            parked = ", ".join(f"{s}×{statuses.count(s)}" for s in sorted(set(statuses)))
            findings.append(
                f"arc {key}: reads `open`, nothing in flight, nothing startable — "
                f"all {len(unfinished)} unfinished item(s) are parked ({parked}). "
                f"If the whole arc is waiting on something outside the graph, "
                f"declare it `blocked` with evidence; otherwise un-park one, "
                f"because right now the arc claims a tail nobody can pick up."
            )
    return findings


def orphan_items(by_key: dict[str, dict[str, Any]]) -> list[str]:
    """Items carrying no arc at all, key-ordered.

    Not a `validate_arcs` problem: an item without an arc is startable, correct
    and common, and failing the graph over it would be noise. It is reported in
    the render so the narrative layer's coverage gap is visible where somebody
    reads it.
    """
    return [k for k in sorted(by_key) if arc_of(by_key[k]) is None]


def render_arcs_markdown(
    arcs: dict[str, dict[str, Any]], by_key: dict[str, dict[str, Any]]
) -> str:
    """The narrative projection: every arc, its state, and its live items.

    **The same clock/aggregate ban as ``render_markdown`` applies, for the same
    reason.** This file is regenerated wholesale and committed, so a timestamp or
    a graph-wide count would differ between any two branches that ran `sync` and
    conflict on merge while saying nothing about what either changed. That cost
    four rebases on ROADMAP.md in one afternoon (#1103); reintroducing it on a
    second generated file would be repeating a known mistake, not discovering a
    new one. Per-arc counts are fine — they change only when that arc changes.
    """
    lines: list[str] = []
    add = lines.append

    add("# ARCS.md — what's in flight, at arc level")
    add("")
    add(
        "<!-- GENERATED FILE — DO NOT EDIT BY HAND. "
        f"Regenerate with `{CLI} sync`. "
        "Edit roadmap/arcs/*.yaml instead. -->"
    )
    add("")
    add(
        "The narrative layer above `roadmap/ROADMAP.md`: *why* each theme is "
        "still open. Work items live in the roadmap graph and are listed per arc "
        "below; the prose here is the arc's own, and is the one place a "
        "multi-PR theme gets explained rather than enumerated. For when this was "
        "last regenerated ask git — `git log -1 --format=%cI -- ARCS.md` — "
        "because nothing here derives from the clock or from a graph-wide total, "
        "so two branches editing different arcs merge cleanly."
    )
    add("")

    problems = validate_arcs(arcs, by_key)
    if problems:
        add("## ⚠️ Problems")
        add("")
        add(
            "The arc layer does not hold together. Each of these has one correct "
            "fix and fails `roadmap.py validate`."
        )
        add("")
        for problem in problems:
            add(f"- {problem}")
        add("")

    findings = arc_findings(arcs, by_key)
    if findings:
        add("## Arcs claiming work the queue cannot offer")
        add("")
        add(
            "Rendered high because it is the failure this layer was built to "
            "catch: an arc asserting a tail that no session can be handed. Not a "
            "validation error — the graph is coherent, the backlog is thin — so "
            "it does not fail a build. Each one is a decision: file the work, or "
            "declare the arc `closed`/`blocked` with evidence."
        )
        add("")
        for finding in findings:
            add(f"- {finding}")
        add("")

    add("## Legend")
    add("")
    add("| State | Meaning |")
    add("|---|---|")
    for state in ARC_STATES:
        add(f"| {ARC_STATE_MARKERS[state]} {state} | {ARC_STATE_MEANINGS[state]} |")
    add("")
    add(
        "`dark`, `closed` and `blocked` may be **declared** by a human with dated "
        "evidence, because each is about something the items cannot show: an "
        "environment flag, a closure whose finished items `prune` has deleted, or "
        "a blocker outside the graph entirely. `blocked` is *also* derived when "
        "every unfinished item is itself blocked. `open` is never declared — it "
        "is the fallback every check fires on, so stating it would only silence "
        "them."
    )
    add("")

    by_state: dict[str, list[str]] = {state: [] for state in ARC_STATES}
    for key in sorted(arcs):
        by_state[derive_arc_state(arcs[key], by_key)].append(key)

    for state in ARC_STATES:
        keys = by_state[state]
        if not keys:
            continue
        add(f"## {ARC_STATE_MARKERS[state]} {state.capitalize()}")
        add("")
        for key in keys:
            arc = arcs[key]
            items = items_in_arc(key, by_key)
            startable = startable_in_arc(key, by_key)
            add(f"### {ARC_STATE_MARKERS[state]} {arc.get('title') or key}")
            add("")
            add(f"`{key}` · {len(items)} item(s), {len(startable)} startable")
            add("")

            declared = declared_arc_state(arc)
            if declared is not None:
                add(f"**Declared `{declared}`.** {(arc.get('state_evidence') or '').strip()}")
                add("")

            refs = [str(r).strip() for r in (arc.get("refs") or []) if str(r).strip()]
            if refs:
                add(" · ".join(f"`{ref}`" for ref in refs))
                add("")

            narrative = (arc.get("narrative") or "").strip()
            if narrative:
                for line in narrative.splitlines():
                    add(line)
                add("")

            if items:
                add("| item | status | priority |")
                add("|---|---|---|")
                for item in sorted(items, key=queue_sort_key):
                    add(
                        f"| `{item['key']}` | {derive_status(item, by_key)} "
                        f"| {priority_of(item) or '—'} |"
                    )
                add("")

    orphans = orphan_items(by_key)
    if orphans:
        add("## Items with no arc")
        add("")
        add(
            "Startable and legitimate — an item does not need an arc. Listed so "
            "the narrative layer's coverage gap is visible rather than implied."
        )
        add("")
        for key in orphans:
            add(f"- `{key}` — {by_key[key].get('title') or ''}")
        add("")

    return "\n".join(lines).rstrip() + "\n"

def compare_arc_sources(
    db: dict[str, dict[str, Any]], files: dict[str, dict[str, Any]]
) -> list[str]:
    """Every way the arc store and the committed arc files disagree.

    THE GAP THIS CLOSES was created by the change that added arcs and found
    immediately. ``compare_sources`` covers items only, so arc drift was
    undetectable by construction — and it happened on the very first
    reconciliation: `roadmap.py pull` creates an arc file that is missing but
    never UPDATES one that exists, so declaring `integrations` and
    `ssh-cert-broker` blocked in the store left `main`'s files still saying
    nothing, with no command able to report it. That is precisely the failure
    ``roadmap/README.md`` describes as "the two sources drift, and nothing else
    can see it", reproduced one layer up by the layer that was supposed to have
    learned from it.

    Deliberately NOT compared: ``narrative``. It is the arc's prose, it churns on
    every reword, and a prose difference is not something another session
    collides on — the same reasoning ``compare_sources`` applies to ``evidence``.
    What is compared is what changes a decision: does the arc exist, and what does
    it claim its state is.
    """
    problems: list[str] = []
    for key in sorted(set(db) - set(files)):
        problems.append(
            f"arc {key}: in the DB but NOT in roadmap/arcs/ — invisible to any "
            f"agent in a checkout. Fix: `roadmap.py pull`"
        )
    for key in sorted(set(files) - set(db)):
        problems.append(
            f"arc {key}: in roadmap/arcs/ but NOT in the DB — `--source db` "
            f"cannot see it, so the rendered ARCS.md will omit it. "
            f"Fix: `roadmap.py push`"
        )
    for key in sorted(set(db) & set(files)):
        d_state = declared_arc_state(db[key])
        f_state = declared_arc_state(files[key])
        if d_state != f_state:
            problems.append(
                f"arc {key}: declared state differs — DB={d_state or 'unset'!r}, "
                f"files={f_state or 'unset'!r}. The state decides what the arc "
                f"claims and whether its findings fire, so the two sources are "
                f"telling readers different things."
            )
        if (db[key].get("title") or "") != (files[key].get("title") or ""):
            problems.append(f"arc {key}: title differs between the DB and the files")
    return problems
