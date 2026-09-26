"""The roadmap server: many tenants, one SQLite file each, token-authenticated.

Every test runs a real server on an ephemeral loopback port and talks to it over
HTTP with ``urllib`` — the same library the CLI's ``_api`` uses — because the
properties under test live in the transport: which tenant a token reaches, what a
401 says, what reaches the audit log. A handler called directly would pass them
by having no wire.

Each test says what broken looks like. Nothing here imports Lucille; the server
is stdlib, and the isolation job asserts that stays true.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from roadmap_core import cli, server
from roadmap_core.stores import ApiStore


@pytest.fixture
def served(tmp_path: Path):
    srv = server.make_server(tmp_path / "data", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"  # type: ignore[attr-defined]
    yield srv
    srv.shutdown()
    srv.server_close()


def call(base: str, method: str, path: str, token: str | None = None,
         body: Any = None, headers: dict[str, str] | None = None,
         context: ssl.SSLContext | None = None) -> tuple[int, Any, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10, context=context) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None, resp.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None, exc.headers


def tenant(srv: server.RoadmapServer, name: str, scopes=("read", "write"),
           label: str = "wren (ceo)") -> str:
    if name not in {t["id"] for t in srv.registry.tenants()}:
        srv.registry.add_tenant(name)
    return srv.registry.create_token(name, label=label, scopes=scopes)[1]


def item(key: str, **fields: Any) -> dict[str, Any]:
    return {"key": key, "title": key.replace("-", " "), "evidence": "measured", **fields}


# --- who a request is ---------------------------------------------------------


def test_every_request_without_a_valid_token_gets_the_same_401(served):
    """Broken looks like: a revoked, expired or disabled-tenant token reaching
    data, or the 401 saying which of those it was — a probe learns the difference."""
    good = tenant(served, "org-a")
    revoked_id, revoked = served.registry.create_token("org-a", label="gone")
    served.registry.revoke_token(revoked_id)
    _, expired = served.registry.create_token("org-a", label="old", expires_days=1e-9)
    disabled = tenant(served, "org-off")
    served.registry.disable_tenant("org-off")
    assert call(served.base, "GET", "/admin/roadmap", good)[0] == 200
    answers = []
    for tok in (None, "", "rmk_nope", "not-even-the-prefix", revoked, expired, disabled):
        code, body, headers = call(served.base, "GET", "/admin/roadmap", tok)
        answers.append((code, body["detail"], headers.get("WWW-Authenticate")))
    assert set(answers) == {(401, "missing or invalid bearer token", 'Bearer realm="roadmap"')}


def test_a_token_sees_only_its_own_tenant(served, tmp_path):
    """Broken looks like: tenant B listing, claiming or learning of tenant A's
    item — or both tenants' rows landing in one file."""
    a, b = tenant(served, "org-a"), tenant(served, "org-b")
    assert call(served.base, "PUT", "/admin/roadmap/secret-plan", a, item("secret-plan"))[0] == 200
    assert call(served.base, "GET", "/admin/roadmap", b)[1]["items"] == []
    assert call(served.base, "POST", "/admin/roadmap/secret-plan/claim", b, {"by": "x"})[0] == 404
    assert call(served.base, "GET", "/admin/roadmap/secret-plan/history", b)[0] == 404
    assert call(served.base, "PUT", "/admin/roadmap/secret-plan", b, item("secret-plan"))[0] == 200
    call(served.base, "POST", "/admin/roadmap/secret-plan/claim", b, {"by": "bob"})
    a_items = call(served.base, "GET", "/admin/roadmap", a)[1]["items"]
    assert [(i["key"], i["claimed_by"]) for i in a_items] == [("secret-plan", None)]
    files = sorted(p.name for p in (tmp_path / "data" / "tenants").glob("*.db"))
    assert files == ["org-a.db", "org-b.db"]


def test_each_scope_opens_only_its_own_doors(served):
    """Broken looks like: a read token writing, a write token pruning or taking an
    item from its holder, or an admin token refused what it is for."""
    reader = tenant(served, "org-a", scopes=("read",))
    writer = tenant(served, "org-a", scopes=("read", "write"))
    admin = tenant(served, "org-a", scopes=("read", "write", "admin"))
    assert call(served.base, "PUT", "/admin/roadmap/one", reader, item("one"))[0] == 403
    assert call(served.base, "PUT", "/admin/roadmap/one", writer, item("one"))[0] == 200
    assert call(served.base, "GET", "/admin/roadmap", reader)[0] == 200
    assert call(served.base, "POST", "/admin/roadmap/one/claim", writer, {"by": "s1"})[0] == 200
    forced = call(served.base, "POST", "/admin/roadmap/one/claim", writer,
                  {"by": "s2", "force": True})
    assert forced[0] == 403 and "admin scope" in forced[1]["detail"]
    assert call(served.base, "DELETE", "/admin/roadmap/one", writer)[0] == 403
    assert call(served.base, "POST", "/admin/roadmap/one/refile", writer)[0] == 403
    assert call(served.base, "POST", "/admin/roadmap/one/claim", admin,
                {"by": "s2", "force": True})[1]["claimed_by"] == "s2"
    assert call(served.base, "DELETE", "/admin/roadmap/one", admin)[0] == 200


# --- the served contract -----------------------------------------------------


def test_it_serves_the_contract_the_host_api_serves(served):
    """The paths, codes and shapes Lucille's `/admin/roadmap` returns, so the CLI's
    `db` source and `ApiStore` work against this unchanged. Broken looks like: a
    stored status where the derived one belongs, a second claimer admitted, a
    release that writes `ready` over `blocked`, or a pruned key a push recreates."""
    tok = tenant(served, "org-a", scopes=("read", "write", "admin"))
    base = served.base
    assert call(base, "PUT", "/admin/roadmap/first", tok, item("first"))[0] == 200
    assert call(base, "PUT", "/admin/roadmap/second", tok,
                item("second", blocked_on=["first"]))[0] == 200
    listed = {i["key"]: i for i in call(base, "GET", "/admin/roadmap", tok)[1]["items"]}
    assert listed["second"]["status"] == "blocked" and listed["second"]["unmet_deps"] == ["first"]
    assert listed["first"]["blocks"] == ["second"]
    ready = call(base, "GET", "/admin/roadmap/ready", tok)[1]
    assert [i["key"] for i in ready["items"]] == ["first"] and ready["total_ready"] == 1

    code, claimed, _ = call(base, "POST", "/admin/roadmap/first/claim", tok, {"by": "session-1"})
    assert code == 200 and claimed["status"] == "claimed" and claimed["claimed_by"] == "session-1"
    lost = call(base, "POST", "/admin/roadmap/first/claim", tok, {"by": "session-2"})
    assert lost[0] == 409 and "session-1" in lost[1]["detail"]
    assert call(base, "POST", "/admin/roadmap/nope/claim", tok, {"by": "x"})[0] == 404
    assert call(base, "POST", "/admin/roadmap/first/release", tok, {})[1]["status"] == "ready"
    done = call(base, "POST", "/admin/roadmap/first/status", tok, {"status": "done"})[1]
    assert done["status"] == "done" and done["claimed_by"] is None and done["done_at"]
    assert call(base, "POST", "/admin/roadmap/first/status", tok, {"status": "shipped"})[0] == 400
    # a re-push never moves status: `first` stays done though the body says ready
    assert call(base, "PUT", "/admin/roadmap/first", tok, item("first", status="ready"))[1][
        "status"] == "done"

    assert call(base, "DELETE", "/admin/roadmap/first", tok)[1] == {"deleted": True}
    assert call(base, "PUT", "/admin/roadmap/first", tok, item("first"))[0] == 409
    never = call(base, "DELETE", "/admin/roadmap/never-pushed", tok)
    assert never[0] == 404, "no row to delete is still a 404"
    prunes = call(base, "GET", "/admin/roadmap/prunes", tok)[1]
    assert {p["key"] for p in prunes["prunes"]} == {"first", "never-pushed"}, \
        "the tombstone is written even when there was no row — the served path's rule"
    assert call(base, "POST", "/admin/roadmap/first/refile", tok)[1] == {"refiled": True}
    assert call(base, "PUT", "/admin/roadmap/first", tok, item("first"))[0] == 200
    assert call(base, "POST", "/admin/roadmap/first/refile", tok)[0] == 404

    arc = {"key": "the-arc", "title": "An arc", "narrative": "why"}
    assert call(base, "PUT", "/admin/roadmap/arcs/the-arc", tok, arc)[1]["title"] == "An arc"
    assert [a["key"] for a in call(base, "GET", "/admin/roadmap/arcs", tok)[1]["arcs"]] == [
        "the-arc"]
    assert call(base, "DELETE", "/admin/roadmap/arcs/the-arc", tok)[1] == {"deleted": "the-arc"}
    assert call(base, "GET", "/admin/roadmap/second/impact", tok)[0] == 501
    assert call(base, "GET", "/admin/elsewhere", tok)[0] == 404
    assert call(base, "DELETE", "/admin/roadmap/prunes", tok)[0] == 400
    assert "prunes" not in {p["key"] for p in call(base, "GET", "/admin/roadmap/prunes", tok)[1][
        "prunes"]}, "a reserved word never becomes a tombstone"
    reserved = call(base, "PUT", "/admin/roadmap/arcs", tok, item("arcs"))
    assert reserved[0] == 400 and "uses for itself" in reserved[1]["detail"]


def test_apistore_and_the_cli_speak_to_it_unchanged(served, monkeypatch):
    """Broken looks like: the client needing anything but a URL and a token —
    `ApiStore` and the CLI's `_api` are the callers this server exists for."""
    tok = tenant(served, "org-a")

    def via_http(method: str, path: str, payload: dict | None = None) -> Any:
        code, body, _ = call(served.base, method, path, tok, payload)
        assert code < 400, (code, body)
        return body

    with ApiStore(via_http) as api:
        via_http("PUT", "/admin/roadmap/shared", item("shared"))
        assert api.claim("shared", by="wren")["claimed_by"] == "wren"
        assert set(api.items()) == {"shared"}
        assert api.release("shared")["claimed_by"] is None
    monkeypatch.setenv("ROADMAP_API_URL", served.base)
    monkeypatch.setenv("ROADMAP_API_TOKEN", tok)
    assert set(cli.load_from_db()) == {"shared"}
    monkeypatch.delenv("ROADMAP_API_TOKEN")
    monkeypatch.setenv("LUCILLE_ADMIN_JWT", tok)
    assert set(cli.load_from_db()) == {"shared"}, "the extracted-from names still work"
    monkeypatch.delenv("LUCILLE_ADMIN_JWT")
    with pytest.raises(SystemExit, match="ROADMAP_API_TOKEN is not set"):
        cli.load_from_db()


# --- history and races --------------------------------------------------------


def test_every_status_and_claim_change_is_in_the_transition_log(served):
    """The per-item history the store never kept. Broken looks like: a claim,
    release or status change missing from `/history`, an actor that is not the
    token's label, or a cursor that skips or repeats a transition."""
    tok = tenant(served, "org-a", label="reed (reviewer)")
    base = served.base
    call(base, "PUT", "/admin/roadmap/tracked", tok, item("tracked"))
    call(base, "POST", "/admin/roadmap/tracked/claim", tok, {"by": "builder-7"})
    call(base, "POST", "/admin/roadmap/tracked/status", tok, {"status": "verifying"})
    call(base, "POST", "/admin/roadmap/tracked/status", tok, {"status": "done"})
    hist = call(base, "GET", "/admin/roadmap/tracked/history", tok)[1]["transitions"]
    assert [(h["op"], h["from_status"], h["to_status"]) for h in hist] == [
        ("create", None, "ready"), ("claim", "ready", "claimed"),
        ("status", "claimed", "verifying"), ("status", "verifying", "done")]
    assert hist[1]["claimed_by"] == "builder-7"
    assert {h["actor"].split(" (tok_")[0] for h in hist} == {"reed (reviewer)"}
    first = call(base, "GET", "/admin/roadmap/transitions?limit=2", tok)[1]
    rest = call(base, "GET", f"/admin/roadmap/transitions?after={first['next']}", tok)[1]
    assert [t["id"] for t in first["transitions"] + rest["transitions"]] == [h["id"] for h in hist]
    assert call(base, "GET", "/admin/roadmap/transitions?after=x", tok)[0] == 400


def test_a_claim_race_has_exactly_one_winner(served):
    """Broken looks like: two sessions both told the item is theirs — the one
    thing a claim exists to prevent."""
    tok = tenant(served, "org-a")
    call(served.base, "PUT", "/admin/roadmap/contested", tok, item("contested"))
    codes: list[int] = []
    lock = threading.Lock()

    def claim(n: int) -> None:
        code = call(served.base, "POST", "/admin/roadmap/contested/claim", tok,
                    {"by": f"session-{n}"})[0]
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200] + [409] * 7


# --- the wire -----------------------------------------------------------------


def _raw(srv, method: str, path: str, token: str, body: bytes, headers: dict[str, str]):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=body,
                 headers={"Authorization": f"Bearer {token}", **headers})
    resp = conn.getresponse()
    out = resp.status, json.loads(resp.read() or b"null")
    conn.close()
    return out


def test_what_a_request_may_carry_is_bounded(served, monkeypatch):
    """Broken looks like: an oversized, chunked, non-JSON or mis-typed body read
    and acted on, or a key that is not a key reaching the store."""
    tok = tenant(served, "org-a")
    monkeypatch.setattr(server, "MAX_BODY_BYTES", 64)
    big = json.dumps(item("big", evidence="x" * 200)).encode()
    assert _raw(served, "PUT", "/admin/roadmap/big", tok, big,
                {"Content-Type": "application/json"})[0] == 413
    assert _raw(served, "PUT", "/admin/roadmap/big", tok, b"{}",
                {"Content-Type": "text/plain"})[0] == 415
    assert _raw(served, "PUT", "/admin/roadmap/big", tok, b"{nope",
                {"Content-Type": "application/json"})[0] == 400
    assert _raw(served, "PUT", "/admin/roadmap/big", tok, b"[1]",
                {"Content-Type": "application/json"})[0] == 400
    conn = http.client.HTTPConnection("127.0.0.1", served.server_address[1], timeout=10)
    conn.putrequest("POST", "/admin/roadmap/big/claim")
    conn.putheader("Authorization", f"Bearer {tok}")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(b"0\r\n\r\n")
    assert conn.getresponse().status == 411
    conn.close()
    monkeypatch.setattr(server, "MAX_BODY_BYTES", 1 << 20)
    for bad in ("Bad_Key", "..", "a--b", "x" * 201):
        assert call(served.base, "POST", f"/admin/roadmap/{bad}/release", tok, {})[0] == 400, bad
    assert call(served.base, "PUT", "/admin/roadmap/ready", tok, item("ready"))[0] in (400, 405)
    assert call(served.base, "PUT", "/admin/roadmap/prunes", tok, item("prunes"))[0] == 400
    wrong = call(served.base, "PUT", "/admin/roadmap/one", tok, item("two"))
    assert wrong[0] == 400 and "mismatch" in wrong[1]["detail"]
    assert call(served.base, "PUT", "/admin/roadmap/one", tok,
                item("one", blocked_on="first"))[0] == 400
    assert call(served.base, "PUT", "/admin/roadmap/one", tok,
                item("one", priority="urgent"))[0] == 400
    many = "&".join(f"k{n}=1" for n in range(30))
    assert call(served.base, "GET", f"/admin/roadmap/transitions?{many}", tok)[0] == 400


def test_budgets_answer_429_with_a_retry_after(served):
    """Broken looks like: a token or an address with no ceiling, or a 429 that does
    not say when to come back."""
    tok = tenant(served, "org-a")
    served.request_budget = server._Budget(0.01, 2)
    codes = [call(served.base, "GET", "/admin/roadmap", tok) for _ in range(3)]
    assert [c[0] for c in codes] == [200, 200, 429] and int(codes[2][2]["Retry-After"]) >= 1
    served.auth_failures = server._Budget(0.01, 3)
    fails = [call(served.base, "GET", "/admin/roadmap", "rmk_wrong")[0] for _ in range(4)]
    assert fails == [401, 401, 401, 429]


def test_errors_are_json_and_say_nothing_about_the_server(served, monkeypatch):
    """Broken looks like: an HTML error page, a Python version in `Server`, or a
    traceback in a 500."""
    tok = tenant(served, "org-a")
    conn = http.client.HTTPConnection("127.0.0.1", served.server_address[1], timeout=10)
    conn.request("PATCH", "/admin/roadmap")
    resp = conn.getresponse()
    assert resp.status == 501 and resp.getheader("Content-Type") == "application/json"
    assert resp.getheader("Server") == "roadmap" and json.loads(resp.read())["detail"]
    conn.close()

    def boom(req):
        raise RuntimeError("the secret internals")

    routes = tuple((m, p, s, boom if h is server._list_items else h)
                   for m, p, s, h in server.ROUTES)
    monkeypatch.setattr(server, "ROUTES", routes)
    code, body, headers = call(served.base, "GET", "/admin/roadmap", tok)
    assert code == 500 and set(body) == {"detail", "ref"} and "secret" not in json.dumps(body)
    assert headers["Cache-Control"] == "no-store"
    assert call(served.base, "GET", "/healthz")[1] == {"ok": True}


def test_plaintext_is_refused_off_loopback(tmp_path):
    """Broken looks like: a server that sends bearer tokens across a network in the
    clear because nobody said otherwise."""
    with pytest.raises(ValueError, match="refusing to serve plaintext"):
        server.make_server(tmp_path / "d", host="0.0.0.0", port=0)
    with pytest.raises(ValueError, match="both a certificate and its key"):
        server.make_server(tmp_path / "d", port=0, tls_cert=tmp_path / "c.pem")
    srv = server.make_server(tmp_path / "d", host="0.0.0.0", port=0, allow_plaintext=True)
    srv.server_close()


def test_it_serves_tls_and_the_handshake_is_not_on_the_accept_loop(tmp_path):
    """Broken looks like: HTTPS failing, plain HTTP answered on the TLS port, or a
    client that opens a socket and says nothing stalling every other client."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl is needed to mint a test certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                    "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1", "-subj",
                    "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    srv = server.make_server(tmp_path / "data", port=0, tls_cert=cert, tls_key=key)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        import socket
        silent = socket.create_connection(("127.0.0.1", port))  # never handshakes
        ctx = ssl.create_default_context(cafile=str(cert))
        tok = tenant(srv, "org-a")
        code, _, headers = call(f"https://127.0.0.1:{port}", "GET", "/admin/roadmap", tok,
                                context=ctx)
        assert code == 200 and "max-age" in headers["Strict-Transport-Security"]
        with pytest.raises((urllib.error.URLError, ConnectionError, http.client.HTTPException)):
            call(f"http://127.0.0.1:{port}", "GET", "/healthz")
        silent.close()
    finally:
        srv.shutdown()
        srv.server_close()


# --- what is kept -------------------------------------------------------------


def test_the_registry_holds_no_usable_credential(served, tmp_path):
    """Broken looks like: a token recoverable from the registry file or the audit
    log, or a data file readable by anyone but its owner."""
    tok = tenant(served, "org-a")
    call(served.base, "GET", "/admin/roadmap", tok)
    call(served.base, "GET", "/admin/roadmap", "rmk_" + "x" * 40)
    data = tmp_path / "data"
    blob = b"".join(p.read_bytes() for p in data.rglob("*") if p.is_file())
    assert tok.encode() not in blob and tok[4:].encode() not in blob
    audit = served.registry.audit_rows()
    assert {a["status"] for a in audit} >= {200, 401}
    assert all(a["token_id"] is None or a["token_id"].startswith("tok_") for a in audit)
    assert (data / "registry.db").stat().st_mode & 0o077 == 0
    assert (data / "tenants" / "org-a.db").stat().st_mode & 0o077 == 0


def test_the_server_and_the_cli_agree_on_what_a_key_is():
    """Broken looks like: the server accepting a key no item file could be named."""
    assert server.KEY_RE.pattern == cli._ID_RE.pattern


def test_tenants_and_tokens_are_managed_on_the_host(tmp_path, capsys):
    """`roadmap serve --data … tenant|token`. Broken looks like: a token printed by
    `list`, a second copy of a tenant, or a revoked token still authenticating."""
    d = str(tmp_path / "data")
    assert cli.main(["serve", "--data", d, "tenant", "add", "org-core"]) == 0
    with pytest.raises(SystemExit, match="already exists"):
        cli.main(["serve", "--data", d, "tenant", "add", "org-core"])
    with pytest.raises(SystemExit, match="not a tenant id"):
        cli.main(["serve", "--data", d, "tenant", "add", "../escape"])
    capsys.readouterr()
    cli.main(["serve", "--data", d, "token", "create", "--tenant", "org-core",
              "--label", "wren (ceo)", "--scopes", "read,write"])
    secret = capsys.readouterr().out.strip().splitlines()[-1]
    reg = server.Registry(d)
    principal = reg.authenticate(secret)
    assert principal is not None and principal.tenant == "org-core"
    assert principal.scopes == frozenset({"read", "write"})
    cli.main(["serve", "--data", d, "token", "list"])
    listed = capsys.readouterr().out
    assert principal.token_id in listed and secret not in listed
    cli.main(["serve", "--data", d, "token", "revoke", principal.token_id])
    assert reg.authenticate(secret) is None
    with pytest.raises(SystemExit, match="scopes must be"):
        cli.main(["serve", "--data", d, "token", "create", "--tenant", "org-core",
                  "--label", "x", "--scopes", "root"])
    assert os.environ.get("ROADMAP_API_TOKEN") != secret


def test_idle_connections_are_capped_and_the_cap_frees_itself(tmp_path, monkeypatch):
    """Broken looks like: a client that opens sockets and sends nothing holding a
    thread each without limit, or a cap that never gives its slots back."""
    import socket
    import time

    monkeypatch.setattr(server, "MAX_CONNECTIONS", 2)
    srv = server.make_server(tmp_path / "data", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        time.sleep(0.2)
        extra = socket.create_connection(("127.0.0.1", port))
        extra.settimeout(5)
        assert extra.recv(1) == b"", "the connection over the cap is closed at once"
        extra.close()
        for s in idle:
            s.close()
        deadline = time.monotonic() + 5
        while True:
            try:
                if call(f"http://127.0.0.1:{port}", "GET", "/healthz")[0] == 200:
                    break
            except (urllib.error.URLError, ConnectionError, http.client.HTTPException):
                pass
            assert time.monotonic() < deadline, "the slots never came back"
            time.sleep(0.05)
    finally:
        srv.shutdown()
        srv.server_close()


def test_since_is_read_in_utc_whatever_offset_it_is_written_with(served):
    """Broken looks like: `since` with a non-UTC offset compared as text against
    UTC timestamps — off by the offset."""
    tok = tenant(served, "org-a")
    call(served.base, "PUT", "/admin/roadmap/timed", tok, item("timed"))
    everything = call(served.base, "GET", "/admin/roadmap/transitions", tok)[1]["transitions"]
    at = server._parse(everything[0]["at"])
    ahead = (at - server._dt.timedelta(minutes=1)).astimezone(
        server._dt.timezone(server._dt.timedelta(hours=5)))
    from urllib.parse import quote
    got = call(served.base, "GET", f"/admin/roadmap/transitions?since={quote(ahead.isoformat())}",
               tok)[1]["transitions"]
    assert [t["key"] for t in got] == ["timed"]


def test_a_request_line_cannot_forge_a_log_line(served, caplog):
    """Broken looks like: a control character from the wire written raw into the
    server's log — a forged line, or a terminal escape, in the operator's view."""
    import logging
    import socket

    caplog.set_level(logging.INFO, logger="roadmap.server")
    sock = socket.create_connection(("127.0.0.1", served.server_address[1]))
    sock.sendall(b"GET /x\x1b[31mforged\x08 HTTP/1.0\r\n\r\n")
    sock.recv(4096)
    sock.close()
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "roadmap.server")
    assert "forged" in text and "\x1b" not in text and "\x08" not in text
    assert "\\x1b" in text


# --- user tokens (0.5.0)
# ---------------------------------------------------------------------------------------------

def _t(tenant: str) -> dict[str, str]:
    return {"X-Roadmap-Tenant": tenant}


RW = ("read", "write")


def _person(srv, user="gal", grants=(("org-core", RW), ("lucille", RW)), scopes=RW) -> str:
    for t, _ in grants:
        if t not in {x["id"] for x in srv.registry.tenants()}:
            srv.registry.add_tenant(t)
    srv.registry.add_user(user, label="gal (operator)")
    for t, sc in grants:
        srv.registry.grant(user, t, sc)
    return srv.registry.create_user_token(user, label="gal's sessions", scopes=scopes)[1]


def test_one_user_token_reaches_each_granted_tenant_by_name_and_no_other(served):
    """The operator, 2026-09-26: "we don't need token per org. we need token per user … that
    should be resolved from the token". One secret, and the request names the tenant. Broken
    looks like: the token reaching a tenant with no grant, or one tenant's items answering
    under another's name."""
    srv, base = served, served.base
    tok = _person(srv)
    srv.registry.add_tenant("numerotech")               # exists, and no grant on it
    for t in ("org-core", "lucille"):
        code, _, _ = call(base, "PUT", f"/admin/roadmap/{t}-item", tok, item(f"{t}-item"),
                          headers={"X-Roadmap-Tenant": t})
        assert code == 200, (t, code)
    code, body, _ = call(base, "GET", "/admin/roadmap", tok, headers=_t("org-core"))
    assert code == 200 and {i["key"] for i in body["items"]} == {"org-core-item"}
    code, body, _ = call(base, "GET", "/admin/roadmap", tok, headers=_t("lucille"))
    assert code == 200 and {i["key"] for i in body["items"]} == {"lucille-item"}
    for headers in (_t("numerotech"), _t("no-such"), {}, _t("../org-core")):
        code, _, _ = call(base, "GET", "/admin/roadmap", tok, headers=headers)
        assert code == 401, headers                     # the same no as a bad token


def test_a_user_tokens_scopes_are_its_cap_meeting_the_grant(served):
    srv, base = served, served.base
    tok = _person(srv, grants=(("org-core", RW), ("lucille", ("read",))))
    h = {"X-Roadmap-Tenant": "lucille"}
    assert call(base, "GET", "/admin/roadmap", tok, headers=h)[0] == 200
    # the grant is read
    assert call(base, "PUT", "/admin/roadmap/x", tok, item("x"), headers=h)[0] == 403
    reader = srv.registry.create_user_token("gal", label="fleet reader", scopes=("read",))[1]
    h = {"X-Roadmap-Tenant": "org-core"}
    assert call(base, "GET", "/admin/roadmap", reader, headers=h)[0] == 200
    # the token is read
    assert call(base, "PUT", "/admin/roadmap/x", reader, item("x"), headers=h)[0] == 403


def test_a_tenant_token_naming_another_tenant_is_refused_never_repointed(served):
    srv, base = served, served.base
    a = tenant(srv, "org-core")
    tenant(srv, "lucille")
    assert call(base, "GET", "/admin/roadmap", a, headers=_t("org-core"))[0] == 200
    assert call(base, "GET", "/admin/roadmap", a, headers=_t("lucille"))[0] == 401
    assert call(base, "GET", "/admin/roadmap", a)[0] == 200    # unchanged without the header


def test_revoking_disabling_and_ungranting_each_close_the_door(served):
    srv, base = served, served.base
    tok = _person(srv)
    h = {"X-Roadmap-Tenant": "lucille"}
    assert call(base, "GET", "/admin/roadmap", tok, headers=h)[0] == 200
    srv.registry.revoke_grant("gal", "lucille")
    assert call(base, "GET", "/admin/roadmap", tok, headers=h)[0] == 401
    assert call(base, "GET", "/admin/roadmap", tok, headers=_t("org-core"))[0] == 200
    srv.registry.disable_tenant("org-core")
    assert call(base, "GET", "/admin/roadmap", tok, headers=_t("org-core"))[0] == 401
    srv.registry.enable_tenant("org-core")
    srv.registry.disable_user("gal")
    assert call(base, "GET", "/admin/roadmap", tok, headers=_t("org-core"))[0] == 401
    srv.registry.enable_user("gal")
    tid = [t["id"] for t in srv.registry.tokens() if t["user"] == "gal"][0]
    assert tid.startswith("utk_") and srv.registry.revoke_token(tid)
    assert call(base, "GET", "/admin/roadmap", tok, headers=_t("org-core"))[0] == 401


def test_the_cli_names_its_tenant_from_the_environment(served, monkeypatch):
    from roadmap_core import cli
    srv, base = served, served.base
    tok = _person(srv)
    call(base, "PUT", "/admin/roadmap/lucille-item", tok, item("lucille-item"),
         headers=_t("lucille"))
    monkeypatch.setenv("ROADMAP_API_URL", base)
    monkeypatch.setenv("ROADMAP_API_TOKEN", tok)
    monkeypatch.setenv("ROADMAP_TENANT", "lucille")
    assert {i["key"] for i in cli._api("GET", "/admin/roadmap")["items"]} == {"lucille-item"}
    monkeypatch.delenv("ROADMAP_TENANT")
    with pytest.raises(SystemExit, match="set ROADMAP_TENANT"):
        cli._api("GET", "/admin/roadmap")


def test_users_and_grants_are_managed_on_the_host(tmp_path, capsys):
    from roadmap_core import cli
    data = str(tmp_path / "d")
    for argv in (["tenant", "add", "org-core"], ["user", "add", "gal", "--label", "gal (operator)"],
                 ["user", "grant", "gal", "--tenant", "org-core", "--scopes", "read"]):
        assert cli.main(["serve", "--data", data, *argv]) == 0
    argv = ["serve", "--data", data, "token", "create", "--user", "gal", "--label", "g"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "for user gal" in out and out.strip().splitlines()[-1].startswith("rmk_")
    assert cli.main(["serve", "--data", data, "user", "list"]) == 0
    assert "org-core:read" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["serve", "--data", data, "user", "grant", "gal", "--tenant", "nope"])
