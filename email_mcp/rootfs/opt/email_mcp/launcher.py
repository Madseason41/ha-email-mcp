"""Start mcp-email-server's streamable-http transport in stateless JSON mode.

Why: Home Assistant Cloud cloudhooks relay only the response body, status and
Content-Type (no Mcp-Session-Id header, no streaming). Stateless mode removes
the need for the session header and json_response makes every POST answer a
single application/json body instead of an SSE stream. FastMCP's constructor
pins these settings, so the FASTMCP_* env vars do not apply; we flip them on
the module-level server object before the CLI command runs.
"""

import sys

from mcp_email_server import cli
from mcp_email_server.app import mcp

mcp.settings.stateless_http = True
mcp.settings.json_response = True

if __name__ == "__main__":
    sys.argv = ["mcp-email-server", "streamable-http", *sys.argv[1:]]
    cli.app()
