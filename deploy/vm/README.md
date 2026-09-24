# `roadmap serve` on a VM

One server for every org's roadmap, on a VM whose :443 already belongs to
another stack's Caddy. The server runs in its own container. The host's Caddy
terminates TLS in front of it, from a site block this repo installs into that
Caddy's `conf.d/`. Files here:

| File | What it is |
|---|---|
| `Dockerfile` | the server, built from this checkout; runs as uid 10001, root filesystem read-only |
| `docker-compose.yml` | the server alone, on the host Caddy's network; no port published |
| `roadmap.caddyfile` | the site block: the hostname, a body cap, the proxy to `roadmap-serve:8765` |
| `deploy.sh` | build, start, install the site block, reload Caddy, check the public URL |

## The shape

```
session ──HTTPS :443──▶ roadmap.lucille-ai.com  (A record, DNS only)
                          ▼
                     VM :443 ── sslh ── lucille-caddy (Let's Encrypt, automatic)
                                           │  conf.d/roadmap.caddyfile
                                           │  network lucille_default
                                           ▼
                                      roadmap-serve:8765 ── /data (SQLite: registry + one file per tenant)
```

The same arrangement `nivi.lucille-ai.com` runs on (gald33/nivi, `infra/README.md`):
the same Cloudflare rule, the same Caddy, and a site block from the service's own
repo. No Origin Rule, no API token, no second Caddy and no new port. The server
shares the host Caddy's network, `lucille_default`, and nothing else of
Lucille's stack.

## Once: DNS, at Cloudflare

An `A` record `roadmap` → `159.65.126.83` in the `lucille-ai.com` zone,
**grey-clouded (DNS only)**, like `nivi`. Create it before the first deploy:
Caddy asks Let's Encrypt for the certificate as soon as the site block loads,
and Let's Encrypt allows only a few failed validations per name per hour.

It must stay grey. Proxied, Cloudflare terminates TLS, so Caddy's TLS-ALPN
challenge cannot reach it, and "Always Use HTTPS" answers the HTTP-01 challenge
with a redirect. Caddy then keeps serving the certificate it has and **fails
renewal silently** until it expires, ninety days on (nivi's README, where this
was learned).

## Once: the sessions' network access

A cloud session reaches only the hosts its environment allows. Measured
2026-09-24 from an org-core session: `switchboard.lucille-ai.com` answered;
`nivi.lucille-ai.com`, `lucille-ai.com`, `example.com` and `www.cloudflare.com`
were all refused by the session's egress proxy. So the environment allows hosts
by name, not by how their DNS is served. Add `roadmap.lucille-ai.com` to the
allowed domains of every environment whose sessions use the server (the
environment's settings, Network access).

## Once: the VM

As the `lucille` user, which is in the `docker` group:

```bash
git clone https://github.com/gald33/roadmap-core ~/roadmap-core
git -C ~/roadmap-core checkout <release tag or commit>   # the image is built from what is checked out
~/roadmap-core/deploy/vm/deploy.sh
```

`deploy.sh` stops before building anything if one of these is missing: Docker,
the `lucille_default` network, `/opt/lucille/caddy/conf.d`, the `lucille-caddy`
container, or 1 GB free on `/`. It refuses to build rather than prune, because
the disk is shared and the other tenants' images are not its to delete. Then it:

1. builds and starts `roadmap-serve`;
2. waits for `/healthz` inside the container;
3. copies `roadmap.caddyfile` to `/opt/lucille/caddy/conf.d/` and runs
   `caddy reload` in `lucille-caddy` (a reload, so no live connection drops). If
   Caddy rejects it, the file is taken back out of `conf.d/`: left there, it
   would fail the whole config at Caddy's next restart, and every site on the box
   would go down with it;
4. checks `https://roadmap.lucille-ai.com/healthz` from the VM until it answers
   `{"ok":true}`, for up to two minutes. On a first deploy, that is the time
   Caddy takes to get the certificate.

Set `PLATFORM_NETWORK`, `PLATFORM_CADDY_CONFD`, `PLATFORM_CADDY_CONTAINER` or
`MIN_DISK_FREE_MB` to run it on a host where those differ.

### What it looks like when it breaks

- **`curl` exit 35, `tlsv1 alert internal error`**: Caddy has no certificate for
  the name. Either the site block is missing from `conf.d/`, or the certificate
  was never issued. The container is fine meanwhile. `deploy.sh` says which to
  check.
- **The site block vanished after a Lucille deploy**: Lucille's deploy runs
  `git clean -fd` in `/opt/lucille`. `conf.d/*.caddyfile` survives it only
  because Lucille's `.gitignore` ignores it (gald33/Lucille#1417). Re-run
  `deploy.sh` to put the block back.

## Tenants and tokens, on the VM

These are managed on the host and never over HTTP. A token is printed once, and
the server keeps only its SHA-256.

```bash
roadmap_admin() { docker exec roadmap-serve roadmap serve --data /data "$@"; }
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

`roadmap push --source db` seeds the tenant from the org's `roadmap/items/`.
After that, `roadmap claim|release|status --source db` writes to it with no pull
request.

## Upgrade

```bash
git -C ~/roadmap-core fetch && git -C ~/roadmap-core checkout <new tag or commit>
~/roadmap-core/deploy/vm/deploy.sh
```

The data volume `roadmap_roadmap-data` is kept. The schema is created on open
(`CREATE TABLE IF NOT EXISTS`); nothing migrates it. `deploy.sh` removes the
image each build replaces, and only that image.

## Backup

This makes an online copy into the volume, safe while the server runs (it uses
SQLite's backup API). Then copy the backup off the box:

```bash
docker exec -i roadmap-serve python -c '
import datetime, os, pathlib, sqlite3
os.umask(0o077)
out = pathlib.Path("/data/backups") / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out.mkdir(parents=True)
for p in [pathlib.Path("/data/registry.db"), *sorted(pathlib.Path("/data/tenants").glob("*.db"))]:
    with sqlite3.connect(p) as src, sqlite3.connect(out / p.name) as dst:
        src.backup(dst)
    print(out / p.name)
'
docker cp roadmap-serve:/data/backups ./roadmap-backups
```

## Known limits

- **Every client arrives from Caddy's address.** The server's budget for
  *failed* authentication (20 attempts, then one every 5 s) is kept per address,
  so all clients share it. A valid token never spends it. The server does not
  read `X-Forwarded-For`, by design: a header a client can set is not an
  address.
- **The server shares a network with Lucille's containers.** They can reach
  `roadmap-serve:8765` without TLS, and it can reach them. A token is still
  required for everything but `/healthz`. The network is the price of sharing
  the host's Caddy, the same price nivi pays.
- **Per-source connection ceiling on :443.** `lucille-connlimit` caps each
  source address at 40 concurrent connections to :443 by default (Lucille's
  `docker-compose.vm.yml`), shared with every other site on the box. Cloud sessions reach the box from a few shared egress
  addresses. The client opens one short connection per call.
- **A body over 1 MiB is refused, but not every client sees the 413.** Caddy
  answers 413 from the declared `Content-Length` before proxying (the server
  would answer 413 too, and behind a proxy that reached curl as a 502). curl
  reads the answer while it is still sending and prints the 413. Python's
  `urllib`, which the `roadmap` CLI uses, sends the whole body first. Caddy has
  closed the connection by then, so the CLI reports `Connection reset by peer`
  (measured 2026-09-24: roadmap-core 0.4.0, Caddy v2.11.4, Python 3.12).
- **`impact` answers 501.** It needs a host's feedback tickets.
- **Container logs are capped** at 50 MB × 5.
