<!-- repoctx-pointer:v1 -->
<!-- This CLAUDE.md is a thin pointer managed by repoctx so Claude Code -->
<!-- transitively loads AGENTS.md. Edit AGENTS.md for project guidance. -->
@AGENTS.md


## Coordinating with other agents

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **If this work spans more than one repo**, put `--lobby` on every
  `switchboard` command (or `join_room` on the MCP side). Each repo has its own
  room, so an agent in another checkout is not on your roster even holding the
  same key and hub — the lobby is the room that key already shares, and it
  needs no workspace agreed between you. Compare `switchboard --lobby whoami`
  with a peer before trusting an empty roster: a different key derives a
  different lobby, and that looks exactly like a quiet one.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  On this CLI it is a line after `say` and `whisper` (and a field under
  `--json`), printed only when something is actually waiting.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **If the thing you are waiting for is a message**, arm the listener before
  the turn ends: run `switchboard listen --until forecast:p50` as a background
  process. It parks on your inbox and exits when something arrives, and a
  runner that re-invokes a session when a background process exits — Claude
  Code does — wakes you seconds after the message lands rather than at the
  next scheduled check. `--until` is when to give up and come back empty;
  without one it parks indefinitely, which is a promise to be reachable that
  nothing keeps. It peeks rather than drains, so still call `inbox` yourself
  when you wake, and it exits on the first message, so arm it again if you are
  still waiting. It takes the flags every command takes, so `-w` or `--invite`
  parks it in another room for cross-repo work.
- **Optionally, when a message precedes a stretch of heads-down work**, pass
  `execution_class` (a short label like "coding") and `effort`
  (`low`/`medium`/`high`) to `say`/`dm`/`checkin`/`inbox`. Your runtime turns
  that pair into an estimate of when you will next read messages and attaches
  it for collaborators — you never estimate seconds. Incoming messages may
  carry the same as `timing_forecast`: a prediction, not a promise, and best
  used to size how often you check rather than as exact times to check at.
- **If you are driving the `switchboard` CLI rather than the MCP tools**, the
  same primitives are there under slightly different spellings — `roster` is
  `switchboard agents`, `board_set` is `switchboard board set`, and the two
  timing fields above are `--execution-class` and `--effort` flags.
  `.claude/skills/switchboard-coordinate/SKILL.md` has the full mapping and
  the two things only the MCP surface offers.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
