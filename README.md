# Email MCP for Home Assistant

A private Home Assistant add-on repository plus a HACS-compatible integration. An assistant can read iCloud or Yahoo mail through Home Assistant. Sending stays off unless you turn it on and list the recipients.

| Piece | Path | What it does |
| --- | --- | --- |
| Add-on | [`email_mcp/`](email_mcp/) | Runs [mcp-email-server](https://github.com/wh1isper/mcp-email-server) 1.13.3 (BSD 3-Clause, by wh1isper) behind a bearer-token proxy. Home Assistant builds it locally. amd64 only. |
| Integration | [`custom_components/email_mcp_bridge/`](custom_components/email_mcp_bridge/) | UI config flow. Registers a Home Assistant webhook and, when Home Assistant Cloud is signed in, a cloud webhook, then relays MCP requests to the add-on. |

The code in this repository is [MIT](LICENSE). mcp-email-server is BSD 3-Clause and is not vendored here; the add-on uses its published container image, pinned by digest.

The user guide is [`email_mcp/DOCS.md`](email_mcp/DOCS.md). It is also shown on the add-on's Documentation tab. Nothing is configured in YAML.

## Layout

```
repository.yaml          Add-on repository metadata
email_mcp/               The add-on (config, Docker, docs, rootfs)
custom_components/       Email MCP Bridge custom integration
hacs.json                HACS metadata for the integration
.github/workflows/       Lint and tests. Does not publish images.
```

## Install

Home Assistant cannot install an add-on from a private GitHub repository. While this repository is private, copy `email_mcp` to `/addons/email_mcp` and `custom_components/email_mcp_bridge` to `/config/custom_components/`, then install the local add-on from the store. Making the repository public later lets you add `https://github.com/Madseason41/ha-email-mcp` as an add-on repository, and add the same URL in HACS as an integration. Installed from that repository, the add-on's internal address is `http://ba3d2a6a-email-mcp:9560`. A local copy under `/addons` is `http://local-email-mcp:9560`. The full click-by-click steps are in the [documentation](email_mcp/DOCS.md).

## Security

- Read-only by default. `allow_send` adds draft and send only, and an empty recipient list still denies every send.
- The integration and the add-on both check the bearer token. Unauthenticated requests never reach the mail server.
- Passwords, the token, and webhook addresses are not logged.
- Attachment files are never written to disk. Reading attachment bytes is a separate switch and defaults to off.

## Tests

```bash
pip install -r requirements-test.txt
pytest -q tests/addon tests/integration

# Needs Docker. Builds a local image and does not push it.
pip install -r requirements-e2e.txt
bash tests/e2e/run_e2e.sh
```

Tokens and passwords in the tests are fake.
