#!/usr/bin/env bash
#
# Deploy `roadmap serve` on a VM whose :443 is already owned by another stack's
# Caddy: build and start the server from this checkout, install its site block
# into that Caddy's conf.d/, and check the public URL answers.
#
# Deploys what is checked out. It never fetches or switches commits; the
# runbook (README.md beside this file) checks out a tag first.
#
# Idempotent: a second run with nothing changed rebuilds from cache, leaves the
# site block alone, and checks the same things.
#
# The ingress half is the pattern gald33/nivi's scripts/deploy.sh uses on the
# same host, with one change: a site block Caddy rejects is taken back out of
# conf.d/ rather than left there for the next Caddy restart to fail on.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE_PATH=deploy/vm/docker-compose.yml
SITE_BLOCK=deploy/vm/roadmap.caddyfile
CONTAINER=roadmap-serve

# Where the host's Caddy reads co-tenant site blocks from, and the container to
# reload once a block changes. The defaults are the Lucille VM's.
PLATFORM_NETWORK="${PLATFORM_NETWORK:-lucille_default}"
PLATFORM_CADDY_CONFD="${PLATFORM_CADDY_CONFD:-/opt/lucille/caddy/conf.d}"
PLATFORM_CADDY_CONTAINER="${PLATFORM_CADDY_CONTAINER:-lucille-caddy}"

# Free space on / below which the build is refused. It is refused rather than
# pruned: the disk is shared, and the other tenants' images and build cache are
# not this script's to delete. The image is ~200 MB.
MIN_DISK_FREE_MB="${MIN_DISK_FREE_MB:-1024}"

export PLATFORM_NETWORK

log() { printf '[roadmap-deploy] %s\n' "$*"; }
die() { printf '[roadmap-deploy] ERROR: %s\n' "$*" >&2; exit 1; }
compose() { docker compose -f "$COMPOSE_FILE_PATH" "$@"; }

# The hostname is set in one place, the site block's address, and read from it.
HOST="$(awk '/^[^#[:space:]].*\{[[:space:]]*$/ { print $1; exit }' "$SITE_BLOCK")"
[[ -n "$HOST" ]] || die "no site address found in $SITE_BLOCK"
PUBLIC_URL="https://$HOST/healthz"

preflight() {
  docker info >/dev/null 2>&1 || die "docker is not reachable by this user"
  docker network inspect "$PLATFORM_NETWORK" >/dev/null 2>&1 \
    || die "network '$PLATFORM_NETWORK' not found (docker network ls; set PLATFORM_NETWORK)"
  # With no conf.d there is no way in: unlike a site that also has an older
  # inline block, this server has no other ingress, so this is fatal, not a skip.
  [[ -d "$PLATFORM_CADDY_CONFD" ]] \
    || die "$PLATFORM_CADDY_CONFD not found: the host's Caddy has no conf.d to install into"
  docker inspect "$PLATFORM_CADDY_CONTAINER" >/dev/null 2>&1 \
    || die "container '$PLATFORM_CADDY_CONTAINER' not found (set PLATFORM_CADDY_CONTAINER)"
  local free_mb
  free_mb="$(df -Pm / | awk 'NR==2 { print $4 }')"
  (( free_mb >= MIN_DISK_FREE_MB )) \
    || die "/ has ${free_mb} MB free, under the ${MIN_DISK_FREE_MB} MB floor. Free space first; this script does not prune what is not its own."
  log "Deploying $(git describe --always --dirty 2>/dev/null || echo 'an unknown commit') as $HOST"
}

start() {
  log "Building and starting $CONTAINER"
  compose up -d --build --remove-orphans
}

# Ask the server itself, from inside its container, rather than Docker's health
# status: that one first reports after a full health interval, and a container
# counts as running seconds before the server has bound its port.
verify_local() {
  local attempts=30
  while (( attempts-- > 0 )); do
    if docker exec "$CONTAINER" python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)" \
        >/dev/null 2>&1; then
      log "Server healthy inside $CONTAINER"
      return 0
    fi
    sleep 2
  done
  docker logs --tail 50 "$CONTAINER" 2>&1 || true
  die "$CONTAINER did not answer /healthz after 60s"
}

# Install the site block into the host Caddy's conf.d/ and reload it.
#
# A reload carrying a config Caddy rejects leaves the running config serving,
# so a bad block cannot take the site down at reload time. But left in conf.d/
# it would at the next Caddy *start* (a reboot, or a Lucille deploy recreating
# the container), when the whole config fails to load and every site on the box
# goes dark with it. So a
# rejected block is taken back out: the previous one is restored, or the file
# is removed if there was none, and the deploy fails.
install_ingress() {
  local target="$PLATFORM_CADDY_CONFD/roadmap.caddyfile" backup=""
  if cmp -s "$SITE_BLOCK" "$target"; then
    log "Ingress unchanged"
    return 0
  fi
  if [[ -f "$target" ]]; then
    backup="$(mktemp)"
    cp "$target" "$backup"
  fi
  log "Installing $SITE_BLOCK into $PLATFORM_CADDY_CONFD"
  cp "$SITE_BLOCK" "$target"
  # Reload, not restart: a restart drops every live connection on the box.
  if docker exec "$PLATFORM_CADDY_CONTAINER" \
      caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
    [[ -n "$backup" ]] && rm -f "$backup"
    return 0
  fi
  if [[ -n "$backup" ]]; then
    cp "$backup" "$target" && rm -f "$backup"
    log "Restored the previous $target"
  else
    rm -f "$target"
    log "Removed the rejected $target"
  fi
  die "Caddy rejected the reload with $SITE_BLOCK in conf.d; the running config is unchanged"
}

# The only check that speaks for a client. The container can answer on its own
# network while nobody outside can reach it; gald33/nivi lost 9.5 hours to a
# missing site block while every local check passed.
#
# Runs from the VM against the public name, which hairpins back through sslh
# and Caddy on this host. A first deploy waits here for Caddy's certificate.
verify_public() {
  local attempt=0 body="" rc=0
  while (( attempt++ < 24 )); do
    body="$(curl -sS --max-time 20 "$PUBLIC_URL" 2>/dev/null)" && rc=0 || rc=$?
    if (( rc == 0 )) && [[ "$body" == '{"ok":true}' ]]; then
      log "Public URL healthy ($PUBLIC_URL -> $body)"
      return 0
    fi
    sleep 5
  done
  if (( rc == 6 )); then
    die "$HOST does not resolve from this host. Create its DNS record (README.md beside this file) and run this again."
  fi
  if (( rc == 35 )); then
    die "$PUBLIC_URL failed the TLS handshake: Caddy has no certificate to offer for $HOST. Either conf.d lacks the site block (ls $PLATFORM_CADDY_CONFD; the Caddyfile must still import conf.d), or the certificate was never issued: on a first deploy, check that $HOST resolves to this host and is grey-clouded, and read \`docker logs $PLATFORM_CADDY_CONTAINER 2>&1 | grep $HOST\`."
  fi
  die "$PUBLIC_URL answered '${body:-nothing}' (curl rc=$rc). The server is healthy inside its container, so look at the ingress, not the server."
}

# Every build retags roadmap-serve:local and leaves the previous image dangling.
# Remove those, and only those: the label filter keeps this to images built from
# deploy/vm/Dockerfile.
prune_own_images() {
  docker image prune -f --filter "label=org.opencontainers.image.title=roadmap-serve" >/dev/null || true
}

main() {
  preflight
  start
  verify_local
  install_ingress
  verify_public
  prune_own_images
  log "Deployed"
}

main "$@"
