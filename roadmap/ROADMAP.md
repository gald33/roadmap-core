# ROADMAP.md — open work items

<!-- GENERATED FILE — DO NOT EDIT BY HAND. Regenerate with `roadmap sync`. -->

This is the agent-readable projection of the roadmap graph; the store is the `roadmap_items` table (see `roadmap/README.md`). For when it was last regenerated ask git — `git log -1 --format=%cI -- roadmap/ROADMAP.md` — because nothing in this file is derived from the clock or from a graph-wide total, so that two branches editing different items merge cleanly. Do not add one back.

`ARCS.md` is the narrative layer — *why* an arc is open. This file is the work-item layer — *what* is claimable right now, and who holds it.

## ▶ Ready — startable now

Claim before starting: `roadmap claim <key>`

**In priority order, most important first.** An item with no marker carries no stated priority — take it as unjudged, not as low. The order within a band is alphabetical and means nothing.

- `now` **`a-claim-cannot-survive-the-floors-ci`** — On the SQLite floor, claiming an item turns CI red — `push` drops the claim it is asked to restore
- `now` **`cli-messages-name-a-script-that-does-not-exist`** — Finish the job on the CLI's own messages — fourteen still name roadmap.py
  - ↔ related: **`credential-error-names-one-repos-secret`** — Same sweep, same file, different string class — land them together or the second PR re-reads the same 2000 lines. That one is about a command name; this one is about environment variables and an error message, so neither grep finds the other.
- `next` **`artifact-namespaces-are-one-projects`** — Let a project declare its own artifact namespaces instead of inheriting seven
- `next` **`credential-error-names-one-repos-secret`** — Stop the db source asking an adopter for a credential only one company mints
  - ↔ related: **`cli-messages-name-a-script-that-does-not-exist`** — Same sweep, same file, different string class — land them together or the second PR re-reads the same 2000 lines. That one is about a command name; this one is about environment variables and an error message, so neither grep finds the other.
- `later` **`arcs-md-path-is-not-configurable`** — Decide whether ARCS.md at the repository root is the contract or an accident
- `later` **`generated-file-points-at-an-uncreated-readme`** — Create the roadmap/README.md the generated files and the validator both cite

## ⏸ Deferred — startable, deliberately not now

_Nothing deferred._

## 🔒 Claimed — someone is on these

_Nothing claimed._

## ⛔ Blocked

_Nothing blocked._

## Dependency graph

```mermaid
graph TD
  a_claim_cannot_survive_the_floors_ci["On the SQLite floor, claiming an item turns CI red — `push` drops the claim it is asked to restore"]
  arcs_md_path_is_not_configurable["Decide whether ARCS.md at the repository root is the contract or an accident"]
  artifact_namespaces_are_one_projects["Let a project declare its own artifact namespaces instead of inheriting seven"]
  cli_messages_name_a_script_that_does_not_exist["Finish the job on the CLI's own messages — fourteen still name roadmap.py"]
  credential_error_names_one_repos_secret["Stop the db source asking an adopter for a credential only one company mints"]
  generated_file_points_at_an_uncreated_readme["Create the roadmap/README.md the generated files and the validator both cite"]
  nothing_tells_an_adopter_their_setup_is_broken["Nothing answers 'is this project's roadmap setup working?' — add `doctor` and `--version`"]
  roadmap_core_runs_its_own_roadmap["Run this package's own backlog on this package"]
  cli_messages_name_a_script_that_does_not_exist -.- credential_error_names_one_repos_secret
```

## Items

### `a-claim-cannot-survive-the-floors-ci`

- **title:** On the SQLite floor, claiming an item turns CI red — `push` drops the claim it is asked to restore
- **status:** ready
- **arc:** adoptable-by-anyone
- **priority:** now
- **refs:**
  - `roadmap_core/cli.py`
  - `roadmap_core/stores.py`
  - `.github/workflows/roadmap.yml`
  - `templates/roadmap.yml`

<details><summary>evidence</summary>

> FOUND 2026-08-22 BY FOLLOWING THIS REPOSITORY'S OWN PROTOCOL. `roadmap claim`
> an item, commit the projection it tells you to commit, open a PR — and the
> `roadmap` check fails with `ARCS.md is stale — run 'roadmap sync'`. Running
> `sync` does not fix it. Nothing the operator did was wrong.
>
> THE MECHANISM. On the floor the store is EPHEMERAL: CI is
> `push` → `validate` → `sync --check` against a SQLite file that does not exist
> until `push` creates it. So every rendered artifact in CI comes from a store
> built moments earlier out of `roadmap/items/*.yaml`.
>
> `push` honours a file's `status` when it CREATES an item and ignores it on
> update — the mitigation against a stale checkout resurrecting finished work.
> It does not carry `claim` at all, on either path. Reproduced against a fresh
> store:
>
>     file:   claim: {by: claude/adopter-doctor-and-version, at: ...}
>     store:  claimed_by=None  status=ready
>
>     $ roadmap sync --check
>     ARCS.md is stale — run `roadmap sync`
>
>     < `adoptable-by-anyone` · 4 item(s), 3 startable
>     > `adoptable-by-anyone` · 4 item(s), 4 startable
>     < | nothing-tells-an-adopter-...  | claimed | now |
>     > | nothing-tells-an-adopter-...  | ready   | now |
>
> So the committed markdown says `claimed` and a fresh push renders `ready`, and
> the two can never agree while a claim exists. The floor's CI is red for exactly
> as long as anybody is holding an item — which is to say, whenever the
> coordination feature this tool exists for is in use.
>
> WHY IT WAS INVISIBLE. Both existing adopters hid it from opposite sides.
> Lucille's store is SERVED and persistent, so a claim lives in the store, `pull`
> projects it into the file, and the file is a projection rather than a source —
> the round trip never has to work. Switchboard is on the floor but had no live
> claim when its ROADMAP.md was last generated. The failure needs the floor AND
> somebody actually claiming something, which is the first thing a second agent
> on a floor project would ever do.
>
> NOT A ONE-LINE FIX, which is why this is an item and not a follow-up commit.
> "Honour `claim` on create, like `status`" is the obvious move and it is
> dangerous on the served path: a push from a checkout predating a release would
> recreate a claim the store had already dropped, which is the resurrection class
> this project has been bitten by before and now guards with tombstones. The
> question to answer first is whether claim-in-the-file is authoritative on the
> floor and merely a projection when served — two different meanings for one
> field, decided by store type.
>
> ACCEPTANCE:
>
>   1. A floor project can claim an item, commit what the CLI tells it to commit,
>      and have CI pass. Verified by doing exactly that in a scratch project in
>      `tests/test_adoption.py`, including the `sync --check` step — the loop
>      test currently claims and releases but never renders in between.
>   2. The served path still cannot resurrect a released claim from a stale file.
>      Asserted, not assumed.
>   3. Whichever way it is decided, `roadmap/README.md` states which side owns
>      `claim` and under which store, because the answer is not guessable from
>      the field's name.
>
> WORKAROUND UNTIL THEN: release before you commit the markdown, which is what
> PR #5 did — and which means the floor's claim protocol currently cannot be
> followed and recorded at the same time.

</details>

### `arcs-md-path-is-not-configurable`

- **title:** Decide whether ARCS.md at the repository root is the contract or an accident
- **status:** ready
- **arc:** the-floor-as-shipped
- **priority:** later
- **refs:**
  - `roadmap_core/cli.py`
  - `https://github.com/gald33/switchboard/pull/154`

<details><summary>evidence</summary>

> The two generated files land in different places:
>
>     GENERATED_MD      = REPO_ROOT / "roadmap" / "ROADMAP.md"     (cli.py:109)
>     GENERATED_ARCS_MD = REPO_ROOT / "ARCS.md"                    (cli.py:113)
>
> One is namespaced under `roadmap/`, its sibling is dropped in the repository
> root. Nothing in the code says why, and the asymmetry is invisible until you
> adopt the package and a file you did not ask for appears next to your README.
>
> Measured cost, not hypothetical: adopting this in `switchboard` needed a
> paragraph in that repo's `roadmap/README.md` warning contributors NOT to tidy
> `ARCS.md` into `roadmap/`, because `sync --check` compares against the hard-coded
> path and moving it turns CI red on a file it cannot find. Every adopter will
> either write that paragraph or discover it from a red build.
>
> Root placement may well be deliberate — an arcs narrative is for humans browsing
> the repo, and burying it one directory down does reduce the chance anyone reads
> it. That is a defensible answer. It is just not written down anywhere, and the
> next person to look at these two constants will read it as an oversight and
> "fix" it.
>
> So the work is to decide and then make the decision enforce itself: either move
> it under `roadmap/` with the other generated file, or keep it and say why in a
> comment next to the constant. `later` because nothing is broken — a documented
> wart is survivable, an undocumented one is what this is.

</details>

### `artifact-namespaces-are-one-projects`

- **title:** Let a project declare its own artifact namespaces instead of inheriting seven
- **status:** ready
- **arc:** adoptable-by-anyone
- **priority:** next
- **refs:**
  - `roadmap_core/graph.py`

<details><summary>evidence</summary>

> `ARTIFACT_PREFIXES` is a fixed tuple of seven namespaces, and every one belongs
> to the project this package came from:
>
>     ('alembic', 'module', 'roadmap', 'web', 'docs', 'config', 'script')
>
> `validate_graph` reports any token outside it. Reproduced in a scratch project,
> using the exact resource-name shape switchboard's own `docs/seam.md` recommends
> for compiling a static collision into a name:
>
>     $ roadmap validate
>     error: a-thing: unknown artifact namespace 'artifact' in
>     'artifact:migrations-counter' — one of alembic, module, roadmap, web,
>     docs, config, script, or add it deliberately
>
> So the documented cross-tool pattern fails validation out of the box. "Or add it
> deliberately" is the right instinct and points at the bug: it invites an adopter
> to edit a constant inside an installed package, which they cannot do without
> vendoring it.
>
> The field is warn-only by design, and that is what keeps this at `next` rather
> than `now` — nothing is blocked, the edge still surfaces the contention. But a
> warning every adopter learns to ignore is the failure mode the field's own
> comment names: "a typo'd token silently matches nothing, which is the one
> failure mode of a warn-only field — it looks declared and warns about nothing."
> A namespace list nobody can extend produces exactly that habit.
>
> Cheapest shape that keeps the typo guard: read the allowed set from the
> project — a key in `roadmap/README.md`'s frontmatter, or a `roadmap/config.yaml`
> — and fall back to the current tuple when absent, so nothing changes for the
> repo that has one today.

</details>

### `cli-messages-name-a-script-that-does-not-exist`

- **title:** Finish the job on the CLI's own messages — fourteen still name roadmap.py
- **status:** ready
- **arc:** adoptable-by-anyone
- **priority:** now
- **related to** (not a dependency — both are startable):
  - `credential-error-names-one-repos-secret` — Same sweep, same file, different string class — land them together or the second PR re-reads the same 2000 lines. That one is about a command name; this one is about environment variables and an error message, so neither grep finds the other.
- **refs:**
  - `https://github.com/gald33/roadmap-core/pull/1`
  - `https://github.com/gald33/roadmap-core/pull/2`
  - `roadmap_core/cli.py`

<details><summary>evidence</summary>

> PR #1 fixed the strings the *generated markdown* emits and introduced
> `graph.CLI` for them. PR #2 (merged as 2015b48, released 0.2.1) fixed four more
> in the CLI itself. Neither finished the sweep.
>
> Recounted on `main` at 2015b48, after #2 landed: zero strings say `python
> scripts/roadmap.py`, and **fourteen** still say bare `roadmap.py`. Those are the
> ones no grep for the old form will ever surface, which is why the sweep looked
> finished:
>
>     365   re-run `roadmap.py pull` afterwards and check the file
>     814   seed it with `roadmap.py push` first
>     851   right, run `roadmap.py status <key> done`
>     856   If this file predates a `prune`, run `roadmap.py pull`
>     952   refiled, run `roadmap.py refile <key>`
>     998   Run `roadmap.py pull` first
>     1002  `roadmap.py prune` when you are ready to clear it
>     1043  Fix: `roadmap.py pull`
>     1048  Fix: `roadmap.py push`
>     1263  `roadmap.py diff` shows the gap
>     1291  then `roadmap.py release <key>`
>     1315  Release what you are done with — `roadmap.py release <key>`
>     1753  Run `roadmap.py pull` first
>     1813  when you finish or stop: `roadmap.py release <key>`
>
> Observed rather than only grepped: running `roadmap ready` in a scratch project
> with one item prints
>
>     note: claims here are the committed projection and can lag the store.
>     `--source db` is authoritative; `roadmap.py diff` shows the gap.
>
> There is no `roadmap.py` on an adopter's PATH. The console script is `roadmap`.
> Every one of these is advice a reader cannot act on, and they appear at exactly
> the moments someone is stuck — a stale file, a lost claim, a drifted store.
>
> Do it as a sweep with a guard, not string by string. A test asserting no
> user-facing string in `cli.py` matches `roadmap\.py` is what stops the
> nineteenth from being added next month; that is the shape PR #1 used for the
> markdown, and it is the only reason this count is knowable at all.

</details>

### `credential-error-names-one-repos-secret`

- **title:** Stop the db source asking an adopter for a credential only one company mints
- **status:** ready
- **arc:** adoptable-by-anyone
- **priority:** next
- **related to** (not a dependency — both are startable):
  - `cli-messages-name-a-script-that-does-not-exist` — Same sweep, same file, different string class — land them together or the second PR re-reads the same 2000 lines. That one is about a command name; this one is about environment variables and an error message, so neither grep finds the other.
- **refs:**
  - `roadmap_core/cli.py`

<details><summary>evidence</summary>

> Three adopter-facing surfaces name the project this package was extracted from:
>
> * **`--help`.** The module docstring, which argparse prints, says the db source
>   is "the live graph via `GET /admin/roadmap` (needs LUCILLE_ADMIN_JWT)". That
>   is the first thing an adopter reads.
> * **The credential error** (`_api`, cli.py:702-708):
>
>       LUCILLE_ADMIN_JWT is not set — needed for the db source.
>       Mint one with the `mint-admin-jwt` skill, or use --source files.
>
>   The remedy it offers is a skill that exists in one private repository.
> * **The defaults**: `DEFAULT_BACKEND` reads `LUCILLE_BACKEND_URL` (cli.py:137)
>   and the write lock's TTL reads `LUCILLE_ROADMAP_LEASE_TTL` (cli.py:153).
>
> Worth being precise about the blast radius, because it is smaller than it looks
> and that changes the priority: read commands default to `--source files`, so a
> bare `roadmap ready` in a fresh project works and never touches any of this.
> Verified in a scratch checkout. It is the *write* commands that default to `db`
> (cli.py:1932), so `roadmap push` with no `ROADMAP_SOURCE` is where an adopter
> meets it.
>
> The fix is not to delete the concept — a served store is a real mode. It is to
> name the variables after this package and let a host map its own: something like
> `ROADMAP_API_TOKEN` / `ROADMAP_API_URL`, with the Lucille names accepted as
> fallbacks so its shim keeps working.

</details>

### `generated-file-points-at-an-uncreated-readme`

- **title:** Create the roadmap/README.md the generated files and the validator both cite
- **status:** ready
- **arc:** the-floor-as-shipped
- **priority:** later
- **refs:**
  - `roadmap_core/graph.py`
  - `README.md`

<details><summary>evidence</summary>

> Two places send a reader to `roadmap/README.md`, and nothing ever writes one.
>
> * Every generated `ROADMAP.md` opens with "the store is the `roadmap_items`
>   table (see `roadmap/README.md`)".
> * `validate_graph`'s missing-evidence error is literally
>   "no evidence — see roadmap/README.md rule 2".
>
> There is no `roadmap new`, no `roadmap init`, and the adoption instructions in
> this package's README are `mkdir -p roadmap/items` — so an adopter who follows
> them exactly ends up with two artifacts citing a file they were never told to
> write. The second one is worse than a dead link: it cites a *rule number* in a
> document that does not exist, so the error explains itself by reference to
> nothing.
>
> Two honest fixes, and they are not the same amount of work:
>
> 1. **Make the messages self-contained.** Have the evidence error say what
>    evidence is for rather than delegating to a rule number, and drop the
>    parenthetical from the header. Small, and removes the dangling reference
>    without adding a feature.
> 2. **Ship the file.** A `roadmap init` that writes a starter `roadmap/README.md`
>    alongside `roadmap/items/`. Bigger, and it cuts against "authoring an item is
>    writing a YAML file, there is no `roadmap new`" — though a one-time scaffold
>    is a different thing from a per-item command.
>
> `later` because a reader who does not find the file loses an explanation, not a
> command — everything still works. Doing (1) alone would already be worth it, and
> is the version to start from.

</details>

### `nothing-tells-an-adopter-their-setup-is-broken`

- **title:** Nothing answers "is this project's roadmap setup working?" — add `doctor` and `--version`
- **status:** done
- **arc:** adoptable-by-anyone
- **priority:** now
- **refs:**
  - `roadmap_core/cli.py`
  - `tests/test_adoption.py`

<details><summary>evidence</summary>

> MEASURED 2026-08-22 by auditing the two repositories that consume this package
> and the one that ships it.
>
> THE ARC SAYS the failures that matter are the ones that "fail silently, at the
> moment they most need it, and look like their mistake". This is that failure
> with no string in it to fix: there is no command an adopter can run to find out
> whether their setup works, and no way for anyone to find out which version they
> are running. Verified absent at e715075 — `grep` for `--version`, `doctor` and
> `__version__` across `roadmap_core/` returns nothing.
>
> WHAT IT COST THE ADOPTER I CAN MEASURE. Lucille vendored this package until it
> became a dependency. The tool crossed intact — a function-level diff of its old
> in-repo CLI against `roadmap_core/cli.py` shows one function dropped and all 14
> subcommands present, and Lucille's 196 roadmap tests pass against 0.2.1. What
> broke was everything the vendoring had been guaranteeing for free:
>
>   * NOTHING INSTALLED IT. In a clean worktree of Lucille's `main`,
>     `python3 scripts/roadmap.py ready` exits 1 with "roadmap-core is not
>     installed". That is the first command its own contributor guide tells every
>     agent session to run, and claiming work is a convention rather than a gate
>     — so this was never an error anybody saw. It was sessions quietly not
>     claiming, which is the collision the tool exists to prevent.
>   * TWO INSTALLS DRIFTED, UNDETECTED. Its backend declared `roadmap-core>=0.1.0`
>     with a lock at **0.1.0** while the published package was **0.2.1**. The
>     backend's copy and the copy a developer's CLI runs are separate installs;
>     nothing on either side can report its own version, so nothing could have
>     noticed. Its schema-parity test was passing against a package its CLI never
>     used.
>
> A `doctor` would have said both in one line. Neither is exotic: "is the package
> importable, does the repo root resolve where you think, is `roadmap/items/`
> where the CLI is looking, is the store reachable, which version is this".
>
> REPRODUCED WHILE FILING THIS ITEM, which is the strongest evidence in it. Run
> from this repository's own root, against a store that had never been seeded:
>
>     $ ROADMAP_SOURCE=local roadmap validate
>     ok — 0 item(s), 0 arc(s), no problems
>     $ ROADMAP_SOURCE=local roadmap ready
>     nothing ready — everything is claimed, blocked, or done
>
> Seven items were sitting in `roadmap/items/` at the time. `local` reads the
> SQLite store, which is empty until `push` seeds it, so the correct reading of
> "0 items, no problems" is *"I am pointed at an empty store"* — and what it says
> instead is `ok`. `ready` then reports the backlog is finished. Both are green,
> confident and wrong, and the fix is one command (`push`) that nothing suggests
> because nothing has noticed anything is amiss.
>
> That is not a hypothetical adopter. It is this package, in its own checkout,
> three commands into being used.
>
> THE TWO FAILURES THIS PACKAGE ALREADY KNOWS ABOUT are the same shape.
> `tests/test_adoption.py`'s docstring records them: a CLI that could not find its
> graph because the path only existed in one company's layout, and a shim at the
> wrong depth making `push` report *"no item files to push"* — which reads as an
> empty backlog rather than as a misconfiguration. Both are one line of `doctor`
> output. That test exists because they were found the expensive way, twice.
>
> SCOPE — NOT THE SHIM. An earlier draft of this proposed shipping a
> `scripts/roadmap.py` template on the theory that every adopter writes one.
> Checked against the other adopter and it is false: switchboard runs 10 items
> with **no shim at all** — `ROADMAP_SOURCE=local` plus the `roadmap` console
> script. A shim is a *served-store* concern (pinning a repo root and a backend
> URL), not a general one, and templating it would hand the floor's adopters
> machinery for a problem they do not have — the same reasoning
> `templates/roadmap.yml` already states about Lucille's 333-line sync workflow.
>
> ACCEPTANCE — the point is that a broken setup announces itself, not that the
> README explains how to check:
>
>   1. `roadmap doctor` exits non-zero on a broken setup and names the specific
>      thing that is wrong, one line each. VERIFIED AGAINST DELIBERATELY BROKEN
>      PROJECTS — a missing `roadmap/items/`, a repo root resolved somewhere
>      unexpected, an unreachable served store — not by running it on a healthy
>      one and seeing it say OK. A doctor that passes everything is the guard
>      that always fires, inverted.
>   2. `roadmap --version` reports the installed distribution version.
>   3. `doctor` reports the version too, because the question "which one is this"
>      is asked while diagnosing, not before.
>   4. The adoption test covers `doctor` in its scratch project, so the command
>      that certifies an adoption is itself certified by the adoption test.

</details>

### `roadmap-core-runs-its-own-roadmap`

- **title:** Run this package's own backlog on this package
- **status:** done
- **arc:** the-floor-as-shipped
- **refs:**
  - `https://github.com/gald33/switchboard/pull/154`
  - `.github/workflows/roadmap.yml`

<details><summary>evidence</summary>

> A tool for tracking work that did not track its own. Every claim this package
> makes about being adoptable was tested somewhere else — `tests/test_adoption.py`
> against a scratch directory, and then for real in `switchboard` — while the
> repository that ships it had no `roadmap/` at all.
>
> That gap is how the two defects fixed in PR #1 survived: the generated header
> and the ARCS.md preamble named paths from the project this was extracted from,
> and nobody read them here because nothing here generated them. They were found
> by an outside adopter on first contact.
>
> Now the floor runs on itself: `roadmap/items/*.yaml`, `roadmap/arcs/*.yaml`, the
> committed `roadmap/ROADMAP.md` and `ARCS.md`, and a `roadmap` CI job.
>
> **One deliberate difference from `templates/roadmap.yml`, and it is the point of
> doing this at all.** The template tells an adopter `pip install
> "roadmap-core[files]"`, which resolves the last *release*. This repository's job
> installs the checkout (`pip install ".[files]"`) instead, so the roadmap check
> exercises the code in the pull request rather than the version that shipped
> before it. Installing the release here would mean a PR that breaks rendering is
> validated by the last build that did not — which is precisely the class of bug
> this item exists to catch.

</details>
