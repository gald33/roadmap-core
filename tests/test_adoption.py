"""Standing the roadmap up in a project that is not Lucille.

Every other test in this package proves a part works. This one proves the
parts add up to a tool somebody else can adopt, which is the only claim the
extraction actually makes and the only one that cannot be checked by testing
components in isolation.

It runs the CLI as a subprocess against a scratch directory with nothing on the
path but this package: no backend, no FastAPI, no SQLAlchemy, no Postgres, no
server, no token. If `pip install roadmap-core` plus a copied script is not
enough to author an item and claim it, this fails.

Written after the first attempt failed twice, in ways nothing else here would
have caught:

  * `_load_graph` loaded `roadmap-core/roadmap_core/graph.py` by a path that
    only exists in Lucille's layout, and raised when it was absent — so a
    project with the package *installed and importable* could not run a single
    command. A package whose CLI refuses to use it is not adoptable.
  * `REPO_ROOT` is the script's grandparent, so the script has to live at
    `<project>/scripts/roadmap.py`. Copy it to the project root and every path
    silently resolves one directory too high: `push` reports "no item files to
    push" while looking at a directory that is not yours.

The second is a convention rather than a bug, and it is asserted here so that
it is a *documented* convention rather than a thing you discover by having an
empty backlog for an afternoon.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# The CLI reads and writes `roadmap/items/*.yaml`, so adoption genuinely needs
# `roadmap-core[files]`. That is NOT installed in the isolation job, which
# asserts `yaml` is absent to prove the library stands alone — a different and
# equally load-bearing claim. So this file skips there and runs in
# `roadmap-core-adoption`, which installs the extra an adopter installs and
# fails if anything skips.
pytest.importorskip(
    "yaml",
    reason="adoption needs roadmap-core[files]; the isolation job proves the "
           "opposite claim and must not have it",
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLI = PACKAGE_ROOT.parent / "scripts" / "roadmap.py"

ITEM = """\
id: first-thing
title: Try the roadmap in a project that is not Lucille
status: ready
evidence: |
  Adopting should take a checkout and nothing else.
"""


@pytest.fixture
def project(tmp_path):
    """A fresh project: the documented layout, and nothing else."""
    if not CLI.exists():  # pragma: no cover - only outside this repo
        pytest.skip("scripts/roadmap.py is not beside this package")
    root = tmp_path / "myproject"
    (root / "scripts").mkdir(parents=True)
    (root / "roadmap" / "items").mkdir(parents=True)
    shutil.copy(CLI, root / "scripts" / "roadmap.py")
    (root / "roadmap" / "items" / "first-thing.yaml").write_text(ITEM)
    return root


def run(project: Path, *args: str) -> subprocess.CompletedProcess:
    """The CLI, with ONLY this package importable.

    A bare PYTHONPATH rather than the ambient environment: inheriting site
    packages would let a stray install of anything satisfy an import this
    package is not allowed to need, and the test would pass while proving the
    opposite of what it claims.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PACKAGE_ROOT)
    env["ROADMAP_SOURCE"] = "local"
    env.pop("LUCILLE_ADMIN_JWT", None)   # no credential
    env.pop("BACKEND_URL", None)         # and no server to reach
    # NOTE: site-packages is still visible to the subprocess. That is why the
    # test above reads the import graph instead of trusting this environment —
    # a stray install here once turned a missing dependency into a green run.
    return subprocess.run(
        [sys.executable, "scripts/roadmap.py", *args],
        cwd=project, env=env, capture_output=True, text=True, timeout=60,
    )


def test_a_project_that_is_not_lucille_can_run_the_whole_loop(project):
    """Author, push, read, claim, release — with no backend and no token.

    One test rather than five, because the claim being made is that the *loop*
    closes. Any step failing leaves the tool unusable, and a green suite of
    four passing steps would say the opposite.
    """
    pushed = run(project, "push")
    assert pushed.returncode == 0, pushed.stderr
    assert "1 item(s)" in pushed.stdout, pushed.stdout

    ready = run(project, "ready")
    assert "first-thing" in ready.stdout, ready.stdout + ready.stderr

    claimed = run(project, "claim", "first-thing")
    assert claimed.returncode == 0, claimed.stderr
    assert "claimed first-thing" in claimed.stdout

    # The claim is projected back into the file, which is what makes it visible
    # to the next agent in a checkout — the store alone is not the handoff.
    assert "claim:" in (project / "roadmap" / "items" / "first-thing.yaml").read_text()

    released = run(project, "release", "first-thing")
    assert released.returncode == 0, released.stderr
    assert "released first-thing" in released.stdout


def test_the_store_is_one_file_inside_the_project(project):
    """No provisioning: the whole store is a file the project carries. If this
    ever became a service, "adoptable" would quietly stop being true."""
    run(project, "push")
    stores = list((project / "roadmap").glob("*.db"))
    assert len(stores) == 1, f"expected one store file, found {stores}"


def test_the_cli_never_reaches_for_the_backend(project):
    """The property, read off the import graph rather than off the environment.

    An earlier version of this checked whether `fastapi` and `sqlalchemy` were
    *importable* and shrugged either way, which asserted nothing. Worse, the
    whole file passed locally on a machine that happened to have PyYAML
    installed and failed in the isolation job that did not — a false pass of
    exactly the kind this file exists to prevent, caused by trusting the
    ambient interpreter.

    So: the environment is not the subject. What the CLI imports is.
    """
    import ast

    tree = ast.parse((project / "scripts" / "roadmap.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    for banned in ("fastapi", "sqlalchemy", "app", "pgvector", "alembic"):
        assert banned not in imported, f"the CLI imports {banned!r}"


def test_the_cli_expects_to_live_in_scripts(project):
    """A convention, asserted so it is documented rather than discovered.

    `REPO_ROOT` is the script's grandparent. Put the script at the project root
    and every path resolves one directory too high — `push` then reports "no
    item files to push" while looking somewhere that is not your project, which
    reads exactly like an empty backlog.
    """
    shutil.copy(project / "scripts" / "roadmap.py", project / "roadmap.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PACKAGE_ROOT)
    env["ROADMAP_SOURCE"] = "local"
    misplaced = subprocess.run(
        [sys.executable, "roadmap.py", "push"],
        cwd=project, env=env, capture_output=True, text=True, timeout=60,
    )
    assert "no item files to push" in misplaced.stdout + misplaced.stderr
