"""`pull` must carry the statuses the graph cannot derive — and only add them.

`derive_status` names four facts the edges cannot produce: ``done``,
``verifying``, an active claim and a deferral. `pull` carried two of them (the
claim and ``done``); ``verifying`` post-dates it and was never added. So a
checkout could read ``ready`` for work that had already shipped, and `pull`
would print "the files already match the store" while `diff` — the assertion
the sync workflow is built on — reported the divergence in the same second.

The second half of this file is the harder half, and it is why the projection
is DELIBERATELY ONE-DIRECTIONAL. The store's silence about ``verifying`` is
IGNORANCE, NOT DENIAL: the backend honors ``status`` on INSERT and ignores it
on UPDATE, so a file's ``verifying`` has no path into the store at all. A
symmetric "the store wins" projection would therefore erase every such mark.

Measured in Lucille from the two committed artifacts, 2026-09-17: ONE item
where the store holds the underivable status and the file does not (which this
fix heals), against SEVENTEEN where the file holds it and the store does not —
eight of them sitting at store-status ``ready``. Projecting the store over the
files would have deleted seventeen records and put eight shipped items back on
the startable queue, which is the exact harm this item was filed about,
manufactured by its own fix.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml", reason="pull reads and writes roadmap/items/*.yaml")

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

ITEM = """\
id: first-thing
title: Try the roadmap in a project that is not Lucille
status: ready
evidence: |
  Adopting should take a checkout and nothing else.
"""


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "myproject"
    (root / "roadmap" / "items").mkdir(parents=True)
    (root / "roadmap" / "items" / "first-thing.yaml").write_text(ITEM)
    return root


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PACKAGE_ROOT)
    env["ROADMAP_SOURCE"] = "local"
    for name in ("LUCILLE_ADMIN_JWT", "ROADMAP_API_TOKEN", "ROADMAP_API_URL", "BACKEND_URL"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "roadmap_core.cli", *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )


def status_in_file(project: Path, key: str = "first-thing") -> str | None:
    for line in (project / "roadmap" / "items" / f"{key}.yaml").read_text().splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return None


def set_status_in_file(project: Path, value: str, key: str = "first-thing") -> None:
    path = project / "roadmap" / "items" / f"{key}.yaml"
    path.write_text(
        "\n".join(
            f"status: {value}" if line.startswith("status:") else line
            for line in path.read_text().splitlines()
        )
        + "\n"
    )


# --- the defect ---------------------------------------------------------------


def test_pull_projects_verifying_from_the_store_onto_the_file(project):
    """The whole item. A store that knows an item is `verifying` must be able to
    tell a checkout, or every tokenless session — which is how dispatch runs BY
    DESIGN — reads `ready` for work that shipped days ago."""
    assert run(project, "push").returncode == 0
    assert run(project, "status", "first-thing", "verifying").returncode == 0

    # `status` writes the file as well as the store, so undo that half: this is
    # the state a checkout reaches whenever the store moved and the file did not
    # — an item set `verifying` from another session, or straight against the API.
    set_status_in_file(project, "ready")
    assert status_in_file(project) == "ready"

    pulled = run(project, "pull")
    assert pulled.returncode == 0, pulled.stderr
    assert status_in_file(project) == "verifying", (
        "the store holds `verifying`, the file still reads `ready`, and `pull` — "
        "the command whose entire job is to bring the store's reality back into "
        f"the checkout — left it there:\n{pulled.stdout}{pulled.stderr}"
    )


def test_pull_does_not_claim_agreement_that_diff_denies(project):
    """`pull` and `diff` are two halves of one workflow: roadmap-sync runs `pull`
    to heal, then `diff` to assert the heal worked. When `pull` prints "the files
    already match the store" and `diff` reports a divergence on the same two
    sources seconds later, one of them is lying and the workflow is red forever
    with no automatic remedy."""
    assert run(project, "push").returncode == 0
    assert run(project, "status", "first-thing", "verifying").returncode == 0
    set_status_in_file(project, "ready")

    pulled = run(project, "pull")
    diffed = run(project, "diff")

    assert not (
        "already match the store" in pulled.stdout and diffed.returncode == 1
    ), (
        "`pull` asserted the files already match the store, and `diff` "
        "immediately reported a real divergence:\n"
        f"pull : {pulled.stdout.strip()}\n"
        f"diff : {diffed.stdout.strip()}"
    )


# --- the direction it must NOT go ---------------------------------------------


def test_pull_does_not_erase_a_verifying_the_store_never_heard_of(project):
    """THE GUARD ON THE FIX, and the reason it is one-directional.

    The backend honors `status` on INSERT and ignores it on UPDATE, so a file's
    `verifying` cannot reach an existing item in the store — the store's `ready`
    is ignorance, not a denial. Projecting it back over the file would delete the
    only record that the work shipped, and hand the item to the next session as
    startable. In Lucille that is 17 items, 8 of them store-`ready`."""
    assert run(project, "push").returncode == 0
    set_status_in_file(project, "verifying")

    pulled = run(project, "pull")
    assert pulled.returncode == 0, pulled.stderr
    assert status_in_file(project) == "verifying", (
        "`pull` overwrote a file's `verifying` with the store's `ready`. The "
        "store cannot be TOLD about `verifying` (honored on INSERT, ignored on "
        "UPDATE), so this deletes the record and re-offers shipped work:\n"
        f"{pulled.stdout}{pulled.stderr}"
    )


def test_pull_does_not_downgrade_a_done_file_to_verifying(project):
    """Same rule one step further along. `done` is more settled than `verifying`;
    a store that has only heard `verifying` must not walk a file's `done` back."""
    assert run(project, "push").returncode == 0
    assert run(project, "status", "first-thing", "verifying").returncode == 0
    set_status_in_file(project, "done")

    pulled = run(project, "pull")
    assert pulled.returncode == 0, pulled.stderr
    assert status_in_file(project) == "done", (
        f"`pull` walked a `done` back to `verifying`:\n{pulled.stdout}{pulled.stderr}"
    )


def test_pull_still_projects_done(project):
    """The behaviour that already existed, asserted so the generalisation cannot
    quietly drop it."""
    assert run(project, "push").returncode == 0
    assert run(project, "status", "first-thing", "done").returncode == 0
    set_status_in_file(project, "ready")

    pulled = run(project, "pull")
    assert pulled.returncode == 0, pulled.stderr
    assert status_in_file(project) == "done", pulled.stdout + pulled.stderr


def test_pull_says_which_status_it_projected(project):
    """A repair that leaves no trace hides the next regression exactly as this one
    hid itself. `updated <file>` alone does not say a status moved, so the one
    line a reader gets must name the transition."""
    assert run(project, "push").returncode == 0
    assert run(project, "status", "first-thing", "verifying").returncode == 0
    set_status_in_file(project, "ready")

    pulled = run(project, "pull")
    assert "ready -> verifying" in pulled.stdout, (
        "`pull` healed a status and reported only that the file was updated:\n"
        f"{pulled.stdout}"
    )
