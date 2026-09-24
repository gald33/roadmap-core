# The roadmap server's image, built from this checkout (context: the repo root).
#
# Built from the source tree rather than installed from PyPI so the VM runs
# exactly the commit it cloned: `git -C ~/roadmap-core rev-parse HEAD` is the
# running version, with no second place (a registry) that could disagree.
FROM python:3.12-slim

# deploy.sh removes this image's superseded builds, and no other image, by it.
LABEL org.opencontainers.image.title=roadmap-serve

# One unprivileged user. It owns /data before the volume is first mounted, so a
# fresh named volume inherits that ownership and nothing here ever runs as root.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin roadmap \
 && mkdir /data && chown roadmap:roadmap /data && chmod 700 /data

COPY pyproject.toml README.md LICENSE /src/
COPY roadmap_core /src/roadmap_core
COPY templates /src/templates
RUN pip install --no-cache-dir /src && rm -rf /src

USER roadmap
VOLUME /data
EXPOSE 8765

# Plain HTTP on the host's private Docker network only. No port of this
# container is published, and the host's Caddy terminates TLS in front of it.
# `--allow-plaintext` is the server's own refusal switched off for exactly that
# case. See deploy/vm/README.md.
ENTRYPOINT ["roadmap", "serve", "--data", "/data"]
CMD ["run", "--host", "0.0.0.0", "--port", "8765", "--allow-plaintext"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)"]
