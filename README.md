# roadmap-core

The roadmap graph: status derivation, dependency and relation edges, arc state,
validation, and the markdown renderers behind `roadmap/ROADMAP.md` and `ARCS.md`.

**Stdlib-only and dependency-free**, which is the point rather than a nicety.
Three properties depend on it:

1. `scripts/roadmap.py` loads `roadmap_core/graph.py` **by path**, so a coding
   agent in a checkout can read the backlog with no install, no DB, no admin
   token and no network.
2. The Lucille backend imports the same module, so a status derived by the API
   and a status rendered into the committed markdown cannot disagree — one
   implementation, two callers. That divergence is a failure this repo has been
   bitten by before.
3. It is the extraction seam. The roadmap is being prepared to run as its own
   product whose default store is a single SQLite file with nothing to
   provision; a package that pulled in a web framework or an ORM could not be
   adopted by another repo without adopting Lucille with it.

## Storage

`store.py` is the schema — one SQLite file, `CREATE TABLE IF NOT EXISTS` on first
open, no migration step. `stores.py` is what you talk to:

| | `LocalStore` | `ApiStore` |
|---|---|---|
| needs | a writable path | a `call(method, path, payload)` |
| provisioning | none | a running host |
| `claim`/`release` | one `BEGIN IMMEDIATE` transaction | one HTTP request |
| `impact` | raises `Unsupported` | the host's tickets |

Both satisfy the same `Store` protocol, so a caller never branches on which it
holds. `ApiStore` is constructed with the host's own caller and holds no token,
no URL and no `urllib` import — auth stays entirely a host concern, and
`test_stores.py` asserts that rather than trusting it.

```python
from roadmap_core.stores import LocalStore

with LocalStore("roadmap/roadmap.db") as store:   # created if absent
    store.upsert_item({"key": "a-thing", "title": "A thing"})
    store.claim("a-thing", by="claude/some-branch")
```

From the CLI, `--source local` on `push`, `claim`, `release` and `status`, or
`ROADMAP_SOURCE=local` once. `ROADMAP_STORE` sets the path.

## Adopting it in another project

Measured end to end by `tests/test_adoption.py`, which runs the CLI as a
subprocess against a scratch project with nothing on the path but this package
— no backend, no FastAPI, no SQLAlchemy, no Postgres, no server, no token.

```bash
pip install "roadmap-core[files]"   # [files] adds PyYAML, which authoring needs
mkdir -p roadmap/items
export ROADMAP_SOURCE=local
```

That is the whole install. `roadmap` is a console script that comes with the
package — there is nothing to copy. (It used to say `curl -o scripts/roadmap.py
<this repo>/scripts/roadmap.py`, and this repo is private, so the package was
installable, importable and useless to anyone outside it.)

Then the ordinary loop, which needs nothing else:

```bash
cat > roadmap/items/first-thing.yaml <<'YAML'
id: first-thing
title: The first thing to do
status: ready
evidence: |
  Why this is worth doing, and how you will know it worked.
YAML

roadmap push            # files -> store
roadmap ready           # what is startable
roadmap claim first-thing
roadmap release first-thing
```

The store is one SQLite file at `roadmap/roadmap.db`. There is nothing to
provision and no migration to run: it is created on first open.

**Two things that are conventions rather than choices**, both found by doing
this rather than by reading the code:

- **Your project root is the nearest ancestor holding `roadmap/items` or
  `.git`**, so the commands work from anywhere inside it. Set
  `ROADMAP_REPO_ROOT` to pin it. Deliberately not bare `roadmap/`: that is a
  directory the tool *creates*, so keying on it let one command run in the
  wrong place mint the marker that made that place look like a project
  forever after.
- **Authoring is writing a YAML file**, not calling an API. `push` is what
  moves it into the store; there is no `roadmap new`. That is deliberate:
  filing an item belongs in a diff somebody reviews.

`ROADMAP_SOURCE=local` selects the SQLite store. Without it the CLI expects the
API store, which is how Lucille runs it — see `roadmap_core.stores`.

### CI

Copy `templates/roadmap.yml` to `.github/workflows/roadmap.yml`. That is the
whole CI story for the floor:

```
push      # files -> store, rebuilt fresh each run
validate  # schema, dangling dependencies, cycles
sync --check   # is the committed ROADMAP.md still what the graph renders?
```

No schedule, no credentials, no bot identity, no commit back to the default
branch, no self-hosted runner. `tests/test_adoption.py` reads the commands out
of that file and runs them, so a template that has drifted from the CLI fails
rather than reading as tested.

The third line is the one that earns the workflow. `ROADMAP.md` is generated but
committed — that is what lets an agent read the backlog with no install and no
network — and a generated file nobody regenerates is a file that lies.

**Do not commit `roadmap/roadmap.db`.** It is derived: `push` rebuilds it from
the YAML on first open, and a binary file in git conflicts on every claim. The
files are the record; the store is the transaction that decides who gets one.

### Upgrading to a served store

The floor's simplicity comes from one property: the store is inside the
checkout, so there is no second copy to drift from. Move the store to a server —
so that claims are visible across machines the moment they are taken, rather
than when a branch merges — and four things come back, none of which the
template can supply for you:

| What returns | Why |
|---|---|
| A **credential** step | the store is now behind auth, and the CLI needs a token per run |
| A **wait-for-reachable** step | a concurrent deploy can hold the store down, and being early is not being wrong |
| `pull` and a **bot commit** | the store now knows things no checkout does, and they have to land in the files an agent reads |
| A **schedule** | finishing an item is usually a code change somewhere else entirely, so no path filter can catch it — only re-asking on a clock can |

`ApiStore` is constructed with your own caller, so the auth stays yours (see the
table under [Storage](#storage)). Lucille's `.github/workflows/roadmap-sync.yml`
is the worked example of all four, and the reason it is not shipped as a
template: almost every line of it is a consequence of Lucille's own deployment,
and handing an adopter that machinery for a problem they do not have reads as
required rather than as one option.

## What is NOT here

HTTP, auth, and the CLI. The graph is pure functions over plain dicts keyed by
`key`, so the same code serves DB rows, API payloads and parsed YAML with no
adapter. Lucille's own persistence stays in `backend/app/crud/roadmap.py` over
SQLAlchemy; the two definitions of the same three tables are held together by
`backend/tests/test_roadmap_store_parity.py`, which asserts both the columns and
the row dicts the two readers produce.

The package's tests live in `tests/` here and import nothing outside the stdlib.
`.github/workflows/roadmap-core-tests.yml` runs them in a job that fails if
`app`, `fastapi`, `sqlalchemy` or `yaml` can be imported at all — the isolation
is asserted, not assumed.
