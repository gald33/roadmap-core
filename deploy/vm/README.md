# `roadmap serve` on a VM

One server for every org's roadmap: the TLS in front of it, the one-time steps,
and the commands to run it. Files here:

| File | What it is |
|---|---|
| `Dockerfile` | the server, built from this checkout; runs as uid 10001, root filesystem read-only |
| `caddy/Dockerfile` | Caddy with the Cloudflare DNS module, for a DNS-01 certificate |
| `Caddyfile` | TLS for `$ROADMAP_HOST` on `$ORIGIN_PORT`, proxying to the server; `Authorization` is deleted from access logs |
| `docker-compose.yml` | the two, on their own network; only Caddy publishes a port |
| `.env.example` | the four values the stack needs; copied to `.env`, which is never committed |

## The shape

```
session ──HTTPS :443──▶ Cloudflare (roadmap.lucille-ai.com, proxied)
                          │  Origin Rule: destination port → 8445
                          ▼
                     VM :8445 ── Caddy (Let's Encrypt cert by DNS-01)
                                   │  compose network only
                                   ▼
                              roadmap:8765 ── /data (SQLite: registry + one file per tenant)
```

This is the shape the switchboard hub already runs on the Lucille VM (Lucille
`CLAUDE.md`, "Ingress"). Cloudflare on :443 is what makes the server reachable
from cloud agent sessions, whose egress often allows only standard-port HTTPS.
DNS-01 is the only ACME challenge that works for an origin on a non-standard
port. The stack shares the VM with Lucille's own stack, but not its compose
files, its Caddy or its deploy.

## Once: DNS, at Cloudflare

1. An `A` record `roadmap` → the VM's address, **proxied** (orange cloud).
2. An Origin Rule: hostname equals `roadmap.lucille-ai.com` → destination port `8445`.
3. SSL/TLS mode **Full (strict)** for that host. The origin's certificate is a
   real Let's Encrypt one, so strict works.
4. An API token with `Zone:DNS:Edit` on `lucille-ai.com`, for the DNS-01
   challenge. The switchboard hub's is scoped the same way.

## Once: the VM

```bash
ss -ltn | grep -q ':8445 ' && echo "8445 is taken" || echo "8445 is free"   # 8443 is Lucille's Caddy, 8444 the hub
git clone https://github.com/gald33/roadmap-core ~/roadmap-core
cd ~/roadmap-core && git checkout <release tag or commit>    # the image is built from what is checked out
cp deploy/vm/.env.example deploy/vm/.env && chmod 600 deploy/vm/.env
$EDITOR deploy/vm/.env                                      # ACME_EMAIL, CLOUDFLARE_API_TOKEN
docker compose -f deploy/vm/docker-compose.yml up -d --build
curl -s https://roadmap.lucille-ai.com/healthz              # {"ok":true}
```

If the host firewall filters inbound ports, open `8445/tcp` to Cloudflare. If a
per-source connection limit guards the hub's `:8444`, extend it to `:8445`.

## Tenants and tokens, on the VM

Managed here and never over HTTP. A token is printed once, and the server keeps
only its SHA-256.

```bash
roadmap_admin() { docker compose -f ~/roadmap-core/deploy/vm/docker-compose.yml exec roadmap roadmap serve --data /data "$@"; }
roadmap_admin tenant add org-core
roadmap_admin token create --tenant org-core --label "wren (ceo)"            # read,write
roadmap_admin token create --tenant org-core --label "operator" --scopes read,write,admin
roadmap_admin token list
roadmap_admin token revoke tok_…
```

Each session of that org gets the token as an environment secret, never a
committed file:

```bash
ROADMAP_API_URL=https://roadmap.lucille-ai.com
ROADMAP_API_TOKEN=rmk_…
```

`roadmap push --source db` seeds the tenant from the org's `roadmap/items/`, and
`roadmap claim|release|status --source db` then write to it with no pull request.

## Upgrade

```bash
cd ~/roadmap-core && git fetch && git checkout <new tag or commit>
docker compose -f deploy/vm/docker-compose.yml up -d --build
```

The data volume is kept. The schema is created on open (`CREATE TABLE IF NOT
EXISTS`), not migrated.

## Backup

An online copy, safe while the server runs (SQLite's backup API), into the
volume. Then copy it off the box:

```bash
cd ~/roadmap-core
docker compose -f deploy/vm/docker-compose.yml exec -T roadmap python -c '
import datetime, os, pathlib, sqlite3
os.umask(0o077)
out = pathlib.Path("/data/backups") / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out.mkdir(parents=True)
for p in [pathlib.Path("/data/registry.db"), *sorted(pathlib.Path("/data/tenants").glob("*.db"))]:
    with sqlite3.connect(p) as src, sqlite3.connect(out / p.name) as dst:
        src.backup(dst)
    print(out / p.name)
'
docker compose -f deploy/vm/docker-compose.yml cp roadmap:/data/backups ./roadmap-backups
```

## Known limits

- **Every client arrives from Caddy's address.** The server's per-address budget
  for *failed* authentication (20, then one every 5 s) is therefore shared by all
  clients. A valid token never spends it. The server does not read
  `X-Forwarded-For`, by design: a header a client can set is not an address.
- **`impact` answers 501.** It needs a host's feedback tickets.
- **Container logs are capped** at 50 MB × 5 per service.
