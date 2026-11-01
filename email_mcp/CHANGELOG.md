# Changelog

## 0.1.1

- Fix the add-on going unreachable after a few days with `Upstream MCP server unavailable: PoolTimeout` until it was restarted.
- GET /mcp now returns 405 Method Not Allowed, so clients do not hold an idle server-sent-events stream open. A dropped stream also releases its upstream connection.
- A full connection pool returns 503 instead of waiting until the add-on is restarted.
- The Supervisor watchdog restarts the add-on when the upstream mail server stops answering.

## 0.1.0

- First release of the Email MCP add-on.
- Read-only by default, with optional draft and send limited to an allow-list.
- iCloud and Yahoo presets, plus custom IMAP/SMTP accounts.
- Bearer token generated on first start when the field is left empty.
- Configuration tab strings for every option.
