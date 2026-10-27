"""Constants for the Email MCP Bridge integration."""

DOMAIN = "email_mcp_bridge"

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_WEBHOOK_ID = "webhook_id"
CONF_CLOUDHOOK_INJECT = "cloudhook_inject_token"

DEFAULT_URL = "http://local-email-mcp:9560"
MCP_PATH = "/mcp"

# Request headers relayed to the add-on (everything else is dropped).
FORWARD_REQUEST_HEADERS = (
    "Accept",
    "Content-Type",
    "Mcp-Session-Id",
    "Mcp-Protocol-Version",
    "Last-Event-ID",
)
# Response headers relayed back to the client (aiohttp/webhook path only;
# Home Assistant Cloud cloudhooks relay only Content-Type).
FORWARD_RESPONSE_HEADERS = ("Mcp-Session-Id", "Mcp-Protocol-Version", "WWW-Authenticate")
ALLOWED_CONTENT_TYPES = ("application/json", "text/event-stream")

MAX_REQUEST_BYTES = 4 * 1024 * 1024
CLOUDHOOK_REPLY_SECONDS = 60
