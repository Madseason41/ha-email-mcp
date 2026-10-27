# Email MCP (iCloud / Yahoo)

Read-only-by-default mailbox access for Home Assistant, for iCloud, Yahoo, and other IMAP servers.

The add-on runs [mcp-email-server](https://github.com/wh1isper/mcp-email-server) 1.13.3 by wh1isper, which is BSD 3-Clause. This add-on is MIT. A bearer-token proxy sits in front of the mail server. Sending, drafts, moving, deleting, and marking read are refused unless you explicitly allow drafts and sending, and sending still only goes to addresses you list.

Setup is entirely in the Home Assistant UI. The step-by-step guide is [DOCS.md](DOCS.md), which is also what the add-on's **Documentation** tab shows.

## Architecture

Only `amd64` is supported. There is no `image` key: Home Assistant builds the add-on from this Dockerfile when you install it. The upstream image is pinned by digest.

The port is not published on the host. Installed from this repository, Home Assistant Core reaches it at `http://ba3d2a6a-email-mcp:9560`. A local add-on copy is reached at `http://local-email-mcp:9560`. A companion integration, `email_mcp_bridge`, is in this repository and relays a Home Assistant webhook to that address, because Home Assistant Cloud does not forward add-on ports.

## Configuration

Every setting is on the add-on's **Configuration** tab, with names and descriptions in `translations/en.yaml`. For iCloud or Yahoo, an account only needs a name, email address, provider, and app password. Other account fields are optional and can be left blank.
