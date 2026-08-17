"""Did the fix hold? The rule, separated from where tickets live.

An item claims to answer some tickets. Once it is marked done, the question
worth asking is *is anybody still reporting this on a build at or past the one
that was live when we called it finished* — the failure mode where something is
marked done and the complaint keeps arriving.

**Why this is here rather than in the backend.** This is the one place the
roadmap reads the host application's own data, and so the one place that
decides whether the roadmap can be adopted by a project that is not Lucille.
Split the wrong way, adopting the roadmap means adopting a feedback-ticket
table. Split this way, the roadmap keeps the *rule* and gives up any knowledge
of *what a ticket is*: the host fetches its own tickets, flattens each to the
three fields below, and gets a classification back.

Lucille's adapter reads ``FeedbackTicket``; another project's reads Linear, or
GitHub issues, or nothing at all — the roadmap cannot tell the difference and
has no reason to.

Two coordinates, and time wins. A ticket's stamp and an item's stamp are two
observations of the same deployment sequence, so ``created_at`` against
``done_at`` is the primary test; the version strings are carried through so a
reader can see which build each side was on without a second query.

Stdlib only, like everything else in this package.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

__all__ = [
    "BEFORE",
    "AFTER",
    "UNSTAMPED",
    "ITEM_NOT_DONE",
    "classify",
    "report",
]

#: Filed before the item was marked done. The expected case; needs no attention.
BEFORE = "before"
#: Filed *after* — the complaint recurred past the fix. The finding to act on.
AFTER = "after"
#: No usable stamp on the ticket, so there is nothing to compare. Deliberately
#: distinct from ``BEFORE``: "I compared and it was earlier" and "I could not
#: compare" must not render as the same answer.
UNSTAMPED = "unstamped"
#: Nothing has shipped yet, so no ticket can be after it.
ITEM_NOT_DONE = "item_not_done"


def _moment(value: Any) -> _dt.datetime | None:
    """Accept a datetime or an ISO 8601 string, and treat naive as UTC.

    The host may hand these over as either — a SQLAlchemy row gives datetimes, a
    JSON payload or a YAML file gives strings — and an adapter should not have
    to know which shape this module prefers.
    """
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def classify(*, done_at: Any, created_at: Any) -> str:
    """Which side of *done* one ticket falls on.

    The whole rule, in one testable place. It used to sit inside a function that
    also opened a database session, which meant the only way to test the rule
    was to have the host's schema on hand.
    """
    done = _moment(done_at)
    if done is None:
        return ITEM_NOT_DONE
    filed = _moment(created_at)
    if filed is None:
        return UNSTAMPED
    return AFTER if filed > done else BEFORE


def report(
    *,
    item: dict[str, Any],
    tickets: list[dict[str, Any]],
    version_key: Any = None,
) -> dict[str, Any]:
    """One item, its linked tickets, and which side of ``done`` each falls on.

    ``item`` needs ``key``, ``title``, ``status``, ``arc``, ``done_at``,
    ``done_version`` and ``tickets`` (the ids it claims to answer). ``tickets``
    is whatever the host resolved those ids to, each carrying at least ``id``
    and ``created_at``; anything else it includes is passed through untouched,
    so a host can surface its own fields without this module learning them.

    ``version_key`` reduces a version stamp to a comparable string. It is the
    host's, because a stamp's shape is the host's: Lucille's is
    ``<ISO deploy time>+<short sha>``, and another project's is its own
    business. Omitted, versions are reported as-is.

    An id the host could not resolve is **kept and marked**, never dropped. The
    report's job is to show what the graph says, including that it says
    something unusable — a missing row and a healthy one must not render the
    same.
    """
    key_of = version_key if callable(version_key) else (lambda stamp: stamp)

    resolved = {str(t.get("id")): t for t in tickets if t.get("id") is not None}
    out: list[dict[str, Any]] = []
    for raw in [str(t) for t in (item.get("tickets") or [])]:
        found = resolved.get(raw)
        if found is None:
            out.append({"id": raw, "error": "not resolvable", "impact": UNSTAMPED})
            continue
        entry = {k: v for k, v in found.items() if k != "created_at"}
        created = found.get("created_at")
        entry["id"] = raw
        entry["created_at"] = (
            created.isoformat() if isinstance(created, _dt.datetime) else created
        )
        entry["impact"] = classify(
            done_at=item.get("done_at"), created_at=created
        )
        out.append(entry)

    done_version = item.get("done_version")
    return {
        "key": item.get("key"),
        "title": item.get("title"),
        "status": item.get("status"),
        "arc": item.get("arc"),
        "done_at": (
            item["done_at"].isoformat()
            if isinstance(item.get("done_at"), _dt.datetime)
            else item.get("done_at")
        ),
        "done_version": key_of(done_version),
        "done_modules": dict((done_version or {}).get("modules") or {}),
        "tickets": out,
        # The number to read. Anything above zero is somebody reporting the same
        # thing on a build at or past the one that was live when this was called
        # finished.
        "recurred_after_done": sum(1 for t in out if t.get("impact") == AFTER),
    }
