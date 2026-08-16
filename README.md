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

## What is NOT here

Storage, HTTP, auth, and the CLI. The graph is pure functions over plain dicts
keyed by `key`, so the same code serves DB rows, API payloads and parsed YAML
with no adapter. Persistence lives in the host (`backend/app/crud/roadmap.py`
for Lucille); the tables it maps to are `roadmap_items` and `roadmap_arcs`, both
deliberately portable to SQLite.

Its tests currently live in `backend/tests/test_roadmap*.py` and import
`roadmap_core`. Moving them here is the next increment, not done yet — they run
green where they are, and relocating them in the same change as the Docker build
rewiring would have made this diff unreviewable.
