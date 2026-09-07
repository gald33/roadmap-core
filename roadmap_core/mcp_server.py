"""The roadmap as MCP tools, over stdio, with nothing installed.

**Why this is not built on the MCP SDK.** The package's whole claim is that it
stands alone — ``tests.yml``'s ``isolation`` job installs ``[dev]`` and then
*fails* if ``app``, ``fastapi``, ``sqlalchemy`` or ``yaml`` is importable at
all, because the graph and the store have to work in a checkout with nothing
provisioned. A hard dependency on ``mcp`` would end that, and putting it behind
an extra would only move the question to whichever environment forgot the
extra. The protocol is newline-delimited JSON-RPC over two pipes; it is written
out below in stdlib. This is the same call ``switchboard/mcp_server.py`` made,
and for the same reason it gives: an SDK that renames its API between majors is
a second thing that can break a server whose own logic did not change.

**STDOUT IS THE PROTOCOL.** Every byte on stdout has to be a JSON-RPC frame, and
the CLI this server delegates its writes to is a *printing* program — ``claim``
alone emits four lines, one of them the "when you finish" notice. Those prints
are not incidental output to be silenced: they are what the CLI tells an
operator, so they are captured and returned as the tool's own text. That is what
``_capture`` is for, and it is load-bearing rather than tidy. A single
uncaptured ``print`` corrupts the stream for the rest of the session, and it
fails as a client-side parse error a long way from the line that caused it.

**Writes go through the CLI; reads do not.** The two halves are asymmetric on
purpose:

  writes  ``cmd_claim`` / ``cmd_release`` / ``cmd_status`` do more than talk to
          the store. They project the claim into ``roadmap/items/<key>.yaml``,
          take the Switchboard write lock around that read-modify-write, drop
          the claim when a status reaches ``done``, and warn about artifact
          contention. That sequence is the repository's answer to
          "a-claim-cannot-survive-the-floors-ci"; a second copy of it here
          would be a second answer, free to drift from the first.

  reads   ``list`` / ``ready`` / ``show`` / ``validate`` are ``load()`` plus
          ``graph``, which are pure. The CLI's versions of these print aligned
          columns for a human; an agent wants the fields. So the reads are
          composed here from the same functions the printers use, rather than
          scraped back out of their output.

**Source resolution matches the CLI exactly**, because an agent and a terminal
in the same checkout disagreeing about which store they are looking at is the
kind of difference nobody thinks to check. Reads default to ``files`` — no
server, no JWT, no network, which is what makes a fresh clone answerable —
and ``ROADMAP_SOURCE`` overrides that for both halves, as it does for the CLI.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import traceback
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Callable

from . import graph
from .cli import installed_version, load, load_arcs

__all__ = ["TOOLS", "UnknownTool", "dispatch", "handle_request", "serve_stdio", "main"]

# Negotiated in `initialize`. A client asking for something in this set gets it
# back verbatim; anything else is answered with the newest we speak, which is
# what the spec asks a server to do rather than failing the handshake.
LATEST_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = (LATEST_PROTOCOL, "2025-03-26", "2024-11-05")

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "The roadmap is this repository's backlog as a dependency graph. Call `ready` "
    "before starting work — it lists only what is startable right now, in the order "
    "the queue hands work out, and flags items that share a mutable artifact with a "
    "live claim. Call `claim` before you begin so another session does not take the "
    "same item, and `release` when you finish or stop; an unreleased claim keeps the "
    "item off everyone's queue. Use `set_status` to move an item deliberately — "
    "`verifying` keeps the claim (the branch that shipped it still owns confirming "
    "it), `done` drops it.\n\n"
    "Reads default to the committed `roadmap/items/*.yaml` files, so they work with "
    "no server, no database and no token. Set ROADMAP_SOURCE=local to read and write "
    "a SQLite store instead, or ROADMAP_SOURCE=db for a served backend. Writes "
    "project into the item files and tell you to commit them — that projection is "
    "how a claim survives, so do commit it."
)


def log(message: str) -> None:
    """Stderr, always. See this module's docstring on why never stdout."""
    print(f"roadmap-mcp: {message}", file=sys.stderr, flush=True)


# --- source resolution --------------------------------------------------------
#
# Deliberately not imported from `cli`: the names there (`_ENV_SOURCE`) are
# private and read at import time, and re-reading the environment here is what
# lets a test set it. The DEFAULTS are what must agree, and they are stated in
# terms of the same variable the CLI's `--source` default is built from.

READ_SOURCES = ("files", "local", "db")
WRITE_SOURCES = ("local", "db")


def _read_source(requested: str | None = None) -> str:
    """Where a read looks, defaulting exactly as `roadmap --source` does.

    `files` last in the chain and first in practice: it is the only source that
    needs nothing provisioned, which is the property that lets an agent in a
    fresh clone answer "what should I work on" at all.
    """
    source = requested or os.environ.get("ROADMAP_SOURCE") or "files"
    if source not in READ_SOURCES:
        raise ValueError(f"unknown source {source!r} — one of {', '.join(READ_SOURCES)}")
    return source


def _write_source(requested: str | None = None) -> str:
    """Where a write goes.

    `files` is not offered, and its absence is the point rather than an
    oversight: the item files are a PROJECTION of a write the store arbitrated,
    so "write to files" would be a claim nothing adjudicated — two sessions
    could each hold the same item and the transaction that is supposed to decide
    the race would never run. An environment that says `files` therefore falls
    through to `db`, which is the CLI's own default for the write commands.
    """
    source = requested or os.environ.get("ROADMAP_SOURCE") or "db"
    if source == "files":
        source = "db"
    if source not in WRITE_SOURCES:
        raise ValueError(f"unknown write source {source!r} — one of {', '.join(WRITE_SOURCES)}")
    return source


# --- schemas ------------------------------------------------------------------


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_SOURCE_PROP = {
    "type": "string",
    "enum": list(READ_SOURCES),
    "description": (
        "where to read the graph from. Default: ROADMAP_SOURCE, else 'files' — "
        "the committed YAML, which needs no server and no token."
    ),
}
_WRITE_SOURCE_PROP = {
    "type": "string",
    "enum": list(WRITE_SOURCES),
    "description": (
        "which store arbitrates the write. Default: ROADMAP_SOURCE, else 'db'. "
        "There is no 'files' — a write nothing arbitrated is not a claim."
    ),
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "ready",
        "description": (
            "Items startable right now — nothing blocked, deferred, claimed or done — "
            "in priority-then-key order, the same order ROADMAP.md and the API use. "
            "Each carries its related items and any artifact contention with a live "
            "claim. Call this before starting work."
        ),
        "inputSchema": _schema({"source": _SOURCE_PROP}),
    },
    {
        "name": "list",
        "description": (
            "Every item grouped by its DERIVED status (ready, deferred, blocked, "
            "claimed, verifying, done) — derived, so an item with a live claim reads "
            "'claimed' whatever its stored status column says. Optionally filtered to "
            "one status."
        ),
        "inputSchema": _schema({
            "source": _SOURCE_PROP,
            "status": {
                "type": "string",
                "enum": list(graph.STATUSES),
                "description": "only this status. Omit for all of them.",
            },
        }),
    },
    {
        "name": "show",
        "description": (
            "One item in full: derived status, arc, priority, who holds it and for how "
            "long, what it is blocked on and what it blocks, related items, artifacts "
            "and contention, refs, and the evidence prose."
        ),
        "inputSchema": _schema(
            {"key": {"type": "string", "description": "the item key"}, "source": _SOURCE_PROP},
            ["key"],
        ),
    },
    {
        "name": "validate",
        "description": (
            "Schema problems, dangling dependencies, cycles, and arc coherence. "
            "'problems' are errors; 'findings' are observations about a thin backlog "
            "and never make a graph invalid."
        ),
        "inputSchema": _schema({"source": _SOURCE_PROP}),
    },
    {
        "name": "claim",
        "description": (
            "Stake an item so no other session starts it. The store arbitrates, so "
            "losing the race is an ordinary refusal and not an error to retry around. "
            "On success the claim is projected into roadmap/items/<key>.yaml — commit "
            "that, or the claim is invisible to anyone in a checkout."
        ),
        "inputSchema": _schema({
            "key": {"type": "string", "description": "the item key"},
            "by": {
                "type": "string",
                "description": "who is claiming. Default: the current git branch.",
            },
            "force": {
                "type": "boolean",
                "description": "take an item someone else holds. Default false.",
            },
            "source": _WRITE_SOURCE_PROP,
        }, ["key"]),
    },
    {
        "name": "release",
        "description": (
            "Drop a claim, and project the drop into the item file. Call this when you "
            "finish or when you stop — an unreleased claim keeps the item off "
            "everyone's queue until it goes stale."
        ),
        "inputSchema": _schema(
            {"key": {"type": "string", "description": "the item key"},
             "source": _WRITE_SOURCE_PROP},
            ["key"],
        ),
    },
    {
        "name": "set_status",
        "description": (
            "Move one item's status deliberately — the only writer for that field. "
            "'verifying' keeps the claim, because the branch that shipped the work "
            "still owns observing that it landed; 'done' drops the claim. The new "
            "status is projected into the item file for you to commit."
        ),
        "inputSchema": _schema({
            "key": {"type": "string", "description": "the item key"},
            "status": {"type": "string", "enum": list(graph.STATUSES)},
            "source": _WRITE_SOURCE_PROP,
        }, ["key", "status"]),
    },
]


# --- reads --------------------------------------------------------------------


def _summary(item: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One item as a queue line: what decides whether to start it, and nothing else."""
    view: dict[str, Any] = {
        "key": item["key"],
        "title": item["title"],
        "status": graph.derive_status(item, by_key),
        "priority": graph.priority_of(item),
        "arc": item.get("arc"),
    }
    if item.get("claimed_by"):
        view["claimed_by"] = item["claimed_by"]
        # Formatted, not raw: PyYAML resolves a timestamp to a datetime while the
        # API returns a string, so the two sources emit different JSON types for
        # one field unless this is normalised — the same drift `cmd_list` fixes.
        view["claimed_at"] = graph.fmt_timestamp(item.get("claimed_at"))
        age = graph.claim_age_days(item)
        if age is not None:
            view["claim_age_days"] = round(age, 1)
            view["claim_stale"] = age >= graph.STALE_CLAIM_DAYS
    if unmet := graph.unmet_deps(item, by_key):
        view["unmet_deps"] = unmet
    if related := graph.relations_for(item["key"], by_key):
        view["related"] = related
    # A merge cost, not a blocker — said against the item so it is read by
    # whoever is looking at that line, exactly as `cmd_ready` prints it.
    if contention := graph.artifact_contention(item["key"], by_key):
        view["artifact_contention"] = contention
    return view


def tool_ready(source: str | None = None) -> dict[str, Any]:
    by_key = load(_read_source(source))
    items = [_summary(item, by_key) for item in graph.ready_items(by_key)]
    result: dict[str, Any] = {"source": _read_source(source), "ready": items}
    if not items:
        result["note"] = "nothing ready — everything is claimed, blocked, deferred, or done"
    # The stale-claim nudge the CLI prints. A session asking what to start is the
    # one most able to notice a hold that should have been dropped.
    stale = graph.stale_claims(by_key)
    if stale:
        # `claim_age_days` comes from `stale_claims` rather than being
        # recomputed here — it copies the item with the age already on it
        # precisely so every surface reports one number from one clock.
        result["stale_claims"] = [
            {"key": i["key"], "claimed_by": i.get("claimed_by"),
             "age_days": i["claim_age_days"]}
            for i in stale
        ]
    return result


def tool_list(source: str | None = None, status: str | None = None) -> dict[str, Any]:
    by_key = load(_read_source(source))
    wanted = (status,) if status else graph.STATUSES
    grouped: dict[str, list[dict[str, Any]]] = {}
    for name in wanted:
        group = sorted(
            (i for i in by_key.values() if graph.derive_status(i, by_key) == name),
            key=graph.queue_sort_key,
        )
        if group:
            grouped[name] = [_summary(item, by_key) for item in group]
    return {"source": _read_source(source), "total": len(by_key), "by_status": grouped}


def tool_show(key: str, source: str | None = None) -> dict[str, Any]:
    by_key = load(_read_source(source))
    item = by_key.get(key)
    if item is None:
        raise LookupError(f"no such item: {key}")
    view = _summary(item, by_key)
    view["source"] = _read_source(source)
    view["blocked_on"] = [
        {"key": dep,
         "title": by_key[dep]["title"] if dep in by_key else None,
         "done": by_key.get(dep, {}).get("status") == "done",
         "known": dep in by_key}
        for dep in item.get("blocked_on") or []
    ]
    view["blocks"] = sorted(
        other_key for other_key, other in by_key.items()
        if key in (other.get("blocked_on") or [])
    )
    view["artifacts"] = graph.normalize_artifacts(item)
    view["refs"] = list(item.get("refs") or [])
    if (item.get("defer_reason") or "").strip():
        view["defer_reason"] = " ".join(item["defer_reason"].split())
    if item.get("evidence"):
        view["evidence"] = item["evidence"].rstrip()
    return view


def tool_validate(source: str | None = None) -> dict[str, Any]:
    resolved = _read_source(source)
    by_key = load(resolved)
    arcs = load_arcs(resolved)
    problems = graph.validate_graph(by_key) + graph.validate_arcs(arcs, by_key)
    return {
        "source": resolved,
        "ok": not problems,
        "items": len(by_key),
        "arcs": len(arcs),
        "problems": problems,
        # Never a failure. An arc with no startable work describes a thin
        # backlog, not a malformed graph — see `graph.arc_findings`.
        "findings": graph.arc_findings(arcs, by_key),
    }


# --- writes -------------------------------------------------------------------


def _capture(command: Callable[[Namespace], int], args: Namespace) -> dict[str, Any]:
    """Run one CLI command with its output diverted off the protocol stream.

    The redirect is the entire reason this wrapper exists. `cmd_claim` and
    friends `print` — to stdout for what happened, to stderr for what to do next
    — and stdout here is the JSON-RPC transport. Swapping both means those lines
    reach the agent as the tool's result, which is where they were always meant
    to go, instead of desynchronising the client's parser.

    `SystemExit` is caught rather than allowed to propagate because the served
    path raises it: `cli._api` turns an HTTP error into `SystemExit` after
    printing the server's own message. Letting that escape would take the whole
    MCP server down over one failed request against a backend that is merely
    unreachable.
    """
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = command(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if exc.code and not isinstance(exc.code, int):
            err.write(f"{exc.code}\n")
    result: dict[str, Any] = {
        "ok": code == 0,
        "exit_code": code,
        "output": out.getvalue().strip(),
    }
    # Kept under its own key rather than merged: the CLI uses stderr for the
    # advice that follows a SUCCESSFUL command ("when you finish: release"), so
    # folding it into `output` would read as failure and dropping it would lose
    # the one instruction a claiming session most needs.
    if notes := err.getvalue().strip():
        result["notes"] = notes
    return result


def tool_claim(key: str, by: str | None = None, force: bool = False,
               source: str | None = None) -> dict[str, Any]:
    from .cli import cmd_claim

    return _capture(cmd_claim, Namespace(
        key=key, by=by, force=bool(force), source=_write_source(source),
    ))


def tool_release(key: str, source: str | None = None) -> dict[str, Any]:
    from .cli import cmd_release

    return _capture(cmd_release, Namespace(key=key, source=_write_source(source)))


def tool_set_status(key: str, status: str, source: str | None = None) -> dict[str, Any]:
    from .cli import cmd_status

    if status not in graph.STATUSES:
        raise ValueError(f"unknown status {status!r} — one of {', '.join(graph.STATUSES)}")
    return _capture(cmd_status, Namespace(
        key=key, status=status, source=_write_source(source),
    ))


HANDLERS: dict[str, Callable[..., Any]] = {
    "ready": tool_ready,
    "list": tool_list,
    "show": tool_show,
    "validate": tool_validate,
    "claim": tool_claim,
    "release": tool_release,
    "set_status": tool_set_status,
}


class UnknownTool(ValueError):
    """A name this bridge does not serve.

    Its own type, and a `ValueError` subclass so anything catching that still
    catches this. The distinction is for the agent on the other end: a bad
    *value* ("finished" is not a status) and a bad *name* ("nonesuch" is not a
    tool) send it looking in completely different places, and one message
    cannot serve both. Reported before the ValueError branch below for the
    same reason switchboard orders its SpecError ahead of one.
    """


def dispatch(name: str, arguments: dict[str, Any]) -> Any:
    handler = HANDLERS.get(name)
    if handler is None:
        raise UnknownTool(f"unknown tool: {name} — have {', '.join(sorted(HANDLERS))}")
    return handler(**arguments)


# --- JSON-RPC -----------------------------------------------------------------


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    is_notification = "id" not in request

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
        return _response(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "roadmap", "version": installed_version()},
            "instructions": INSTRUCTIONS,
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _response(request_id, {})

    if method == "tools/list":
        return _response(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            result = dispatch(name, arguments)
        except LookupError as exc:
            return _response(request_id, _tool_result(
                {"error": "no_such_item", "detail": str(exc)}, is_error=True))
        except ImportError as exc:
            # The `files` source needs PyYAML and the bare package does not ship
            # it. Named rather than folded into `bad_request`, because the fix is
            # an install and nothing about the request was wrong. Note that
            # `cli.load` converts its own missing-PyYAML ImportError into a
            # SystemExit, so that particular case lands in the branch below;
            # this one remains for an ImportError raised anywhere else.
            return _response(request_id, _tool_result(
                {"error": "missing_dependency", "detail": str(exc),
                 "fix": "pip install 'roadmap-core[files]'"}, is_error=True))
        except UnknownTool as exc:
            return _response(request_id, _tool_result(
                {"error": "unknown_tool", "detail": str(exc)}, is_error=True))
        except TypeError as exc:
            return _response(request_id, _tool_result(
                {"error": "bad_arguments", "detail": str(exc)}, is_error=True))
        except ValueError as exc:
            return _response(request_id, _tool_result(
                {"error": "bad_request", "detail": str(exc)}, is_error=True))
        except OSError as exc:
            # A missing SQLite file, an unwritable directory, a backend that will
            # not answer. Environmental, and worth distinguishing from a graph
            # that is genuinely malformed.
            return _response(request_id, _tool_result(
                {"error": "store_unavailable", "detail": str(exc)}, is_error=True))
        except SystemExit as exc:
            # THE READS ARE NOT WRAPPED IN `_capture`, AND THIS IS THE SEAM.
            # `cli.load` reports every graph problem by raising SystemExit —
            # unreadable YAML, an id that disagrees with its filename, a
            # non-kebab-case key, PyYAML missing for `source=files`. SystemExit
            # descends from BaseException, so it passes straight through every
            # `except Exception` between here and the top and ends the process.
            # On a stdio server that is not an exit: it is the client's pipe
            # closing mid-session, with the diagnosis going to a stderr nobody
            # is reading. It bites hardest on `validate`, whose whole job is to
            # report exactly these problems — an agent asking what is wrong with
            # the graph would get a dead server instead of the answer.
            #
            # The writes never reach here because `_capture` already catches
            # SystemExit for the same reason. This is the reads' half of it.
            detail = str(exc.code) if exc.code not in (None, 0) else "the graph could not be read"
            payload = {"error": "graph_unreadable", "detail": detail}
            # Semantic rather than sniffed out of the message: if PyYAML is not
            # importable at all, a `files` read cannot have failed for any other
            # reason, and the fix is an install rather than an edit.
            if importlib.util.find_spec("yaml") is None:
                payload["fix"] = "pip install 'roadmap-core[files]'"
            return _response(request_id, _tool_result(payload, is_error=True))
        except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the server
            log("tool error:\n" + traceback.format_exc())
            return _response(request_id, _tool_result(
                {"error": "tool_failed", "detail": f"{type(exc).__name__}: {exc}"},
                is_error=True))
        return _response(request_id, _tool_result(result))

    if is_notification:
        return None
    return _error(request_id, JSONRPC_METHOD_NOT_FOUND, f"unknown method: {method}")


def serve_stdio(stdin: Any = None, stdout: Any = None) -> None:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            payload = _error(None, JSONRPC_PARSE_ERROR, "invalid JSON")
            stdout.write(json.dumps(payload) + "\n")
            stdout.flush()
            continue
        if not isinstance(request, dict):
            payload = _error(None, JSONRPC_INVALID_REQUEST, "expected a JSON object")
            stdout.write(json.dumps(payload) + "\n")
            stdout.flush()
            continue
        try:
            response = handle_request(request)
        except (Exception, SystemExit):  # noqa: BLE001 - one bad request must not end the session
            # SystemExit named explicitly alongside Exception: it is not an
            # `Exception` subclass, and a `sys.exit` reached from anywhere under
            # here is a request that failed, never a server that should stop
            # answering the ones after it.
            log("unhandled error:\n" + traceback.format_exc())
            response = _error(
                request.get("id"), JSONRPC_INTERNAL_ERROR, "internal error (see stderr)")
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()


USAGE = """usage: roadmap-mcp

Serve the roadmap as MCP tools over stdio. Reads newline-delimited JSON-RPC on
stdin and writes one frame per line to stdout, so it is normally started by an
MCP client rather than run by hand.

  -h, --help     this message
  -V, --version  the installed roadmap-core version

environment:
  ROADMAP_SOURCE     files (the default for reads) | local | db. Writes never
                     go to files; an environment saying so writes to db.
  ROADMAP_STORE      path to the SQLite store used by source=local
  ROADMAP_REPO_ROOT  the checkout to resolve roadmap/items against

Seven tools: ready, list, show, validate, claim, release, set_status."""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        # A stdio server is launched by a client and takes no arguments, so
        # anything here is a misconfiguration. It used to be answered by
        # ignoring argv and blocking on stdin, which is the worst available
        # response to a typo in an `.mcp.json`: the client sees a server that
        # never completes a handshake, and nothing anywhere says why. `--help`
        # from a terminal hung the same way.
        if args[0] in ("-h", "--help"):
            # stdout, because this path never serves — no frames follow it.
            print(USAGE)
            return 0
        if args[0] in ("-V", "--version"):
            print(installed_version())
            return 0
        print(f"roadmap-mcp: unrecognised argument: {args[0]}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    serve_stdio()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
