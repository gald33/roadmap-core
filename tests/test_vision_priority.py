"""`vision` — a priority for a direction, not a piece of work.

org-core's item plots (0.60.16, ``org_core/itemplots.py``) draw a ``vision`` band,
and the operator asked on 2026-10-10 for "a 'vision' category, together with
empty". roadmap-core accepted only ``now|next|later``, so no item could enter that
band: the files failed ``validate``, the server answered 400, and the band was
empty by construction rather than by judgement.

Two properties, and each test says what broken looks like:

* ``vision`` is a valid priority on every path that reads or writes one, and it
  ranks after ``later``.
* A ``vision`` item is NOT startable. It derives ``deferred`` — out of ``ready``,
  the MCP tool and the API at once, because all three read ``ready_items`` — with
  a stated reason, so the Deferred section never shows a blank one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from roadmap_core import graph, server
from roadmap_core.stores import LocalStore

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _item(key: str, **fields: Any) -> dict[str, Any]:
    return {"key": key, "title": key.replace("-", " "), "status": "ready", "evidence": "measured",
            **fields}


def _graph(*items: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["key"]: i for i in items}


# --- the graph ----------------------------------------------------------------


def test_a_vision_item_validates():
    """Broken looks like: `unknown priority 'vision'` — the band org-core draws
    could not hold a single item."""
    assert graph.validate_graph(_graph(_item("north-star", priority="vision"))) == []


def test_vision_sorts_after_later_and_after_unjudged():
    """Broken looks like: vision ranking with the unjudged (the fallback for an
    unknown value), i.e. ABOVE `later` — a direction outranking work somebody
    explicitly said is startable."""
    items = [
        _item("a-vision", priority="vision"),
        _item("b-later", priority="later"),
        _item("c-unjudged"),
        _item("d-now", priority="now"),
    ]
    assert [i["key"] for i in sorted(items, key=graph.queue_sort_key)] == [
        "d-now", "c-unjudged", "b-later", "a-vision",
    ]


def test_a_vision_item_is_not_startable():
    """Broken looks like: a vision item at the bottom of `ready`, where an idle
    session that has drained everything else claims it and starts building a
    direction nobody has broken into work."""
    by_key = _graph(_item("north-star", priority="vision"), _item("real-work", priority="later"))
    assert graph.derive_status(by_key["north-star"], by_key) == "deferred"
    assert [i["key"] for i in graph.ready_items(by_key)] == ["real-work"]


def test_a_vision_item_says_why_it_is_deferred():
    """Broken looks like: `deferred: ` with nothing after it — the exact
    indistinguishable-from-forgotten line the Deferred section exists to prevent."""
    by_key = _graph(_item("north-star", title="North star", priority="vision"))
    md = graph.render_markdown(by_key)
    deferred = md.split("## ⏸ Deferred", 1)[1].split("\n## ", 1)[0]
    assert "north-star" in deferred
    line = next(ln for ln in deferred.splitlines() if "deferred:" in ln)
    assert line.split("deferred:", 1)[1].strip(), deferred


def test_an_explicit_defer_reason_still_wins_on_a_vision_item():
    item = _item("north-star", priority="vision", defer_reason="waiting on the Q1 plan")
    assert graph.deferral_reason(item) == "waiting on the Q1 plan"


def test_dependency_still_beats_vision():
    """`blocked` is the harder reason and must not be hidden behind the softer one."""
    by_key = _graph(_item("north-star", priority="vision", blocked_on=["first"]), _item("first"))
    assert graph.derive_status(by_key["north-star"], by_key) == "blocked"


# --- files -> DB -> files --------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    pytest.importorskip("yaml", reason="push/pull read and write roadmap/items/*.yaml")
    root = tmp_path / "myproject"
    (root / "roadmap" / "items").mkdir(parents=True)
    (root / "roadmap" / "items" / "north-star.yaml").write_text(
        "id: north-star\ntitle: Where this is all going\nstatus: ready\n"
        "priority: vision\nevidence: |\n  The operator, 2026-10-10.\n"
    )
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


def test_vision_round_trips_files_to_db_to_files(project):
    """Broken looks like: `validate` failing the file, `push` dropping the value,
    or `pull` writing back something other than `vision`."""
    checked = run(project, "validate")
    assert checked.returncode == 0, checked.stdout + checked.stderr

    pushed = run(project, "push")
    assert pushed.returncode == 0, pushed.stdout + pushed.stderr
    db_files = list(project.rglob("*.db"))
    assert db_files, "push --source local wrote no store"
    with LocalStore(db_files[0]) as local:
        assert local.items()["north-star"]["priority"] == "vision"

    # Strip it from the file: now only the store knows, and `pull` must say so.
    path = project / "roadmap" / "items" / "north-star.yaml"
    path.write_text(
        "".join(ln for ln in path.read_text().splitlines(True) if not ln.startswith("priority:"))
    )
    pulled = run(project, "pull")
    assert pulled.returncode == 0, pulled.stdout + pulled.stderr
    assert "priority: vision" in path.read_text().splitlines()

    ready = run(project, "ready")
    assert ready.returncode == 0, ready.stderr
    assert "north-star" not in ready.stdout


# --- the served API ----------------------------------------------------------------


def test_the_server_accepts_vision_and_keeps_it_off_the_queue(tmp_path):
    """Broken looks like: 400 `priority must be one of now, next, later`."""
    import json
    import urllib.request

    srv = server.make_server(tmp_path / "data", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        srv.registry.add_tenant("org")
        tok = srv.registry.create_token("org", label="t", scopes=("read", "write"))[1]

        def call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, method=method, data=data)
            req.add_header("Authorization", f"Bearer {tok}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")

        status, _ = call("PUT", "/admin/roadmap/north-star", {
            "key": "north-star", "title": "north star", "evidence": "x", "priority": "vision",
        })
        assert status in (200, 201)
        _, ready = call("GET", "/admin/roadmap/ready")
        assert [i["key"] for i in ready["items"]] == []
        _, listed = call("GET", "/admin/roadmap")
        row = next(i for i in listed["items"] if i["key"] == "north-star")
        assert row["priority"] == "vision" and row["status"] == "deferred"
        assert listed["problems"] == []
    finally:
        srv.shutdown()
        srv.server_close()
