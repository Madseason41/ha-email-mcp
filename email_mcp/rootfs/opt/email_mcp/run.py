"""Add-on entrypoint: options.json -> mcp-email-server TOML -> two supervised processes.

Never prints passwords or the bearer token. All secret-bearing files are 0600.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import tomli_w

LOG = logging.getLogger("email_mcp")

OPTIONS_PATH = Path(os.environ.get("EMAIL_MCP_OPTIONS", "/data/options.json"))
DATA_DIR = Path(os.environ.get("EMAIL_MCP_DATA_DIR", "/data"))
RUN_DIR = Path(os.environ.get("EMAIL_MCP_RUN_DIR", "/tmp/email_mcp"))
LISTEN_PORT = int(os.environ.get("EMAIL_MCP_LISTEN_PORT", "9560"))
UPSTREAM_PORT = int(os.environ.get("EMAIL_MCP_UPSTREAM_PORT", "9557"))
HERE = Path(__file__).resolve().parent

# (host, port, use_ssl, start_ssl)
PRESETS = {
    "icloud": {
        "imap": ("imap.mail.me.com", 993, True, False),
        "smtp": ("smtp.mail.me.com", 587, False, True),
    },
    "yahoo": {
        "imap": ("imap.mail.yahoo.com", 993, True, False),
        "smtp": ("smtp.mail.yahoo.com", 465, True, False),
    },
}
SECURITY = {"ssl": (True, False), "starttls": (False, True), "none": (False, False)}
# Words the Configuration form may store when the user left the control on its default.
_BOOL_UNSET = {"", "default", "preset"}
_BOOL_TRUE = {"yes", "true", "on", "1"}
_BOOL_FALSE = {"no", "false", "off", "0"}


class ConfigError(Exception):
    pass


def _blank(value) -> bool:
    """An optional Configuration-tab field that was left empty."""
    return value is None or (isinstance(value, str) and not value.strip())


def _opt_bool(source: dict, key: str, default: bool) -> bool:
    """Missing or blank means *default*. Explicit yes/no and real bools are honored."""
    if key not in source or _blank(source.get(key)):
        return default
    value = source[key]
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _BOOL_UNSET:
            return default
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
        raise ConfigError(f"{key} must be yes or no (or left blank)")
    return bool(value)


def _str_list(values) -> list[str]:
    return [item.strip() for item in (values or []) if isinstance(item, str) and item.strip()]


def _endpoint(acct: dict, role: str) -> dict | None:
    provider = acct.get("provider", "custom")
    preset = PRESETS.get(provider, {}).get(role)
    host = acct.get(f"{role}_host")
    if _blank(host):
        host = preset[0] if preset else None
    if not host:
        if role == "imap":
            raise ConfigError(f"account '{acct.get('name')}': imap_host is required for provider=custom")
        return None  # IMAP-only custom account
    port = acct.get(f"{role}_port")
    if _blank(port):
        port = preset[1] if preset else (993 if role == "imap" else 465)
    sec = acct.get(f"{role}_security")
    if _blank(sec) or (isinstance(sec, str) and sec.strip().lower() in _BOOL_UNSET):
        sec = None
    if sec:
        try:
            use_ssl, start_ssl = SECURITY[sec]
        except KeyError:
            raise ConfigError(
                f"account '{acct.get('name')}': {role}_security must be ssl, starttls, or none"
            ) from None
    elif preset:
        use_ssl, start_ssl = preset[2], preset[3]
    else:
        use_ssl, start_ssl = (True, False)
    username = acct.get("username")
    if _blank(username):
        username = acct["email"]
    if role == "smtp" and not _blank(acct.get("smtp_username")):
        username = acct["smtp_username"]
    return {
        "user_name": username,
        "password": acct["password"],
        "host": host,
        "port": int(port),
        "use_ssl": use_ssl,
        "start_ssl": start_ssl,
        "verify_ssl": _opt_bool(acct, "verify_ssl", True),
    }


def build_config(options: dict) -> dict:
    accounts = options.get("accounts") or []
    if not accounts:
        raise ConfigError("no accounts configured")
    allow_send = _opt_bool(options, "allow_send", False)
    recipients = _str_list(options.get("allowed_recipients"))
    names: set[str] = set()
    emails = []
    for acct in accounts:
        name = acct.get("name")
        if not name or name in names:
            raise ConfigError(f"account names must be unique and non-empty (got {name!r})")
        names.add(name)
        if _blank(acct.get("email")) or _blank(acct.get("password")):
            raise ConfigError(f"account '{name}': email and password are required")
        full_name = acct.get("full_name")
        if _blank(full_name):
            full_name = acct["email"].split("@")[0]
        entry = {
            "account_name": name,
            "full_name": full_name,
            "email_address": acct["email"],
            "save_to_sent": _opt_bool(acct, "save_to_sent", True),
            "incoming": _endpoint(acct, "imap"),
        }
        if not _blank(acct.get("drafts_mailbox")):
            entry["drafts_mailbox"] = acct["drafts_mailbox"]
        if not _blank(acct.get("sent_folder_name")):
            entry["sent_folder_name"] = acct["sent_folder_name"]
        smtp = _endpoint(acct, "smtp")
        if smtp:
            entry["outgoing"] = smtp
        emails.append(entry)
    return {
        "credential_storage": "plaintext",
        # Attachment files are never written to disk. There is no option for this.
        "enable_attachment_download": False,
        "enable_attachment_content": _opt_bool(options, "enable_attachment_content", False),
        # Read-only unless the user flips allow_send. Only draft+send are ever
        # granted; organize/delete/append (mark-read, move, delete) stay off.
        "allowed_mutations": ["draft", "send"] if allow_send else [],
        # Empty list denies every recipient even if send were granted.
        "allowed_recipients": recipients if allow_send else [],
        # Empty does not hide mail. It only filters From when the user adds patterns.
        "allowed_senders": _str_list(options.get("allowed_senders")),
        "report_blocked_mutations": _opt_bool(options, "report_blocked_mutations", True),
        "emails": emails,
    }


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)


def _push_token_to_options(options: dict, token: str) -> None:
    """Best effort: store a generated token in the add-on's own options so the
    user can reveal it in the Configuration tab (password field, masked)."""
    sup = os.environ.get("SUPERVISOR_TOKEN")
    if not sup:
        return
    new_opts = dict(options)
    new_opts["bearer_token"] = token
    req = urllib.request.Request(
        "http://supervisor/addons/self/options",
        data=json.dumps({"options": new_opts}).encode(),
        headers={"Authorization": f"Bearer {sup}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed internal URL)
            ok = resp.status == 200
    except Exception as err:  # noqa: BLE001
        LOG.warning("Could not store generated bearer token in add-on options (%s); "
                    "it is still persisted in the add-on's /data", type(err).__name__)
        return
    if ok:
        LOG.info("Generated bearer token was saved to this add-on's Configuration (bearer_token, masked)")


def resolve_token(options: dict) -> str:
    configured = (options.get("bearer_token") or "").strip()
    token_path = DATA_DIR / "bearer_token"
    if configured:
        if len(configured) < 32:
            raise ConfigError("bearer_token must be at least 32 characters (or leave empty to auto-generate)")
        return configured
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            LOG.info("Using previously generated bearer token from /data")
            return token
    token = secrets.token_urlsafe(48)
    _write_private(token_path, token.encode())
    LOG.info("Generated a new random bearer token and stored it under /data (value not logged)")
    _push_token_to_options(options, token)
    return token


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        options = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
        cfg = build_config(options)
        token = resolve_token(options)
    except (OSError, ValueError, KeyError, ConfigError) as err:
        LOG.error("Configuration error: %s", err)
        return 1

    log_level = (options.get("log_level") or "info").upper()
    RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RUN_DIR, 0o700)
    config_path = RUN_DIR / "config.toml"
    _write_private(config_path, tomli_w.dumps(cfg).encode())
    token_file = RUN_DIR / "token"
    _write_private(token_file, token.encode())
    del token

    LOG.info(
        "Accounts: %s | mutations: %s | allowed recipients: %d",
        ", ".join(f"{e['account_name']} ({e['incoming']['host']})" for e in cfg["emails"]),
        ",".join(cfg["allowed_mutations"]) or "none (read-only)",
        len(cfg["allowed_recipients"]),
    )

    # Minimal environments: children never see the bearer token or SUPERVISOR_TOKEN.
    base_env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH")}
    base_env["HOME"] = str(RUN_DIR)
    server_env = dict(base_env,
                      MCP_EMAIL_SERVER_CONFIG_PATH=str(config_path),
                      MCP_EMAIL_SERVER_LOG_LEVEL="WARNING" if log_level == "INFO" else log_level,
                      PYTHONUNBUFFERED="1")
    proxy_env = dict(base_env,
                     EMAIL_MCP_TOKEN_FILE=str(token_file),
                     EMAIL_MCP_LISTEN_PORT=str(LISTEN_PORT),
                     EMAIL_MCP_UPSTREAM=f"http://127.0.0.1:{UPSTREAM_PORT}",
                     EMAIL_MCP_LOG_LEVEL=log_level,
                     PYTHONUNBUFFERED="1")
    py = sys.executable
    server = subprocess.Popen([py, str(HERE / "launcher.py"), "--host", "127.0.0.1", "--port", str(UPSTREAM_PORT)],
                              env=server_env)
    proxy = subprocess.Popen([py, str(HERE / "authproxy.py")], env=proxy_env)
    LOG.info("MCP endpoint: http://<addon-hostname>:%d/mcp (bearer auth required)", LISTEN_PORT)

    stopping = False

    def _stop(*_):
        nonlocal stopping
        stopping = True
        for p in (proxy, server):
            if p.poll() is None:
                p.terminate()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while True:
        for name, p in (("mcp-email-server", server), ("auth proxy", proxy)):
            rc = p.poll()
            if rc is not None:
                if not stopping:
                    LOG.error("%s exited with code %s; stopping add-on", name, rc)
                _stop()
                for q in (proxy, server):
                    try:
                        q.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        q.kill()
                return 0 if stopping else 1
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
