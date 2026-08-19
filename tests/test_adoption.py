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

ITEM = """\
id: first-thing
title: Try the roadmap in a project that is not Lucille
status: ready
evidence: |
  Adopting should take a checkout and nothing else.
"""


@pytest.fixture
def project(tmp_path):
    """A fresh project: the documented layout, and nothing else.

    No `scripts/` and nothing copied into it. The CLI used to be a file an
    adopter fetched out of Lucille, which this fixture faked by copying it off
    the local disk — so the test proved the tool worked *with Lucille present*,
    which is the one condition an adoption test must not assume. Lucille is a
    private repository; nobody outside it could run that `curl`.
    """
    root = tmp_path / "myproject"
    (root / "roadmap" / "items").mkdir(parents=True)
    (root / "roadmap" / "items" / "first-thing.yaml").write_text(ITEM)
    return root


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
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
        [sys.executable, "-m", "roadmap_core.cli", *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
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

    tree = ast.parse((PACKAGE_ROOT / "roadmap_core" / "cli.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    for banned in ("fastapi", "sqlalchemy", "app", "pgvector", "alembic"):
        assert banned not in imported, f"the CLI imports {banned!r}"


def test_the_root_is_found_from_a_subdirectory(project):
    """It used to be the script's grandparent, so the script had to live at
    `<project>/scripts/roadmap.py` and you found that out by having an empty
    backlog for an afternoon. Now the file is in site-packages, so the root has
    to be discovered — and being run from a subdirectory is ordinary."""
    deep = project / "src" / "inner"
    deep.mkdir(parents=True)
    assert run(project, "push").returncode == 0

    from_below = run(deep, "ready")
    assert "first-thing" in from_below.stdout, (
        f"run from a subdirectory it read an empty backlog:\n"
        f"{from_below.stdout}\n{from_below.stderr}"
    )


def test_a_subdirectory_run_does_not_mint_a_second_store(project):
    """The nastiest version of the same bug, and the reason discovery keys on
    `roadmap/items` rather than on `roadmap/`.

    The store defaulted to a WORKING-DIRECTORY-relative `roadmap/roadmap.db`,
    so a command run one directory down created its own database there — and
    then that directory contained a `roadmap/`, so every later command resolved
    the root to it. Two stores in one checkout means two agents can hold the
    same item, and the transaction that exists to arbitrate never sees the
    other one.
    """
    deep = project / "src" / "inner"
    deep.mkdir(parents=True)
    assert run(project, "push").returncode == 0
    assert run(deep, "claim", "first-thing").returncode == 0

    strays = [p for p in project.rglob("*.db") if p.parent.parent != project]
    assert not strays, f"a second store was created away from the root: {strays}"
    assert (project / "roadmap" / "roadmap.db").exists(), "the real store is unused"




# --- the CI half of adopting it ----------------------------------------------
#
# The tests above prove an agent can run the loop by hand. A project also needs
# CI, and the workflow Lucille runs is 333 lines of self-hosted runner, service
# JWT, deploy-race wait and a bot committing back to `main` — machinery that
# exists entirely because Lucille's store is served and its checkout cannot see
# it. None of it applies to the SQLite floor.
#
# So `templates/roadmap.yml` is what an adopter copies, and these read the
# commands out of that file and run them, rather than trusting that a YAML file
# nobody executes still describes a working tool. A template that has drifted
# from the CLI is worse than no template: it reads as tested.

TEMPLATE = PACKAGE_ROOT / "templates" / "roadmap.yml"


def _template_steps() -> list[list[str]]:
    """The `roadmap` invocations the template runs, in order.

    Read with a regex rather than a YAML parser on purpose — this package's
    tests import nothing outside the stdlib, and the isolation job asserts that
    `yaml` is not even importable.
    """
    import re

    found = re.findall(r"run:\s*roadmap ([^\n]*)", TEMPLATE.read_text())
    assert found, f"no `roadmap` steps found in {TEMPLATE}"
    return [line.strip().split() for line in found]


def test_the_template_is_a_workflow_github_would_accept():
    """It lives outside `.github/workflows/`, so nothing else parses it. A
    template that cannot be copied is worse than none — the adopter finds out
    from a red run in their own repo, on their first day with the tool."""
    yaml = pytest.importorskip("yaml")

    doc = yaml.safe_load(TEMPLATE.read_text())
    assert doc["name"]
    assert True in doc or "on" in doc, "no trigger block"
    steps = doc["jobs"]["roadmap"]["steps"]
    assert any("checkout" in str(step.get("uses", "")) for step in steps)


def test_the_template_declares_the_extra_authoring_needs():
    """`push`, `validate` and `sync` all parse YAML. Installing the bare package
    fails all three on the same import, which is how the first run of Lucille's
    own sync workflow died — before the extra existed to name."""
    assert 'pip install "roadmap-core[files]"' in TEMPLATE.read_text()


def test_the_template_selects_the_store_that_needs_no_server():
    """Without `ROADMAP_SOURCE=local` the CLI expects a served store and asks
    for a credential an adopter has no way to mint."""
    assert "ROADMAP_SOURCE: local" in TEMPLATE.read_text()


def test_every_command_in_the_template_runs_green_on_a_clean_project(project):
    """The template, executed. Each step in order, against a project whose
    committed markdown is up to date — which is the state CI is asserting."""
    # Stand in for the committed ROADMAP.md an adopter would have.
    assert run(project, "push").returncode == 0
    assert run(project, "sync").returncode == 0

    for args in _template_steps():
        result = run(project, *args)
        assert result.returncode == 0, (
            f"`roadmap.py {' '.join(args)}` failed in a clean project:\n"
            f"{result.stdout}\n{result.stderr}"
        )


def test_the_template_goes_red_when_the_committed_markdown_is_stale(project):
    """The check that earns the workflow, asserted by its effect rather than by
    its presence. ROADMAP.md is generated but committed so an agent in a
    checkout can read the backlog with no install and no network — and a
    generated file nobody regenerates is a file that lies."""
    run(project, "push")
    run(project, "sync")

    (project / "roadmap" / "items" / "second-thing.yaml").write_text(
        "id: second-thing\ntitle: Filed without regenerating\nstatus: ready\n"
    )
    run(project, "push")

    stale = run(project, "sync", "--check")
    assert stale.returncode != 0, (
        "a new item reached the store and never reached ROADMAP.md, and the "
        f"check passed anyway:\n{stale.stdout}\n{stale.stderr}"
    )


def test_diff_reconciles_against_the_local_store_without_a_credential(project):
    """`diff` is the assertion a sync workflow is built around, and until it
    took `--source` it always reached for Lucille's admin API — so an adopter
    with `ROADMAP_SOURCE=local` and no server was told to mint a JWT with a
    skill that does not exist in their project."""
    run(project, "push")

    agree = run(project, "diff")
    assert agree.returncode == 0, agree.stdout + agree.stderr
    assert "LUCILLE_ADMIN_JWT" not in agree.stdout + agree.stderr

    # And it still detects real drift rather than passing because it looked
    # nowhere: an item in the store that no file mentions.
    seeded = run(project, "pull")
    assert seeded.returncode == 0, seeded.stderr
    (project / "roadmap" / "items" / "first-thing.yaml").unlink()

    drifted = run(project, "diff")
    assert drifted.returncode == 1, (
        "an item in the store with no file is invisible to every checkout, and "
        f"diff reported agreement:\n{drifted.stdout}\n{drifted.stderr}"
    )
    assert "first-thing" in drifted.stdout + drifted.stderr
