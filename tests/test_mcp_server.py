"""The MCP bridge: the protocol it speaks, and the stream it must not corrupt.

Everything here runs against a ``LocalStore`` in ``tmp_path`` and never touches
``roadmap/items/``. That is the same rule the rest of this suite follows — a
package claiming to be adoptable cannot prove it with tests that need this
repository's own backlog — and it has a second payoff here: the write tools
reach the item files through ``cmd_claim``, so a test that let them find real
ones would edit the checkout it is running in.

The isolation job installs ``[dev]`` and fails if ``yaml`` is importable at all,
so nothing below may use ``source="files"``. Every read is ``local``.
"""

from __future__ import annotations

import io
import json

import pytest

from roadmap_core import cli, mcp_server
from roadmap_core.stores import LocalStore


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """A store with three items in it, and the CLI pointed at that store.

    ``ROADMAP_STORE_PATH`` is patched rather than the environment variable it is
    built from, because ``cli`` reads that variable once at import — setting the
    env here would leave every command still opening the real path, and the
    tests would pass while proving nothing about the store they set up.
    """
    path = tmp_path / "roadmap.db"
    with LocalStore(path) as local:
        local.upsert_item({"key": "alpha", "title": "Alpha", "status": "ready"})
        local.upsert_item({"key": "beta", "title": "Beta", "status": "ready",
                           "blocked_on": ["alpha"]})
        local.upsert_item({"key": "gamma", "title": "Gamma", "status": "done"})
    monkeypatch.setattr(cli, "ROADMAP_STORE_PATH", str(path))
    monkeypatch.setenv("ROADMAP_SOURCE", "local")
    # The advisory lease is a no-op without a token, and an inherited one would
    # make these tests reach a hub over the network.
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    return path


def _rpc(*requests: dict) -> list[dict]:
    """Drive `serve_stdio` end to end and return the frames it wrote.

    Through the real loop rather than calling `handle_request` directly: the
    thing most worth pinning is what lands on stdout, and only the loop writes
    there.
    """
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    mcp_server.serve_stdio(stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def _call(name: str, **arguments):
    """One tools/call, with its result decoded back out of the text content."""
    frames = _rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}})
    assert len(frames) == 1
    result = frames[0]["result"]
    payload = json.loads(result["content"][0]["text"])
    return payload, result.get("isError", False)


# --- the protocol -------------------------------------------------------------


def test_initialize_answers_with_tools_and_a_version():
    frames = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05"}})
    result = frames[0]["result"]
    # Echoed, not overridden: a client that asked for a version we speak gets it.
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "roadmap"


def test_an_unknown_protocol_version_gets_the_newest_we_speak():
    frames = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "1999-01-01"}})
    assert frames[0]["result"]["protocolVersion"] == mcp_server.LATEST_PROTOCOL


def test_notifications_are_not_answered():
    """A response to a notification is a protocol violation, and clients that
    validate strictly drop the connection over it."""
    assert _rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_every_tool_advertises_a_schema_and_has_a_handler():
    names = {tool["name"] for tool in mcp_server.TOOLS}
    assert names == set(mcp_server.HANDLERS)
    for tool in mcp_server.TOOLS:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_a_malformed_line_is_reported_without_ending_the_session():
    stdin = io.StringIO(
        "{not json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
    )
    stdout = io.StringIO()
    mcp_server.serve_stdio(stdin=stdin, stdout=stdout)
    frames = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert frames[0]["error"]["code"] == mcp_server.JSONRPC_PARSE_ERROR
    # The point of the test: the second request was still served.
    assert frames[1]["id"] == 2


# --- stdout is the protocol ---------------------------------------------------


def test_a_write_does_not_leak_the_cli_prints_onto_the_stream():
    """THE regression this module's `_capture` exists for.

    `cmd_claim` prints four lines a human is meant to read. stdout here is the
    JSON-RPC transport, so an uncaptured one is not noise — it desynchronises
    the client's parser for the rest of the session, and the error surfaces a
    long way from the print that caused it.

    Asserted as "every line on stdout parses as JSON-RPC", which is the property
    that actually matters, rather than by matching the strings the CLI happens
    to print today.
    """
    stdin = io.StringIO(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "claim", "arguments": {"key": "alpha", "by": "tester"}},
    }) + "\n")
    stdout = io.StringIO()
    mcp_server.serve_stdio(stdin=stdin, stdout=stdout)

    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, f"the CLI's prints reached the protocol stream: {lines}"
    for line in lines:
        assert json.loads(line)["jsonrpc"] == "2.0"


def test_the_cli_output_is_returned_instead_of_discarded():
    """Captured, not silenced. What `claim` says is what the agent needs."""
    payload, is_error = _call("claim", key="alpha", by="tester")
    assert not is_error
    assert payload["ok"] is True
    assert "claimed alpha for tester" in payload["output"]
    # The "when you finish" notice goes to stderr, and it is the one instruction
    # a claiming session most needs — so it is kept, under its own key.
    assert "release" in payload["notes"]


# --- reads --------------------------------------------------------------------


def test_ready_offers_only_what_is_startable():
    payload, is_error = _call("ready")
    assert not is_error
    keys = [item["key"] for item in payload["ready"]]
    # beta is blocked on alpha, gamma is done.
    assert keys == ["alpha"]


def test_ready_reflects_a_claim_taken_through_the_bridge():
    _call("claim", key="alpha", by="tester")
    payload, _ = _call("ready")
    assert payload["ready"] == []
    assert "nothing ready" in payload["note"]


def test_list_groups_by_derived_status_not_the_stored_column():
    """An item with a live claim reads `claimed` whatever its status column says
    — the divergence `a-claim-cannot-survive-the-floors-ci` documents."""
    _call("claim", key="alpha", by="tester")
    payload, _ = _call("list")
    assert [i["key"] for i in payload["by_status"]["claimed"]] == ["alpha"]
    assert payload["total"] == 3


def test_list_can_be_filtered_to_one_status():
    payload, _ = _call("list", status="done")
    assert set(payload["by_status"]) == {"done"}


def test_show_names_both_directions_of_the_dependency_edge():
    payload, is_error = _call("show", key="alpha")
    assert not is_error
    assert payload["blocks"] == ["beta"]
    assert payload["blocked_on"] == []

    payload, _ = _call("show", key="beta")
    assert payload["blocked_on"][0]["key"] == "alpha"
    assert payload["blocked_on"][0]["done"] is False


def test_validate_reports_a_dangling_dependency():
    with LocalStore(cli.ROADMAP_STORE_PATH) as local:
        local.upsert_item({"key": "delta", "title": "Delta", "blocked_on": ["nope"]})
    payload, _ = _call("validate")
    assert payload["ok"] is False
    assert any("nope" in problem for problem in payload["problems"])


# --- failures are answers, not crashes ----------------------------------------


def test_an_unknown_item_is_a_named_error():
    payload, is_error = _call("show", key="does-not-exist")
    assert is_error
    assert payload["error"] == "no_such_item"


def test_an_unknown_tool_is_a_named_error():
    payload, is_error = _call("nonesuch")
    assert is_error
    assert payload["error"] == "unknown_tool"


def test_a_bad_argument_is_a_named_error():
    payload, is_error = _call("show")          # `key` is required
    assert is_error
    assert payload["error"] == "bad_arguments"


def test_an_unknown_status_is_refused_before_the_store_is_touched():
    payload, is_error = _call("set_status", key="alpha", status="finished")
    assert is_error
    assert payload["error"] == "bad_request"


def test_a_failing_tool_leaves_the_server_answering():
    frames = _rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "show", "arguments": {"key": "does-not-exist"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert frames[0]["result"]["isError"] is True
    assert frames[1]["result"]["tools"]


# --- source resolution --------------------------------------------------------


def test_a_write_never_goes_to_the_files_source(monkeypatch):
    """`files` is a projection of a write something else arbitrated. Offering it
    as a write target would be a claim no transaction decided — two sessions
    could each hold the same item."""
    monkeypatch.setenv("ROADMAP_SOURCE", "files")
    assert mcp_server._write_source() == "db"
    assert "files" not in mcp_server.WRITE_SOURCES


def test_reads_default_to_files_when_nothing_says_otherwise(monkeypatch):
    """The default that makes a fresh clone answerable: no server, no token."""
    monkeypatch.delenv("ROADMAP_SOURCE", raising=False)
    assert mcp_server._read_source() == "files"


def test_an_explicit_source_beats_the_environment(monkeypatch):
    monkeypatch.setenv("ROADMAP_SOURCE", "db")
    assert mcp_server._read_source("local") == "local"


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError):
        mcp_server._read_source("postgres")


# --- writes -------------------------------------------------------------------


def test_release_hands_the_item_back_to_the_queue():
    _call("claim", key="alpha", by="tester")
    payload, is_error = _call("release", key="alpha")
    assert not is_error and payload["ok"]
    assert [i["key"] for i in _call("ready")[0]["ready"]] == ["alpha"]


def test_losing_a_claim_race_is_a_refusal_and_not_a_crash():
    """An ordinary outcome. The store arbitrates, so the second caller is told
    who holds it rather than handed a traceback."""
    _call("claim", key="alpha", by="first")
    payload, _ = _call("claim", key="alpha", by="second")
    assert payload["ok"] is False
    assert "first" in (payload["notes"] or "") + payload["output"]


def test_set_status_done_drops_the_claim():
    _call("claim", key="alpha", by="tester")
    payload, is_error = _call("set_status", key="alpha", status="done")
    assert not is_error and payload["ok"]
    shown, _ = _call("show", key="alpha")
    assert shown["status"] == "done"
    assert "claimed_by" not in shown


def test_set_status_verifying_keeps_the_claim():
    """The branch that shipped the work still owns observing that it landed."""
    _call("claim", key="alpha", by="tester")
    _call("set_status", key="alpha", status="verifying")
    shown, _ = _call("show", key="alpha")
    assert shown["claimed_by"] == "tester"


# --- the load path reports failure without ending the session -----------------
#
# `cli.load` raises SystemExit for every graph problem it finds, and SystemExit
# is not an `Exception`. The reads call `load` directly rather than through
# `_capture`, so nothing between them and the interpreter catches it. These
# monkeypatch `load` rather than pointing a read at a broken `roadmap/items/`,
# because this file may not use `source="files"` at all — the isolation job runs
# it with PyYAML absent. What is being pinned is the exception type, and that
# does not need a real YAML file to be raised.


@pytest.mark.parametrize("tool", ["ready", "list", "validate", "show"])
def test_an_unreadable_graph_is_an_error_frame_not_a_dead_server(tool, monkeypatch):
    """The bug this guards: a single malformed item file used to take the whole
    server down, and `validate` — the tool whose entire job is to report that
    file — went down with it."""
    def explode(*_args, **_kwargs):
        raise SystemExit("roadmap/items/oops.yaml: invalid YAML: mapping values not allowed")

    monkeypatch.setattr(mcp_server, "load", explode)
    arguments = {"key": "alpha"} if tool == "show" else {}
    frames = _rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": tool, "arguments": arguments}},
        # The point of the test is this second frame: the session outlives it.
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    )
    assert len(frames) == 2, "the server stopped answering after an unreadable graph"
    payload = json.loads(frames[0]["result"]["content"][0]["text"])
    assert frames[0]["result"]["isError"] is True
    assert payload["error"] == "graph_unreadable"
    assert "oops.yaml" in payload["detail"]
    assert frames[1]["id"] == 2


def test_a_missing_pyyaml_names_the_install_that_fixes_it(monkeypatch):
    """`load` turns its own ImportError into SystemExit, so the missing-PyYAML
    case arrives as one. It still has to arrive carrying the install."""
    def explode(*_args, **_kwargs):
        raise SystemExit("PyYAML required for --source files (pip install pyyaml)")

    monkeypatch.setattr(mcp_server, "load", explode)
    monkeypatch.setattr(mcp_server.importlib.util, "find_spec", lambda name: None)
    payload, is_error = _call("ready")
    assert is_error is True
    assert payload["error"] == "graph_unreadable"
    assert payload["fix"] == "pip install 'roadmap-core[files]'"


def test_a_readable_graph_carries_no_install_advice(monkeypatch):
    """The `fix` key is conditional. A malformed file in a checkout that HAS
    PyYAML is an edit, not an install, and saying otherwise sends the reader to
    the wrong place."""
    def explode(*_args, **_kwargs):
        raise SystemExit("roadmap/items/oops.yaml: expected a mapping at the top level")

    monkeypatch.setattr(mcp_server, "load", explode)
    monkeypatch.setattr(mcp_server.importlib.util, "find_spec", lambda name: object())
    payload, _ = _call("validate")
    assert "fix" not in payload


def test_a_systemexit_escaping_the_handler_still_leaves_the_loop_running(monkeypatch):
    """Defence in depth for the loop itself, not the tools/call branch: a
    SystemExit raised outside a tool must not end the session either."""
    def explode(_request):
        raise SystemExit("something above the tools")

    monkeypatch.setattr(mcp_server, "handle_request", explode)
    frames = _rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert len(frames) == 1
    assert frames[0]["error"]["code"] == mcp_server.JSONRPC_INTERNAL_ERROR
