# mcp-email-server 1.13.3 (BSD-3-Clause), digest-pinned.
# The base image is a literal on purpose. Home Assistant passes a BUILD_FROM
# build-arg when it builds a local add-on, and an ARG default would be replaced
# by the Home Assistant base image.
# hadolint ignore=DL3006
FROM ghcr.io/wh1isper/mcp-email-server:1.13.3@sha256:3cd28108c44bde99fd1d24f13653291a64086346be9d36604e7ec9acd68b7aa9

# hadolint ignore=DL3002
# The upstream image's user is unchanged. tini remains pid 1.

COPY rootfs/opt/email_mcp /opt/email_mcp

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG BUILD_ARCH=amd64
ARG BUILD_DATE
ARG BUILD_DESCRIPTION="Read-only-by-default MCP server for iCloud, Yahoo, and other IMAP mail"
ARG BUILD_NAME="Email MCP (iCloud / Yahoo)"
ARG BUILD_REF
ARG BUILD_REPOSITORY=Madseason41/ha-email-mcp
ARG BUILD_VERSION=0.1.0

LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="${BUILD_NAME}" \
    org.opencontainers.image.description="${BUILD_DESCRIPTION}" \
    org.opencontainers.image.source="https://github.com/${BUILD_REPOSITORY}" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="${BUILD_VERSION}" \
    org.opencontainers.image.revision="${BUILD_REF}" \
    org.opencontainers.image.created="${BUILD_DATE}"

# /healthz is unauthenticated and only reports that the proxy process is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9560/healthz', timeout=3)"]

ENTRYPOINT ["tini", "--", "/app/.venv/bin/python", "/opt/email_mcp/run.py"]
CMD []
