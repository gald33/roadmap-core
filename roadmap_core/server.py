"""A roadmap server: many tenants, one process, the standard library only.

**Why it exists.** ``LocalStore`` is "the roadmap in one SQLite file, with no
server anywhere", and that file is checkout-local by design: CI deletes it,
``.gitignore`` keeps it out of git, and every cloud session runs in its own
container, so every session holds its own copy. A status one session writes is
invisible to the next until a commit carries it, and a commit to ``main`` is a
pull request — the slowest clock an org has. Claims move faster than pull
requests. This module is the one place they can all write: the same SQLite
store, behind the ``/admin/roadmap`` API that ``ApiStore`` and the CLI's ``db``
source already speak, so a client changes nothing but a URL and a token.

**Many tenants, one file each.** A tenant is an id, and its roadmap is its own
SQLite file, ``<data>/tenants/<id>.db``. Isolation is a file boundary, not a
``WHERE`` clause, so no query this module gets wrong can read another tenant's
rows. Which tenant a request is for is decided by its token: a *tenant token*
is bound to one tenant, and the URL carries no tenant, so there is no second
place to disagree with the first.

**People, not only tenants (0.5.0).** A *user token* names a person, not a
tenant: one secret for everyone who works across several orgs, where one cloud
environment is shared by all of them and a tenant token per org cannot be kept
apart (the operator, 2026-09-26: "we don't need token per org. we need token per
user … that should be resolved from the token"). A user holds grants, a scope
set per tenant, and a request made with a user token names its tenant in
``X-Roadmap-Tenant`` — which the server only honours inside the user's grants:
a tenant the user holds no grant on, a disabled one, or none named answer as a
bad token would. The effective scopes are the token's cap intersected with the
grant. A tenant token that also sends the header must name its own tenant, so the
confused-deputy shape of "token for A, header naming B" is refused, not obeyed.

**The threat model, in the order the handler enforces it.**

* *Transport.* TLS 1.2 or later (``tls_cert``/``tls_key``), or a loopback bind
  for a reverse proxy on the same host. Plaintext on any other interface is
  refused unless ``allow_plaintext`` says so out loud. The TLS handshake runs in
  the connection's own thread under the socket timeout, so a client that stalls
  mid-handshake holds one worker, not the accept loop.
* *Authentication.* ``Authorization: Bearer <token>``. A token is 256 random
  bits, shown once when it is created and stored only as its SHA-256, so a
  copied registry holds no usable credential. An unknown token, a revoked or
  expired one and a disabled tenant all get the same 401: a probe learns
  nothing about which.
* *Authorization.* Scopes per token — ``read`` (every GET), ``write`` (upsert,
  status, claim, release) and ``admin`` (prune, refile, deleting an arc, and a
  *forced* claim, which takes an item from the session holding it).
* *Abuse.* A body cap (413), no chunked bodies (411), a socket timeout, a cap on
  concurrent requests (503), a request budget per token and a budget per
  address for failed authentication (429).
* *Accountability.* Every request lands in the audit log under the token's
  public id — never the token. Every change to an item's status or claim lands
  in the tenant's own transition log, which is also the per-item history the
  store never kept: when an item was claimed, by whom, and when it moved on.
* *Surface.* JSON only: no HTML error pages, no CORS, no Python version in
  ``Server``, ``Cache-Control: no-store``, and a 500 that carries a reference id
  rather than a traceback. Tenants, users, grants and tokens are managed from the
  server's own host (``roadmap serve tenant|user|token``), never over HTTP: whoever can write the
  data directory already owns everything in it, so an HTTP door to the same
  place would only add a second way in.

**Not here, on purpose.** Anything that crosses tenants, a token endpoint, and
``impact``, which needs the host's feedback tickets — ``Unsupported``, as
``LocalStore`` already says, answered 501.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import graph
from .stores import ClaimRefused, LocalStore, Pruned, StoreError

__all__ = [
    "SCOPES",
    "Principal",
    "Registry",
    "RoadmapServer",
    "make_server",
    "tls_context",
]

log = logging.getLogger("roadmap.server")

SCOPES = ("read", "write", "admin")
DEFAULT_SCOPES = ("read", "write")

#: A tenant id names a file, so it may hold nothing a path could be built from:
#: no dot, no slash, no case to fold. Lower-case, digits and hyphens, 63 at most.
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
#: An item or arc key. The rule ``cli._ID_RE`` applies to the file an item lives
#: in, restated here so a request cannot address a key no file could hold; the
#: two are pinned together by ``tests/test_server.py``.
KEY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_KEY_LEN = 200
#: Path words the API uses for itself. An item named ``arcs`` would be reachable
#: for PUT and shadowed for GET, so the words are refused as item keys outright.
RESERVED_KEYS = frozenset({"arcs", "prunes", "ready", "transitions"})

TOKEN_PREFIX = "rmk_"
#: the header a user token's request names its tenant in (0.5.0); a tenant token may send
#: it, naming its own
TENANT_HEADER = "X-Roadmap-Tenant"
MAX_TOKEN_LEN = 128
MAX_LABEL_LEN = 80
MAX_BODY_BYTES = 1 << 20          # an item's evidence is prose; a megabyte is generous
SOCKET_TIMEOUT_S = 15.0
MAX_CONCURRENT = 64
#: Open connections, idle ones included. The request cap above counts only
#: requests being handled, so without this a client that opens sockets and sends
#: nothing holds a thread each until the socket timeout.
MAX_CONNECTIONS = 256
#: (refill per second, burst). A session reading the graph and claiming an item
#: makes a handful of calls; a `push` of a large backlog makes one per item.
TOKEN_BUDGET = (20.0, 200)
#: Failed authentications per address: twenty, then one every five seconds.
AUTH_FAIL_BUDGET = (0.2, 20)
#: `last_used_at` is written at most this often per token, so a busy token does
#: not turn every read into a registry write.
LAST_USED_EVERY_S = 60.0
MAX_TRANSITIONS_PAGE = 5000

REGISTRY_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS tenants (
        id          TEXT PRIMARY KEY,
        created_at  TEXT NOT NULL,
        disabled_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tokens (
        id           TEXT PRIMARY KEY,
        tenant       TEXT NOT NULL REFERENCES tenants (id),
        hash         TEXT NOT NULL UNIQUE,
        scopes       TEXT NOT NULL,
        label        TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        expires_at   TEXT,
        revoked_at   TEXT,
        last_used_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id          TEXT PRIMARY KEY,
        label       TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        disabled_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS grants (
        user        TEXT NOT NULL REFERENCES users (id),
        tenant      TEXT NOT NULL REFERENCES tenants (id),
        scopes      TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (user, tenant)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_tokens (
        id           TEXT PRIMARY KEY,
        user         TEXT NOT NULL REFERENCES users (id),
        hash         TEXT NOT NULL UNIQUE,
        scopes       TEXT NOT NULL,
        label        TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        expires_at   TEXT,
        revoked_at   TEXT,
        last_used_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        at       TEXT NOT NULL,
        tenant   TEXT,
        token_id TEXT,
        remote   TEXT,
        method   TEXT NOT NULL,
        path     TEXT NOT NULL,
        status   INTEGER NOT NULL,
        detail   TEXT
    )
    """,
)

#: The tenant's own history of status and claim changes. Server-owned DDL rather
#: than a fourth table in ``store.SCHEMA``, which is held to the Postgres models
#: by a parity test and has no counterpart there.
TRANSITIONS_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS roadmap_transitions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        key         TEXT NOT NULL,
        op          TEXT NOT NULL,
        from_status TEXT,
        to_status   TEXT,
        claimed_by  TEXT,
        actor       TEXT NOT NULL,
        at          TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_roadmap_transitions_key ON roadmap_transitions (key, id)",
)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(moment: _dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    try:
        moment = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=_dt.timezone.utc)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _scopes(scopes: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    wanted = tuple(dict.fromkeys(s.strip() for s in scopes if s.strip()))
    unknown = [s for s in wanted if s not in SCOPES]
    if unknown or not wanted:
        raise RegistryError(f"scopes must be some of {', '.join(SCOPES)}; got {list(scopes)}")
    return wanted


def _label(label: str) -> str:
    label = (label or "").strip()
    if not label or len(label) > MAX_LABEL_LEN or not label.isprintable():
        raise RegistryError(f"a label is 1-{MAX_LABEL_LEN} printable characters — "
                            "say who holds the token")
    return label


def _private(path: Path) -> None:
    """Owner-only, for the registry, every tenant file and SQLite's WAL beside it."""
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            os.chmod(p, 0o600)
        except FileNotFoundError:
            pass


# --- the registry ------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Who a request is: one token and the tenant it acts in, with its scopes — for a user
    token, the tenant the request named and the scopes its grant and the token both allow."""

    tenant: str
    token_id: str
    scopes: frozenset[str]
    label: str
    user: str | None = None

    @property
    def actor(self) -> str:
        return f"{self.label} ({self.token_id})"


class RegistryError(ValueError):
    """A management command was refused, with the reason to show the operator."""


class Registry:
    """Tenants, their tokens and the audit log, in ``<data>/registry.db``.

    Every method opens and closes its own connection. The server is threaded and
    a ``sqlite3`` connection is not shareable across threads, and a connection per
    call is cheap next to the network round trip that caused it.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tenants_dir = self.root / "tenants"
        self.tenants_dir.mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / "registry.db"
        with closing(self._connect()) as conn:
            for ddl in REGISTRY_SCHEMA:
                conn.execute(ddl)
        _private(self.path)
        self._last_used: dict[str, float] = {}
        self._last_used_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # -- tenants

    def add_tenant(self, tenant: str) -> None:
        if not TENANT_RE.match(tenant or ""):
            raise RegistryError(
                f"{tenant!r} is not a tenant id: lower-case letters, digits and hyphens, "
                "starting with a letter or digit, 63 at most"
            )
        with closing(self._connect()) as conn:
            try:
                conn.execute("INSERT INTO tenants (id, created_at) VALUES (?, ?)",
                             (tenant, _iso(_now())))
            except sqlite3.IntegrityError as exc:
                raise RegistryError(f"tenant {tenant!r} already exists") from exc
        with self.open_tenant(tenant):
            pass

    def tenants(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM tenants ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def disable_tenant(self, tenant: str) -> bool:
        """Every token of a disabled tenant answers 401 from the next request on.
        Its file is kept: disabling is reversible by an operator, deleting is not."""
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "UPDATE tenants SET disabled_at = ? WHERE id = ? AND disabled_at IS NULL",
                (_iso(_now()), tenant),
            )
        return cur.rowcount > 0

    def enable_tenant(self, tenant: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute("UPDATE tenants SET disabled_at = NULL WHERE id = ?", (tenant,))
        return cur.rowcount > 0

    def tenant_path(self, tenant: str) -> Path:
        if not TENANT_RE.match(tenant or ""):
            raise RegistryError(f"{tenant!r} is not a tenant id")
        path = (self.tenants_dir / f"{tenant}.db").resolve()
        if path.parent != self.tenants_dir.resolve():
            raise RegistryError(f"{tenant!r} resolves outside the tenants directory")
        return path

    def open_tenant(self, tenant: str) -> LocalStore:
        """The tenant's store, with the transition log beside its three tables."""
        path = self.tenant_path(tenant)
        local = LocalStore(path)
        for ddl in TRANSITIONS_SCHEMA:
            local.connection.execute(ddl)
        _private(path)
        return local

    # -- users and their grants (0.5.0)

    def add_user(self, user: str, *, label: str) -> None:
        if not TENANT_RE.match(user or ""):
            raise RegistryError(f"{user!r} is not a user id: lower-case letters, digits and "
                                "hyphens, "
                                "1-63 characters, starting with a letter or digit")
        label = _label(label)
        with closing(self._connect()) as conn:
            try:
                conn.execute("INSERT INTO users (id, label, created_at) VALUES (?, ?, ?)",
                             (user, label, _iso(_now())))
            except sqlite3.IntegrityError as exc:
                raise RegistryError(f"user {user!r} already exists") from exc

    def users(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            users = [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY id").fetchall()]
            grants = conn.execute(
                "SELECT user, tenant, scopes FROM grants ORDER BY user, tenant").fetchall()
        for u in users:
            u["grants"] = {g["tenant"]: json.loads(g["scopes"])
                           for g in grants if g["user"] == u["id"]}
        return users

    def disable_user(self, user: str) -> bool:
        """Every token of a disabled user answers 401 from the next request on."""
        with closing(self._connect()) as conn:
            cur = conn.execute("UPDATE users SET disabled_at = ?"
                               " WHERE id = ? AND disabled_at IS NULL",
                               (_iso(_now()), user))
        return cur.rowcount > 0

    def enable_user(self, user: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute("UPDATE users SET disabled_at = NULL WHERE id = ?", (user,))
        return cur.rowcount > 0

    def grant(self, user: str, tenant: str,
              scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES) -> None:
        """Give ``user`` ``scopes`` on ``tenant``; a second grant there replaces the first."""
        wanted = _scopes(scopes)
        with closing(self._connect()) as conn:
            if conn.execute("SELECT 1 FROM users WHERE id = ?", (user,)).fetchone() is None:
                raise RegistryError(f"no user {user!r} — add it first")
            if conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant,)).fetchone() is None:
                raise RegistryError(f"no tenant {tenant!r} — add it first")
            conn.execute("INSERT INTO grants (user, tenant, scopes, created_at) VALUES (?, ?, ?, ?)"
                         " ON CONFLICT (user, tenant) DO UPDATE SET scopes = excluded.scopes",
                         (user, tenant, json.dumps(list(wanted)), _iso(_now())))

    def revoke_grant(self, user: str, tenant: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM grants WHERE user = ? AND tenant = ?", (user, tenant))
        return cur.rowcount > 0

    def create_user_token(self, user: str, *, label: str,
                          scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
                          expires_days: float | None = None) -> tuple[str, str]:
        """``(token_id, secret)`` for a person. ``scopes`` caps what any grant allows through
        this token — a ``read`` user token reads every tenant its user holds a grant on, and
        writes none."""
        wanted = _scopes(scopes)
        label = _label(label)
        if expires_days is not None and expires_days <= 0:
            raise RegistryError("expires_days must be positive, or omitted for no expiry")
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT disabled_at FROM users WHERE id = ?", (user,)).fetchone()
            if row is None:
                raise RegistryError(f"no user {user!r} — add it first")
            if row["disabled_at"]:
                raise RegistryError(f"user {user!r} is disabled")
            secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
            token_id = "utk_" + secrets.token_hex(6)
            now = _now()
            expires = _iso(now + _dt.timedelta(days=expires_days)) if expires_days else None
            conn.execute(
                "INSERT INTO user_tokens (id, user, hash, scopes, label, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_id, user, _hash(secret), json.dumps(list(wanted)), label, _iso(now),
                 expires),
            )
        return token_id, secret

    # -- tokens

    def create_token(
        self,
        tenant: str,
        *,
        label: str,
        scopes: tuple[str, ...] | list[str] = DEFAULT_SCOPES,
        expires_days: float | None = None,
    ) -> tuple[str, str]:
        """``(token_id, secret)``. The secret is returned once and never stored."""
        wanted = _scopes(scopes)
        label = _label(label)
        if expires_days is not None and expires_days <= 0:
            raise RegistryError("expires_days must be positive, or omitted for no expiry")
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT disabled_at FROM tenants WHERE id = ?", (tenant,)).fetchone()
            if row is None:
                raise RegistryError(f"no tenant {tenant!r} — add it first")
            if row["disabled_at"]:
                raise RegistryError(f"tenant {tenant!r} is disabled")
            secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
            token_id = "tok_" + secrets.token_hex(6)
            now = _now()
            expires = _iso(now + _dt.timedelta(days=expires_days)) if expires_days else None
            conn.execute(
                "INSERT INTO tokens (id, tenant, hash, scopes, label, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_id, tenant, _hash(secret), json.dumps(list(wanted)), label,
                 _iso(now), expires),
            )
        return token_id, secret

    def tokens(self, tenant: str | None = None) -> list[dict[str, Any]]:
        """Every token's public face — never its hash, which is only ever compared."""
        sql = ("SELECT id, tenant, scopes, label, created_at, expires_at, revoked_at,"
               " last_used_at FROM tokens")
        args: tuple[Any, ...] = ()
        if tenant:
            sql += " WHERE tenant = ?"
            args = (tenant,)
        with closing(self._connect()) as conn:
            rows = conn.execute(sql + " ORDER BY tenant, created_at", args).fetchall()
            out = [dict(r, user=None, scopes=json.loads(r["scopes"])) for r in rows]
            if not tenant:
                urows = conn.execute(
                    "SELECT id, user, scopes, label, created_at, expires_at, revoked_at,"
                    " last_used_at FROM user_tokens ORDER BY user, created_at").fetchall()
                out += [dict(r, tenant=None, scopes=json.loads(r["scopes"])) for r in urows]
        return out

    def revoke_token(self, token_id: str) -> bool:
        table = "user_tokens" if str(token_id).startswith("utk_") else "tokens"
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"UPDATE {table} SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_iso(_now()), token_id),
            )
        return cur.rowcount > 0

    def authenticate(self, secret: str, tenant: str | None = None) -> Principal | None:
        """The principal a bearer token names, acting in ``tenant`` when the request named one
        (``X-Roadmap-Tenant``), or ``None`` for every kind of no — a user token with no tenant
        named, or one its user holds no grant on, or a tenant token naming a tenant other than
        its own, included.

        Looked up by its hash, then compared with ``hmac.compare_digest``: the
        lookup is on a value the caller cannot choose (a SHA-256 of 256 random
        bits), and the comparison keeps the equality check itself constant-time.
        """
        if not secret or len(secret) > MAX_TOKEN_LEN or not secret.startswith(TOKEN_PREFIX):
            return None
        if tenant is not None and not TENANT_RE.match(tenant):
            return None
        digest = _hash(secret)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT t.*, n.disabled_at AS tenant_disabled_at FROM tokens t"
                " JOIN tenants n ON n.id = t.tenant WHERE t.hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return self._authenticate_user(conn, digest, tenant)
            if not hmac.compare_digest(row["hash"], digest):
                return None
            if tenant is not None and tenant != row["tenant"]:
                return None   # a tenant token naming another tenant: refused, never re-pointed
            if row["revoked_at"] or row["tenant_disabled_at"]:
                return None
            expires = _parse(row["expires_at"])
            if expires is not None and expires <= _now():
                return None
            if self._should_touch(row["id"]):
                conn.execute("UPDATE tokens SET last_used_at = ? WHERE id = ?",
                             (_iso(_now()), row["id"]))
        return Principal(tenant=row["tenant"], token_id=row["id"],
                         scopes=frozenset(json.loads(row["scopes"])), label=row["label"])

    def _authenticate_user(self, conn: sqlite3.Connection, digest: str,
                           tenant: str | None) -> Principal | None:
        row = conn.execute(
            "SELECT k.*, u.disabled_at AS user_disabled_at FROM user_tokens k"
            " JOIN users u ON u.id = k.user WHERE k.hash = ?",
            (digest,),
        ).fetchone()
        if row is None or not hmac.compare_digest(row["hash"], digest):
            return None
        if row["revoked_at"] or row["user_disabled_at"] or tenant is None:
            return None
        expires = _parse(row["expires_at"])
        if expires is not None and expires <= _now():
            return None
        grant = conn.execute(
            "SELECT g.scopes FROM grants g JOIN tenants n ON n.id = g.tenant"
            " WHERE g.user = ? AND g.tenant = ? AND n.disabled_at IS NULL",
            (row["user"], tenant),
        ).fetchone()
        if grant is None:
            return None
        scopes = frozenset(json.loads(row["scopes"])) & frozenset(json.loads(grant["scopes"]))
        if not scopes:
            return None
        if self._should_touch(row["id"]):
            conn.execute("UPDATE user_tokens SET last_used_at = ? WHERE id = ?",
                         (_iso(_now()), row["id"]))
        return Principal(tenant=tenant, token_id=row["id"], scopes=scopes, label=row["label"],
                         user=row["user"])

    def _should_touch(self, token_id: str) -> bool:
        now = time.monotonic()
        with self._last_used_lock:
            if now - self._last_used.get(token_id, -LAST_USED_EVERY_S) < LAST_USED_EVERY_S:
                return False
            self._last_used[token_id] = now
            return True

    # -- audit

    def audit(self, *, principal: Principal | None, remote: str, method: str, path: str,
              status: int, detail: str = "") -> None:
        try:
            with closing(self._connect()) as conn:
                conn.execute(
                    "INSERT INTO audit (at, tenant, token_id, remote, method, path, status, detail)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_iso(_now()), principal.tenant if principal else None,
                     principal.token_id if principal else None, remote, method[:10],
                     path[:500], status, detail[:500]),
                )
        except sqlite3.Error:
            # Never fail a request over its own log line — but say so, loudly,
            # because an audit log that silently stops is worse than none.
            log.exception("audit write failed")

    def audit_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# --- budgets -----------------------------------------------------------------


class _Budget:
    """Token buckets by key, in memory. A restart refills them, which is the
    right way to fail: a limiter that outlived a restart would be state to lose."""

    MAX_KEYS = 10_000

    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.burst = rate, float(burst)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> float:
        """0.0 when the request may proceed, else the seconds until it may."""
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) > self.MAX_KEYS:
                self._buckets.clear()
            level, at = self._buckets.get(key, (self.burst, now))
            level = min(self.burst, level + (now - at) * self.rate)
            if level < 1.0:
                self._buckets[key] = (level, now)
                return (1.0 - level) / self.rate
            self._buckets[key] = (level - 1.0, now)
            return 0.0


# --- the API -----------------------------------------------------------------


class _HttpError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class _Request:
    store: LocalStore
    principal: Principal
    key: str | None
    body: dict[str, Any]
    query: dict[str, list[str]]


def _valid_key(key: str) -> str:
    """Every key a route takes, before any handler sees it. The reserved words
    are refused on every verb, not only PUT: `DELETE /admin/roadmap/prunes`
    matches the item route, and would otherwise tombstone a key called `prunes`."""
    if len(key) > MAX_KEY_LEN or not KEY_RE.match(key):
        raise _HttpError(400, f"{key[:60]!r} is not a key: lower-case words joined by hyphens")
    if key in RESERVED_KEYS:
        raise _HttpError(400, f"{key!r} is a word the API uses for itself — choose another key")
    return key


def _decorate(item: dict[str, Any], by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One item plus what only the whole graph can say about it — the shape the
    served API has always returned, so ``list --source db`` reads it unchanged:
    the status *derived*, the unmet dependencies, what it blocks, its relations
    from both sides and its artifact contention."""
    row = dict(item)
    row["status"] = graph.derive_status(item, by_key)
    row["unmet_deps"] = graph.unmet_deps(item, by_key)
    row["blocks"] = sorted(k for k, other in by_key.items()
                           if item["key"] in (other.get("blocked_on") or []))
    row["related_to_merged"] = graph.relations_for(item["key"], by_key)
    row["artifact_contention"] = graph.artifact_contention(item["key"], by_key)
    return row


def _record(req: _Request, key: str, op: str, before: dict[str, Any] | None,
            after: dict[str, Any] | None) -> None:
    req.store.connection.execute(
        "INSERT INTO roadmap_transitions (key, op, from_status, to_status, claimed_by, actor, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (key, op, (before or {}).get("status"), (after or {}).get("status"),
         (after or {}).get("claimed_by"), req.principal.actor, _iso(_now())),
    )


def _item_or_404(req: _Request) -> dict[str, Any]:
    item = req.store.get_item(req.key or "")
    if item is None:
        raise _HttpError(404, f"no such item: {req.key}")
    return item


def _list_items(req: _Request) -> tuple[int, Any]:
    by_key = req.store.items()
    return 200, {"items": [_decorate(by_key[k], by_key) for k in sorted(by_key)],
                 "problems": graph.validate_graph(by_key)}


def _ready(req: _Request) -> tuple[int, Any]:
    by_key = req.store.items()
    ready = graph.ready_items(by_key)
    return 200, {"items": [_decorate(i, by_key) for i in ready], "total_ready": len(ready),
                 "problems": graph.validate_graph(by_key),
                 "stale_claims": graph.stale_claims(by_key)}


def _list_arcs(req: _Request) -> tuple[int, Any]:
    arcs = req.store.arcs()
    return 200, {"arcs": [arcs[k] for k in sorted(arcs)]}


def _put_arc(req: _Request) -> tuple[int, Any]:
    key = req.key or ""
    body = req.body
    if body.get("key", key) != key:
        raise _HttpError(400, f"key mismatch: path {key!r} vs body {body.get('key')!r}")
    for name in ("title", "state", "state_evidence", "narrative"):
        if body.get(name) is not None and not isinstance(body[name], str):
            raise _HttpError(400, f"{name} must be a string")
    if not isinstance(body.get("refs") or [], list):
        raise _HttpError(400, "refs must be a list")
    return 200, req.store.upsert_arc({**body, "key": key})


def _delete_arc(req: _Request) -> tuple[int, Any]:
    cur = req.store.connection.execute("DELETE FROM roadmap_arcs WHERE key = ?", (req.key,))
    if cur.rowcount == 0:
        raise _HttpError(404, f"no such arc: {req.key}")
    return 200, {"deleted": req.key}


def _list_prunes(req: _Request) -> tuple[int, Any]:
    rows = req.store.connection.execute(
        "SELECT key, pruned_at FROM roadmap_prunes ORDER BY key").fetchall()
    return 200, {"prunes": [{"key": r["key"], "pruned_at": r["pruned_at"]} for r in rows],
                 "total": len(rows)}


_ITEM_STRINGS = ("title", "arc", "priority", "defer_reason", "evidence", "evidence_checked_at")
_ITEM_LISTS = ("blocked_on", "related_to", "artifacts", "refs", "tickets")


def _put_item(req: _Request) -> tuple[int, Any]:
    """Create or update an item from its authored form. ``status`` and the claim
    seed a new item and are ignored for an existing one — ``LocalStore``'s rule,
    and the served path's — so a re-push never moves or drops another session's
    work. Deliberate moves go through ``/status`` and ``/claim``."""
    key = req.key or ""
    body = req.body
    if body.get("key") != key:
        raise _HttpError(400, f"key mismatch: path {key!r} vs body {body.get('key')!r}")
    for name in _ITEM_STRINGS:
        if body.get(name) is not None and not isinstance(body[name], str):
            raise _HttpError(400, f"{name} must be a string or null")
    for name in _ITEM_LISTS:
        if not isinstance(body.get(name) or [], list):
            raise _HttpError(400, f"{name} must be a list")
    if any(not isinstance(d, str) for d in body.get("blocked_on") or []):
        raise _HttpError(400, "blocked_on holds item keys")
    status = body.get("status") or "ready"
    if status not in graph.STATUSES:
        raise _HttpError(400, f"status must be one of {', '.join(graph.STATUSES)}")
    priority = (body.get("priority") or "").strip().lower()
    if priority and priority not in graph.PRIORITIES:
        raise _HttpError(400, f"priority must be one of {', '.join(graph.PRIORITIES)}, or null")
    before = req.store.get_item(key)
    try:
        # the claim is dropped here: a served store outlives the checkout that
        # pushes to it, so honouring a file's claim would resurrect one the store
        # already released (`LocalStore`, "claim on the floor")
        clean = {k: v for k, v in body.items() if k not in ("claimed_by", "claimed_at")}
        item = req.store.upsert_item({**clean, "status": status})
    except Pruned as exc:
        raise _HttpError(409, str(exc)) from exc
    if before is None:
        _record(req, key, "create", None, item)
    return 200, item


def _set_status(req: _Request) -> tuple[int, Any]:
    status = req.body.get("status")
    if status not in graph.STATUSES:
        raise _HttpError(400, f"status must be one of {', '.join(graph.STATUSES)}")
    before = _item_or_404(req)
    try:
        item = req.store.set_status(req.key or "", status)
    except StoreError as exc:
        raise _HttpError(404, str(exc)) from exc
    _record(req, req.key or "", "status", before, item)
    return 200, item


def _claim(req: _Request) -> tuple[int, Any]:
    by = req.body.get("by")
    if not isinstance(by, str) or not by.strip() or len(by) > 200 or not by.isprintable():
        raise _HttpError(400, "by names who holds the item: 1-200 printable characters")
    force = req.body.get("force", False)
    if not isinstance(force, bool):
        raise _HttpError(400, "force is true or false")
    if force and "admin" not in req.principal.scopes:
        raise _HttpError(403, "a forced claim takes an item from the session holding it — "
                              "that needs the admin scope")
    before = _item_or_404(req)
    try:
        item = req.store.claim(req.key or "", by=by.strip(), force=force)
    except ClaimRefused as exc:
        raise _HttpError(404 if str(exc).startswith("no such item") else 409, str(exc)) from exc
    _record(req, req.key or "", "claim-forced" if force else "claim", before, item)
    return 200, item


def _release(req: _Request) -> tuple[int, Any]:
    before = _item_or_404(req)
    item = req.store.release(req.key or "")
    _record(req, req.key or "", "release", before, item)
    return 200, item


def _prune(req: _Request) -> tuple[int, Any]:
    """Delete an item and tombstone its key — even when there is no row. The
    served path's rule: a prune that partly failed is re-run, and the second run
    must still record the intent, or the key that most needs protecting from a
    stale push is the one left without it."""
    key = req.key or ""
    before = req.store.get_item(key)
    deleted = req.store.delete_item(key)
    if not deleted:
        req.store.connection.execute(
            "INSERT OR IGNORE INTO roadmap_prunes (id, key, pruned_at) VALUES (?, ?, ?)",
            (secrets.token_hex(16), key, _iso(_now())),
        )
    _record(req, key, "prune", before, None)
    if not deleted:
        raise _HttpError(404, f"no such item: {key} (its key is tombstoned all the same)")
    return 200, {"deleted": True}


def _refile(req: _Request) -> tuple[int, Any]:
    if not req.store.clear_prune(req.key or ""):
        raise _HttpError(404, f"{req.key!r} carries no prune tombstone — nothing to refile")
    _record(req, req.key or "", "refile", None, None)
    return 200, {"refiled": True}


def _impact(req: _Request) -> tuple[int, Any]:
    raise _HttpError(501, "impact needs the host's feedback tickets, which this server does "
                          "not hold — ask the host's own API for it")


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


def _history(req: _Request) -> tuple[int, Any]:
    rows = req.store.connection.execute(
        "SELECT * FROM roadmap_transitions WHERE key = ? ORDER BY id", (req.key,)).fetchall()
    if not rows and req.store.get_item(req.key or "") is None:
        raise _HttpError(404, f"no such item: {req.key}")
    return 200, {"key": req.key, "transitions": [_row(r) for r in rows]}


def _transitions(req: _Request) -> tuple[int, Any]:
    """Every transition after a cursor (``after``, an id) or a moment (``since``,
    ISO 8601), oldest first, a page at a time. The id is the cursor a poller
    should keep: two transitions can share a second, never an id."""
    try:
        after = int((req.query.get("after") or ["0"])[0])
        limit = min(MAX_TRANSITIONS_PAGE, int((req.query.get("limit") or ["1000"])[0]))
    except ValueError as exc:
        raise _HttpError(400, "after and limit are integers") from exc
    since = (req.query.get("since") or [""])[0]
    if since and _parse(since) is None:
        raise _HttpError(400, "since is an ISO 8601 moment")
    sql, args = "SELECT * FROM roadmap_transitions WHERE id > ?", [after]
    if since:
        sql += " AND at > ?"
        # stored `at` is UTC ISO text, so compare UTC ISO text: an offset left in
        # would make the string comparison wrong by the offset
        args.append(_iso(_parse(since).astimezone(_dt.timezone.utc)))  # type: ignore[union-attr]
    rows = req.store.connection.execute(sql + " ORDER BY id LIMIT ?", (*args, max(1, limit)))
    out = [_row(r) for r in rows.fetchall()]
    return 200, {"transitions": out, "next": out[-1]["id"] if out else after}


_Handler_ = Callable[[_Request], "tuple[int, Any]"]
_K = r"(?P<key>[^/]+)"
#: (method, path, scope, handler). Specific paths first: an item key may not be
#: one of `RESERVED_KEYS`, so `/admin/roadmap/arcs` is never an item.
ROUTES: tuple[tuple[str, re.Pattern[str], str, _Handler_], ...] = tuple(
    (m, re.compile(p), s, h) for m, p, s, h in (
        ("GET", r"/admin/roadmap", "read", _list_items),
        ("GET", r"/admin/roadmap/ready", "read", _ready),
        ("GET", r"/admin/roadmap/arcs", "read", _list_arcs),
        ("GET", r"/admin/roadmap/prunes", "read", _list_prunes),
        ("GET", r"/admin/roadmap/transitions", "read", _transitions),
        ("PUT", rf"/admin/roadmap/arcs/{_K}", "write", _put_arc),
        ("DELETE", rf"/admin/roadmap/arcs/{_K}", "admin", _delete_arc),
        ("GET", rf"/admin/roadmap/{_K}/history", "read", _history),
        ("GET", rf"/admin/roadmap/{_K}/impact", "read", _impact),
        ("POST", rf"/admin/roadmap/{_K}/status", "write", _set_status),
        ("POST", rf"/admin/roadmap/{_K}/claim", "write", _claim),
        ("POST", rf"/admin/roadmap/{_K}/release", "write", _release),
        ("POST", rf"/admin/roadmap/{_K}/refile", "admin", _refile),
        ("PUT", rf"/admin/roadmap/{_K}", "write", _put_item),
        ("DELETE", rf"/admin/roadmap/{_K}", "admin", _prune),
    )
)


class _Handler(BaseHTTPRequestHandler):
    server: RoadmapServer
    server_version = "roadmap"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_S

    def version_string(self) -> str:
        return self.server_version

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        self._handle("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base's name
        # The request line and status only. Headers — the bearer token — never.
        # Escaped, so a request line cannot forge a log line of its own.
        line = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in format % args)
        log.info("%s %s", self.client_address[0], line)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """JSON, never the base class's HTML page — including for the errors it
        raises before a request is parsed (a bad request line, an unknown verb)."""
        self.close_connection = True
        self._send(code, {"detail": message or HTTPStatus(code).phrase})

    def _send(self, code: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, separators=(",", ":"), default=str).encode()
        self._status = code
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self.server, "tls", False):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _handle(self, method: str) -> None:
        """Route, then audit, then answer — in that order, so the audit row exists
        before the client can see the response it records."""
        self._status = 0
        self._principal: Principal | None = None
        path = urlsplit(self.path).path
        if not self.server.slots.acquire(blocking=False):
            self._finish(method, path, 503, {"detail": "busy — retry shortly"},
                         {"Retry-After": "1"})
            return
        try:
            try:
                code, payload, headers = self._route(method, path)
            except _HttpError as exc:
                code, payload, headers = exc.status, {"detail": exc.detail}, {}
                if isinstance(exc, _RetryAfter):
                    headers["Retry-After"] = str(exc.seconds)
                if isinstance(exc, _Unauthorized):
                    headers["WWW-Authenticate"] = 'Bearer realm="roadmap"'
            except Exception:  # noqa: BLE001 - the one place a fault becomes a response
                ref = secrets.token_hex(4)
                log.exception("unhandled error ref=%s %s %s", ref, method, path)
                code, payload, headers = 500, {"detail": "internal error", "ref": ref}, {}
            self._finish(method, path, code, payload, headers)
        finally:
            self.server.slots.release()

    def _finish(self, method: str, path: str, code: int, payload: Any,
                headers: dict[str, str]) -> None:
        if path != "/healthz":
            self.server.registry.audit(principal=self._principal, remote=self.client_address[0],
                                       method=method, path=path, status=code)
        self._send(code, payload, headers)

    def _route(self, method: str, path: str) -> tuple[int, Any, dict[str, str]]:
        if path == "/healthz" and method == "GET":
            return 200, {"ok": True}, {}
        principal = self._principal = self._authenticate()
        wait = self.server.request_budget.take(principal.token_id)
        if wait:
            raise _RetryAfter(wait)
        matched_path, found = False, None
        for verb, pattern, scope, handler in ROUTES:
            m = pattern.fullmatch(path)
            if m is None:
                continue
            matched_path = True
            if verb == method:
                found = (scope, handler, m.groupdict().get("key"))
                break
        if found is None:
            raise _HttpError(405 if matched_path else 404,
                             "method not allowed here" if matched_path else "no such endpoint")
        scope, handler, key = found
        if scope not in principal.scopes:
            raise _HttpError(403, f"this token lacks the {scope} scope")
        if key is not None:
            _valid_key(key)
        body = self._read_body() if method in ("PUT", "POST") else {}
        try:
            query = parse_qs(urlsplit(self.path).query, max_num_fields=20)
        except ValueError as exc:
            raise _HttpError(400, "too many query parameters") from exc
        with self.server.registry.open_tenant(principal.tenant) as local:
            code, payload = handler(_Request(local, principal, key, body, query))
        return code, payload, {}

    def _authenticate(self) -> Principal:
        scheme, _, secret = (self.headers.get("Authorization") or "").partition(" ")
        named = (self.headers.get(TENANT_HEADER) or "").strip() or None
        principal = None
        if scheme.lower() == "bearer":
            principal = self.server.registry.authenticate(secret.strip(), named)
        if principal is None:
            wait = self.server.auth_failures.take(self.client_address[0])
            if wait:
                raise _RetryAfter(wait)
            raise _Unauthorized()
        return principal

    def _read_body(self) -> dict[str, Any]:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            raise _HttpError(411, "send the body with a Content-Length, not chunked")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _HttpError(400, "Content-Length is not a number") from exc
        if length < 0:
            raise _HttpError(400, "Content-Length is negative")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise _HttpError(413, f"a body is at most {MAX_BODY_BYTES} bytes")
        if length == 0:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise _HttpError(415, "bodies are application/json")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise _HttpError(400, "the body is not JSON") from exc
        if not isinstance(body, dict):
            raise _HttpError(400, "the body is a JSON object")
        return body


class _RetryAfter(_HttpError):
    def __init__(self, seconds: float) -> None:
        super().__init__(429, "too many requests — slow down")
        self.seconds = max(1, int(seconds + 0.999))


class _Unauthorized(_HttpError):
    def __init__(self) -> None:
        super().__init__(401, "missing or invalid bearer token")



# --- the server --------------------------------------------------------------


class RoadmapServer(ThreadingHTTPServer):
    """One process, many tenants. ``make_server`` is how to build one."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], registry: Registry, *,
                 tls: ssl.SSLContext | None = None) -> None:
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)
        self.registry = registry
        self._tls = tls
        self.tls = tls is not None
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self.connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.request_budget = _Budget(*TOKEN_BUDGET)
        self.auth_failures = _Budget(*AUTH_FAIL_BUDGET)

    def get_request(self) -> tuple[socket.socket, Any]:
        sock, addr = self.socket.accept()
        if self._tls is not None:
            # The handshake is NOT done here: `accept` runs on the one loop every
            # connection shares, and a client that stalls mid-handshake would
            # hold it. It runs in the connection's own thread, below.
            sock = self._tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.connections.acquire(blocking=False):
            log.warning("%s refused: %d connections open", client_address[0], MAX_CONNECTIONS)
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def finish_request(self, request: Any, client_address: Any) -> None:
        request.settimeout(SOCKET_TIMEOUT_S)
        if self._tls is not None:
            try:
                request.do_handshake()
            except (ssl.SSLError, OSError) as exc:
                log.info("%s TLS handshake failed: %s", client_address[0], exc)
                return
        super().finish_request(request, client_address)


def tls_context(cert: str | Path, key: str | Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    return ctx


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(
    data_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    tls_cert: str | Path | None = None,
    tls_key: str | Path | None = None,
    allow_plaintext: bool = False,
) -> RoadmapServer:
    """A server bound and ready for ``serve_forever``. Refuses plaintext on any
    interface but loopback unless ``allow_plaintext`` — a bearer token sent in
    the clear is a token given away."""
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("TLS needs both a certificate and its key")
    if not tls_cert and not allow_plaintext and not _is_loopback(host):
        raise ValueError(
            f"refusing to serve plaintext on {host}: bearer tokens would cross the network "
            "in the clear. Give it --tls-cert and --tls-key, bind 127.0.0.1 behind a TLS "
            "proxy on this host, or pass --allow-plaintext if something else encrypts the wire."
        )
    registry = Registry(data_dir)
    return RoadmapServer((host, port), registry,
                         tls=tls_context(tls_cert, tls_key) if tls_cert else None)


# --- the command line: `roadmap serve …`, run on the server's own host ---------


def _print_rows(rows: list[dict[str, Any]], cols: tuple[str, ...]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c) if r.get(c) is not None else "-")) for r in rows))
              for c in cols}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c) if r.get(c) is not None else "-").ljust(widths[c])
                        for c in cols))


def _cmd_run(args: Any) -> int:
    # Owner-only for every file the process creates from here on: the registry,
    # each tenant's store, and the WAL and SHM files SQLite puts beside them.
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        server = make_server(args.data, host=args.host, port=args.port, tls_cert=args.tls_cert,
                             tls_key=args.tls_key, allow_plaintext=args.allow_plaintext)
    except (ValueError, OSError, ssl.SSLError) as exc:
        raise SystemExit(f"roadmap serve: {exc}") from exc
    scheme = "https" if server.tls else "http"
    host, port = server.server_address[:2]
    print(f"roadmap serve: {scheme}://{host}:{port} — data in {Path(args.data).resolve()}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _cmd_tenant(args: Any) -> int:
    reg = Registry(args.data)
    try:
        if args.tenant_command == "add":
            reg.add_tenant(args.tenant)
            print(f"tenant {args.tenant} added — its store is {reg.tenant_path(args.tenant)}")
        elif args.tenant_command == "list":
            _print_rows(reg.tenants(), ("id", "created_at", "disabled_at"))
        elif args.tenant_command == "disable":
            print("disabled" if reg.disable_tenant(args.tenant) else "no such enabled tenant")
        elif args.tenant_command == "enable":
            print("enabled" if reg.enable_tenant(args.tenant) else "no such tenant")
    except RegistryError as exc:
        raise SystemExit(f"roadmap serve tenant: {exc}") from exc
    return 0


def _cmd_user(args: Any) -> int:
    reg = Registry(args.data)
    try:
        if args.user_command == "add":
            reg.add_user(args.user, label=args.label)
            print(f"user {args.user} added — grant it tenants with `user grant`, "
                  f"then `token create --user {args.user}`")
        elif args.user_command == "list":
            rows = reg.users()
            for r in rows:
                r["grants"] = " ".join(f"{t}:{','.join(sc)}"
                                       for t, sc in r["grants"].items()) or "-"
            _print_rows(rows, ("id", "label", "grants", "created_at", "disabled_at"))
        elif args.user_command == "grant":
            reg.grant(args.user, args.tenant, args.scopes.split(","))
            print(f"user {args.user} holds {args.scopes} on tenant {args.tenant}")
        elif args.user_command == "revoke-grant":
            print("revoked" if reg.revoke_grant(args.user, args.tenant) else "no such grant")
        elif args.user_command == "disable":
            print("disabled" if reg.disable_user(args.user) else "no such enabled user")
        elif args.user_command == "enable":
            print("enabled" if reg.enable_user(args.user) else "no such user")
    except RegistryError as exc:
        raise SystemExit(f"roadmap serve user: {exc}") from exc
    return 0


def _cmd_token(args: Any) -> int:
    reg = Registry(args.data)
    try:
        if args.token_command == "create" and args.user:
            token_id, secret = reg.create_user_token(
                args.user, label=args.label, scopes=args.scopes.split(","),
                expires_days=args.expires_days)
            print(f"token {token_id} for user {args.user} (at most {args.scopes}, "
                  f"on each tenant it holds a grant on;"
                  f" a request names its tenant in {TENANT_HEADER}, the CLI from ROADMAP_TENANT)")
            print("store it now — the server keeps only its hash, and it will not be shown again:")
            print(secret)
        elif args.token_command == "create":
            if not args.tenant:
                raise RegistryError("name --tenant (a tenant token) or --user (a user token)")
            token_id, secret = reg.create_token(
                args.tenant, label=args.label, scopes=args.scopes.split(","),
                expires_days=args.expires_days)
            print(f"token {token_id} for tenant {args.tenant} ({args.scopes})")
            print("store it now — the server keeps only its hash, and it will not be shown again:")
            print(secret)
        elif args.token_command == "list":
            rows = reg.tokens(args.tenant)
            for r in rows:
                r["scopes"] = ",".join(r["scopes"])
            _print_rows(rows, ("id", "tenant", "user", "label", "scopes", "created_at",
                               "expires_at",
                               "revoked_at", "last_used_at"))
        elif args.token_command == "revoke":
            print("revoked" if reg.revoke_token(args.token_id) else "no such live token")
    except RegistryError as exc:
        raise SystemExit(f"roadmap serve token: {exc}") from exc
    return 0


def add_parser(sub: Any) -> None:
    """Register `roadmap serve` on the CLI's subparsers."""
    p = sub.add_parser(
        "serve",
        help="run a roadmap server: many tenants, one SQLite file each, token-authenticated",
        description=(__doc__ or "").split("\n\n")[0],
    )
    p.add_argument("--data", required=True,
                   help="the server's data directory: registry.db and tenants/<id>.db")
    ssub = p.add_subparsers(dest="serve_command", required=True)

    run = ssub.add_parser("run", help="serve until interrupted")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("--tls-cert", help="PEM certificate chain; with --tls-key, serves HTTPS")
    run.add_argument("--tls-key", help="PEM private key for --tls-cert")
    run.add_argument("--allow-plaintext", action="store_true",
                     help="serve plain HTTP on a non-loopback interface — only when something "
                          "else (a private network's own TLS) encrypts the wire")
    run.set_defaults(func=_cmd_run)

    tenant = ssub.add_parser("tenant", help="add, list, disable or enable a tenant")
    tsub = tenant.add_subparsers(dest="tenant_command", required=True)
    for name in ("add", "disable", "enable"):
        tsub.add_parser(name).add_argument("tenant")
    tsub.add_parser("list")
    tenant.set_defaults(func=_cmd_tenant)

    token = ssub.add_parser("token", help="create, list or revoke a tenant's tokens")
    ksub = token.add_subparsers(dest="token_command", required=True)
    create = ksub.add_parser("create", help="mint a token; it is printed once")
    who = create.add_mutually_exclusive_group(required=True)
    who.add_argument("--tenant", help="a tenant token: bound to this tenant alone")
    who.add_argument("--user", help="a user token: every tenant the user holds a grant on, "
                                     "named per request")
    create.add_argument("--label", required=True, help="who holds it, e.g. 'wren (ceo)'")
    create.add_argument("--scopes", default=",".join(DEFAULT_SCOPES),
                        help=f"comma-separated, from {', '.join(SCOPES)} (default: read,write)")
    create.add_argument("--expires-days", type=float, default=None)
    ksub.add_parser("list").add_argument("--tenant", default=None)
    ksub.add_parser("revoke").add_argument("token_id")
    token.set_defaults(func=_cmd_token)

    user = ssub.add_parser(
        "user", help="add, list, grant, disable or enable a person who works across tenants")
    usub = user.add_subparsers(dest="user_command", required=True)
    ua = usub.add_parser("add")
    ua.add_argument("user")
    ua.add_argument("--label", required=True, help="who it is, e.g. 'gal (operator)'")
    usub.add_parser("list")
    ug = usub.add_parser(
        "grant", help="give the user scopes on a tenant; a second grant replaces the first")
    ug.add_argument("user")
    ug.add_argument("--tenant", required=True)
    ug.add_argument("--scopes", default=",".join(DEFAULT_SCOPES),
                    help=f"comma-separated, from {', '.join(SCOPES)} (default: read,write)")
    ur = usub.add_parser("revoke-grant")
    ur.add_argument("user")
    ur.add_argument("--tenant", required=True)
    for name in ("disable", "enable"):
        usub.add_parser(name).add_argument("user")
    user.set_defaults(func=_cmd_user)
