"""Did the fix hold? The rule, tested where no ticket table exists.

These moved out of `backend/tests/test_version_stamps.py` with the rule they
cover. They were always pure — a stub row in, a verdict out — but they could
only run somewhere `app.crud.roadmap` was importable, which meant the rule for
"is anybody still reporting this" was only checkable by a project that already
had Lucille's feedback tickets.

Running them here proves the thing the extraction claims: the roadmap keeps the
rule and gives up any knowledge of what a ticket is.
"""

from __future__ import annotations

import datetime as _dt

from roadmap_core import impact

DONE = _dt.datetime(2026, 8, 3, 17, 28, tzinfo=_dt.timezone.utc)


def test_a_ticket_after_done_is_the_finding():
    """Somebody reporting the same thing on a build at or past the one that was
    live when this was called finished."""
    assert impact.classify(
        done_at=DONE, created_at=DONE + _dt.timedelta(days=4)
    ) == impact.AFTER


def test_a_ticket_before_done_is_the_expected_case():
    assert impact.classify(
        done_at=DONE, created_at=DONE - _dt.timedelta(days=1)
    ) == impact.BEFORE


def test_an_item_that_is_not_done_cannot_have_after_tickets():
    """Nothing shipped, so no complaint can have survived a fix. Classifying
    these as `after` would manufacture recurrences out of open work."""
    assert impact.classify(
        done_at=None, created_at=_dt.datetime(2026, 8, 8, tzinfo=_dt.timezone.utc)
    ) == impact.ITEM_NOT_DONE


def test_an_unstamped_ticket_is_not_quietly_called_early():
    """"I compared and it was earlier" and "I could not compare" must not
    render as the same answer — the second is a gap in the data."""
    assert impact.classify(done_at=DONE, created_at=None) == impact.UNSTAMPED
    assert impact.classify(done_at=DONE, created_at="") == impact.UNSTAMPED
    assert impact.classify(done_at=DONE, created_at="not a date") == impact.UNSTAMPED


def test_iso_strings_and_datetimes_are_both_accepted():
    """A host may hand these over either way — a row gives datetimes, a YAML
    file or JSON payload gives strings — and an adapter should not have to know
    which shape this module prefers."""
    assert impact.classify(
        done_at="2026-08-03T17:28:00Z", created_at="2026-08-07T00:00:00Z"
    ) == impact.AFTER


def test_a_naive_timestamp_is_read_as_utc_rather_than_rejected():
    assert impact.classify(
        done_at="2026-08-03T17:28:00", created_at="2026-08-07T00:00:00"
    ) == impact.AFTER


# --- the report -------------------------------------------------------------


def _item(**kw):
    base = {
        "key": "an-item", "title": "An item", "status": "done", "arc": "some-arc",
        "done_at": DONE, "done_version": {"app_version": "2026-08-03T00:00:00Z+abc",
                                          "modules": {"m": "1.0"}},
        "tickets": [],
    }
    base.update(kw)
    return base


def test_the_number_to_read_counts_only_recurrences():
    report = impact.report(
        item=_item(tickets=["t1", "t2", "t3"]),
        tickets=[
            {"id": "t1", "created_at": DONE + _dt.timedelta(days=1)},
            {"id": "t2", "created_at": DONE - _dt.timedelta(days=1)},
            {"id": "t3", "created_at": DONE + _dt.timedelta(days=9)},
        ],
    )
    assert report["recurred_after_done"] == 2
    assert [t["impact"] for t in report["tickets"]] == [
        impact.AFTER, impact.BEFORE, impact.AFTER,
    ]


def test_an_unresolvable_id_is_kept_and_marked_not_dropped():
    """The report's job is to show what the graph says, including that it says
    something unusable. A silently shorter list reads as a healthy one."""
    report = impact.report(
        item=_item(tickets=["t1", "nonsense"]),
        tickets=[{"id": "t1", "created_at": DONE + _dt.timedelta(days=1)}],
    )
    assert [t["id"] for t in report["tickets"]] == ["t1", "nonsense"]
    bad = report["tickets"][1]
    assert bad["error"] == "not resolvable"
    assert bad["impact"] == impact.UNSTAMPED
    assert report["recurred_after_done"] == 1, "an unusable link is not a recurrence"


def test_host_fields_are_passed_through_untouched():
    """A host surfaces its own columns without this module learning them —
    which is the whole point of the adapter boundary."""
    report = impact.report(
        item=_item(tickets=["t1"]),
        tickets=[{"id": "t1", "created_at": DONE, "source": "slack",
                  "kinds": ["bug"], "anything_at_all": 42}],
    )
    entry = report["tickets"][0]
    assert entry["source"] == "slack"
    assert entry["kinds"] == ["bug"]
    assert entry["anything_at_all"] == 42


def test_the_version_key_is_the_hosts_business():
    """A stamp's shape belongs to the host. Given no reducer, versions are
    reported as-is rather than guessed at."""
    reduced = impact.report(
        item=_item(), tickets=[], version_key=lambda s: (s or {}).get("app_version", ""),
    )
    assert reduced["done_version"] == "2026-08-03T00:00:00Z+abc"
    assert reduced["done_modules"] == {"m": "1.0"}

    raw = impact.report(item=_item(), tickets=[])
    assert isinstance(raw["done_version"], dict), "no reducer, no reduction"


def test_an_item_with_no_tickets_reports_zero_rather_than_nothing():
    report = impact.report(item=_item(tickets=[]), tickets=[])
    assert report["tickets"] == []
    assert report["recurred_after_done"] == 0
    assert report["key"] == "an-item"
