# This package's own roadmap

`roadmap-core` tracking `roadmap-core`, on the SQLite floor it ships.

Not a virtue exercise. The two defects PR #1 fixed — a generated header naming a
script no adopter has, and an ARCS.md preamble linking three files that exist in
one repository — lived in output this project never generated, and were found by
the first outside adopter rather than by anyone here. Dogfooding is the only
reader that reliably notices.

## The rules

**1. Authoring an item is writing a YAML file.** There is no `roadmap new`. Add
`roadmap/items/<key>.yaml`, then `roadmap push`.

**2. Every item owes evidence.** `validate` fails without it. Not a restatement of
the title — *why it is worth doing, and how you will know it worked*. Reproduce it
and paste what you saw: this backlog's items carry line numbers, counted totals
and real command output, because a claim about an adopter's experience that
nobody reproduced is a guess.

**3. Say what is verified and what is inferred.** An item read off a grep and an
item reproduced in a scratch project deserve different amounts of trust before
somebody starts on one. Say which it is in the first line of evidence.

**4. `blocked_on` is for correctness, `related_to` is for everything else.**
`blocked_on` removes an item from the ready queue, so use it only when starting
the other one first is genuinely required. "Read this before touching that" is a
`related_to` with a note, and the note is not optional.

**5. The generated files are generated.** `roadmap/ROADMAP.md` and the root
`ARCS.md` are rebuilt wholesale by `sync`. Editing them by hand is lost work, and
CI fails on the drift.

## The commands

```bash
pip install -e ".[files]"     # the checkout, not the release — see below
export ROADMAP_SOURCE=local

roadmap ready                 # what is startable, in priority order
roadmap show <key>            # one item, with its evidence and edges
roadmap push                  # files -> store
roadmap validate              # schema, dangling deps, cycles, missing evidence
roadmap sync                  # regenerate the two markdown files
roadmap claim <key>           # take it; roadmap release <key> to drop it
```

## Why the CI job differs from the template

`templates/roadmap.yml` tells an adopter to `pip install "roadmap-core[files]"`,
which resolves the last release. `.github/workflows/roadmap.yml` here installs the
**checkout** instead:

```yaml
- run: pip install ".[files]"
```

That difference is the entire point of self-hosting. Installing the release would
mean a pull request that breaks rendering gets validated by the last build that
did not — the exact class of bug this exists to catch. Keep it that way, and do
not "fix" it back to match the template.

## Two things that will look wrong

**The root `ARCS.md` is generated too**, and does not live under `roadmap/` with
its sibling. That path is hard-coded (`cli.py`'s `GENERATED_ARCS_MD`) and
`sync --check` compares against it, so moving the file turns CI red. Whether that
placement is the contract or an accident is itself an item on this board —
`arcs-md-path-is-not-configurable`.

**`roadmap/roadmap.db` is not committed.** It is derived — `push` rebuilds it from
the YAML on first open — and a binary file in git would conflict on every claim.
Covered by `roadmap/roadmap.db` in `.gitignore`.
