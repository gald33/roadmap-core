# Caddy with the Cloudflare DNS module, for a DNS-01 certificate: the only ACME
# challenge that works when the origin serves on a non-standard port, which is
# how the switchboard hub on the same VM is reached (Lucille CLAUDE.md, "Ingress").
FROM caddy:2-builder AS build
RUN xcaddy build --with github.com/caddy-dns/cloudflare

FROM caddy:2-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
