# ARCS.md — what's in flight, at arc level

<!-- GENERATED FILE — DO NOT EDIT BY HAND. Regenerate with `roadmap sync`. Edit roadmap/arcs/*.yaml instead. -->

The narrative layer above `roadmap/ROADMAP.md`: *why* each theme is still open. Work items live in the roadmap graph and are listed per arc below; the prose here is the arc's own, and is the one place a multi-PR theme gets explained rather than enumerated. For when this was last regenerated ask git — `git log -1 --format=%cI -- ARCS.md` — because nothing here derives from the clock or from a graph-wide total, so two branches editing different arcs merge cleanly.

## Legend

| State | Meaning |
|---|---|
| 🟠 open | Open tail — unfinished items, or a stated unresolved decision. |
| 🔵 blocked | Nothing startable — every unfinished item is blocked, or the blocker is outside the graph and was declared. |
| 🟡 dark | Code merged, flag off **in prod env**. Declared, never derived. |
| 🟢 closed | Tail is empty and somebody said so. Declared, never derived. |

`dark`, `closed` and `blocked` may be **declared** by a human with dated evidence, because each is about something the items cannot show: an environment flag, a closure whose finished items `prune` has deleted, or a blocker outside the graph entirely. `blocked` is *also* derived when every unfinished item is itself blocked. `open` is never declared — it is the fallback every check fires on, so stating it would only silence them.

## 🟠 Open

### 🟠 The residue of the repo this came from, on surfaces an adopter reads

`adoptable-by-anyone` · 4 item(s), 3 startable

`https://github.com/gald33/roadmap-core/pull/1` · `https://github.com/gald33/roadmap-core/pull/2`

The package's whole claim is in its own README: a project that is not Lucille
can adopt this without adopting Lucille with it. The library half of that is
true and asserted — stdlib only, and a CI job that fails if `app`, `fastapi`,
`sqlalchemy` or `yaml` can be imported at all.

The *strings* were never held to it. Commands, environment variables, error
remedies and namespace lists all still name one company's checkout, and they
surface exactly where a newcomer is already stuck: a stale generated file, a
claim they cannot release, a validation error they cannot act on.

These are not cosmetic. A dependency an adopter cannot resolve fails loudly at
install time; an instruction they cannot follow fails silently, at the moment
they most need it, and looks like their mistake.

The pattern for closing each one is set by PR #1: put the real value in a
constant, then add a test that fails if the old one comes back. Sweeping the
strings without the guard is how the count went from four to eighteen.

| item | status | priority |
|---|---|---|
| `cli-messages-name-a-script-that-does-not-exist` | ready | now |
| `nothing-tells-an-adopter-their-setup-is-broken` | claimed | now |
| `artifact-namespaces-are-one-projects` | ready | next |
| `credential-error-names-one-repos-secret` | ready | next |

### 🟠 What an adopter actually gets, versus what the docs describe

`the-floor-as-shipped` · 3 item(s), 2 startable

`README.md` · `templates/roadmap.yml`

The SQLite floor is meant to be the whole story for a project with no backend:
`pip install`, `mkdir roadmap/items`, write YAML, done. Measured end to end by
`tests/test_adoption.py`, which runs the CLI as a subprocess with nothing on the
path but this package.

What that test cannot see is everything the adopter *reads* — a file the
generated output cites but no command creates, a second generated file landing
somewhere its sibling does not, a rule number pointing into a document that was
never shipped. None of it breaks a command, which is exactly why a passing
adoption test coexists with all of it.

This arc is also where the package finally runs its own backlog. The two defects
PR #1 fixed lived in generated files this repository never generated, and were
found by the first outside adopter rather than by anyone here. Dogfooding is not
a virtue exercise; it is the only reader who reliably notices.

| item | status | priority |
|---|---|---|
| `roadmap-core-runs-its-own-roadmap` | done | — |
| `arcs-md-path-is-not-configurable` | ready | later |
| `generated-file-points-at-an-uncreated-readme` | ready | later |
