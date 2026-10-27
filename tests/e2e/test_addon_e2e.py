"""End-to-end test of the add-on image against a GreenMail IMAP/SMTP server.

Requires Docker, the image `local/email_mcp:test` (docker build email_mcp)
and a GreenMail container named `greenmail` on network `emailtest`
with 127.0.0.1:19560->9560 published (see tests/e2e/run_e2e.sh). All credentials here are fake test values.
"""

from __future__ import annotations

import imaplib
import json
import os
import smtplib
import subprocess
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest

try:
    import pytest_socket
except ImportError:  # e2e does not need it unless the HA plugin is installed
    pytest_socket = None

pytest.importorskip("mcp")
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

DOCKER = os.environ.get("DOCKER", "sudo docker").split()
IMAGE = os.environ.get("EMAIL_MCP_IMAGE", "local/email_mcp:test")
NAME = "local-email-mcp"
HOST_PORT = 19560
URL = f"http://127.0.0.1:{HOST_PORT}/mcp"
TOKEN = "test-token-NOT-A-SECRET-0123456789abcdefghijklmnop"  # fake
ALICE_PW = "fake-app-pw-alice"
GM_IMAP = ("127.0.0.1", 3143)
GM_SMTP = ("127.0.0.1", 3025)


def _enable_sockets() -> None:
    """The Home Assistant pytest plugin disables sockets before each test."""
    if pytest_socket is not None:
        pytest_socket.enable_socket()


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([*DOCKER, *args], capture_output=True, text=True, check=check)


def options(**over) -> dict:
    opts = {
        "accounts": [
            {
                "name": "alice",
                "email": "alice@test.local",
                "full_name": "Alice Test",
                "provider": "custom",
                "password": ALICE_PW,
                "imap_host": "127.0.0.1",
                "imap_port": 3143,
                "imap_security": "none",
                "smtp_host": "127.0.0.1",
                "smtp_port": 3025,
                "smtp_security": "none",
                "drafts_mailbox": "Drafts",
            }
        ],
        "allow_send": False,
        "allowed_recipients": [],
        "bearer_token": TOKEN,
        "log_level": "info",
    }
    opts.update(over)
    return opts


class Addon:
    def __init__(self, opts: dict) -> None:
        _enable_sockets()
        self.data = Path(tempfile.mkdtemp(prefix="email_mcp_data_"))
        (self.data / "options.json").write_text(json.dumps(opts))
        docker("rm", "-f", NAME, check=False)
        # Shares GreenMail's network namespace (inter-container bridge traffic is
        # filtered on this box); 19560 is published on the greenmail container.
        docker("run", "-d", "--name", NAME, "--network", "container:greenmail",
               "-v", f"{self.data}:/data", IMAGE)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{HOST_PORT}/healthz", timeout=1).status_code == 200:
                    # proxy up; give the MCP server a moment too
                    time.sleep(1.5)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError("add-on did not start:\n" + self.logs())

    def logs(self) -> str:
        r = docker("logs", NAME, check=False)
        return r.stdout + r.stderr

    def stop(self) -> None:
        docker("rm", "-f", NAME, check=False)


def seed_mail() -> None:
    # Clean INBOX + create Drafts, then deliver two messages to alice.
    _enable_sockets()
    im = imaplib.IMAP4(*GM_IMAP)
    im.login("alice@test.local", ALICE_PW)
    im.create("Drafts")
    im.select("INBOX")
    _, data = im.search(None, "ALL")
    for num in data[0].split():
        im.store(num, "+FLAGS", "\\Deleted")
    im.expunge()
    im.logout()
    with smtplib.SMTP(*GM_SMTP) as s:
        for i, subj in enumerate(["Quarterly report", "Dinner on Friday?"]):
            m = EmailMessage()
            m["From"] = "bob@test.local"
            m["To"] = "alice@test.local"
            m["Subject"] = subj
            m.set_content(f"Hello Alice, this is test message {i + 1}. Marker: E2E-BODY-{i + 1}")
            s.send_message(m)
    time.sleep(1)


def inbox_subjects(user: str, pw: str) -> list[str]:
    im = imaplib.IMAP4(*GM_IMAP)
    im.login(user, pw)
    im.select("INBOX")
    _, data = im.search(None, "ALL")
    subjects = []
    for num in data[0].split():
        _, msg = im.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
        subjects.append(msg[0][1].decode().strip().removeprefix("Subject: "))
    im.logout()
    return subjects


async def call(tool: str, args: dict, token: str = TOKEN):
    async with streamablehttp_client(URL, headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            return await session.call_tool(tool, args)


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content) + json.dumps(
        result.structuredContent or {}, default=str)


@pytest.fixture(scope="module")
def readonly_addon():
    seed_mail()
    a = Addon(options())
    yield a
    a.stop()


# ---------- auth gate ----------

def test_healthz_unauthenticated_ok(readonly_addon):
    assert httpx.get(f"http://127.0.0.1:{HOST_PORT}/healthz").status_code == 200


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": f"Basic {TOKEN}"},
    {"Authorization": f"Bearer {TOKEN}x"},
])
def test_rejects_without_valid_bearer(readonly_addon, headers):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    r = httpx.post(URL, json=body, headers={"Accept": "application/json, text/event-stream", **headers})
    assert r.status_code == 401
    assert "www-authenticate" in r.headers


# ---------- raw protocol shape (what the HA webhook / cloudhook sees) ----------

def test_raw_initialize_is_plain_json_and_stateless(readonly_addon):
    hdr = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream",
           "Content-Type": "application/json"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    r = httpx.post(URL, json=init, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in r.headers  # stateless: no session to carry
    assert r.json()["result"]["serverInfo"]["name"]
    # A *separate* request with no session id must work (stateless).
    r2 = httpx.post(URL, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=hdr)
    assert r2.status_code == 200, r2.text
    names = {t["name"] for t in r2.json()["result"]["tools"]}
    assert {"list_emails_metadata", "get_emails_content", "send_email", "save_draft"} <= names


# ---------- MCP client over streamable HTTP ----------

async def test_initialize_and_tools_list(readonly_addon):
    async with streamablehttp_client(URL, headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            assert init.serverInfo.name
            tools = await session.list_tools()
            assert len(tools.tools) >= 10


async def test_list_and_read_seeded_emails(readonly_addon):
    accts = await call("list_available_accounts", {})
    assert "alice" in text_of(accts)
    res = await call("list_emails_metadata", {"account_name": "alice", "page_size": 10})
    assert not res.isError, text_of(res)
    t = text_of(res)
    assert "Quarterly report" in t and "Dinner on Friday?" in t
    meta = res.structuredContent
    emails = meta.get("emails") or meta.get("result", {}).get("emails")
    ids = [e["email_id"] for e in emails]
    content = await call("get_emails_content", {"account_name": "alice", "email_ids": ids})
    assert not content.isError, text_of(content)
    ct = text_of(content)
    assert "E2E-BODY-1" in ct and "E2E-BODY-2" in ct


async def test_send_blocked_by_default(readonly_addon):
    before = inbox_subjects("bob@test.local", "fake-app-pw-bob")
    res = await call("send_email", {"account_name": "alice", "recipients": ["bob@test.local"],
                                    "subject": "SHOULD-NOT-ARRIVE", "body": "x"})
    assert res.isError and "Mutation class 'send' is not allowed" in text_of(res), text_of(res)
    time.sleep(1)
    assert "SHOULD-NOT-ARRIVE" not in inbox_subjects("bob@test.local", "fake-app-pw-bob")
    assert len(inbox_subjects("bob@test.local", "fake-app-pw-bob")) == len(before)


async def test_draft_blocked_by_default(readonly_addon):
    res = await call("save_draft", {"account_name": "alice", "recipients": [], "subject": "DRAFT-BLOCKED",
                                    "body": "x"})
    assert res.isError and "Mutation class 'draft' is not allowed" in text_of(res), text_of(res)


async def test_organize_and_delete_blocked_and_reads_do_not_mark_seen(readonly_addon):
    for tool, cls in (("mark_emails_as_read", "organize"), ("delete_emails", "delete"),
                      ("move_emails", "organize")):
        args = {"account_name": "alice", "email_ids": ["1"]}
        if tool == "move_emails":
            args["destination_mailbox"] = "Drafts"
        res = await call(tool, args)
        assert res.isError, (tool, text_of(res))
        assert "not allowed" in text_of(res) or "validation" in text_of(res).lower(), (tool, text_of(res))
    im = imaplib.IMAP4(*GM_IMAP)
    im.login("alice@test.local", ALICE_PW)
    im.select("INBOX", readonly=True)
    _, unseen = im.search(None, "UNSEEN")
    _, alln = im.search(None, "ALL")
    im.logout()
    assert len(alln[0].split()) == 2  # nothing deleted
    assert unseen[0].split() == alln[0].split()  # reading did not set \Seen


def test_logs_never_contain_secrets(readonly_addon):
    logs = readonly_addon.logs()
    assert TOKEN not in logs
    assert ALICE_PW not in logs


# ---------- send enabled ----------

@pytest.fixture(scope="module")
def send_addon(readonly_addon):
    readonly_addon.stop()
    a = Addon(options(allow_send=True, allowed_recipients=["bob@test.local"]))
    yield a
    a.stop()


async def test_send_allowed_recipient_delivers(send_addon):
    res = await call("send_email", {"account_name": "alice", "recipients": ["bob@test.local"],
                                    "subject": "E2E-ALLOWED-SEND", "body": "hi bob"})
    assert not res.isError, text_of(res)
    time.sleep(1.5)
    assert "E2E-ALLOWED-SEND" in inbox_subjects("bob@test.local", "fake-app-pw-bob")


async def test_send_disallowed_recipient_blocked(send_addon):
    res = await call("send_email", {"account_name": "alice", "recipients": ["mallory@evil.test"],
                                    "subject": "E2E-DISALLOWED", "body": "x"})
    print("disallowed-recipient result:", text_of(res))
    assert res.isError, text_of(res)
    assert "mallory" not in " ".join(inbox_subjects("bob@test.local", "fake-app-pw-bob"))


async def test_draft_allowed_when_send_enabled(send_addon):
    res = await call("save_draft", {"account_name": "alice", "recipients": ["bob@test.local"],
                                    "subject": "E2E-DRAFT", "body": "draft body"})
    assert not res.isError, text_of(res)
    im = imaplib.IMAP4(*GM_IMAP)
    im.login("alice@test.local", ALICE_PW)
    im.select("Drafts")
    _, data = im.search(None, "SUBJECT", "E2E-DRAFT")
    im.logout()
    assert data[0].split()


def test_send_mode_logs_never_contain_secrets(send_addon):
    logs = send_addon.logs()
    assert TOKEN not in logs and ALICE_PW not in logs


# ---------- generated token ----------

def test_generated_token_persisted_and_not_logged(send_addon):
    send_addon.stop()
    a = Addon(options(bearer_token=""))
    try:
        tok_file = a.data / "bearer_token"
        # file is root-owned 0600 inside the bind mount
        r = subprocess.run([*DOCKER, "exec", NAME, "sh", "-c", "stat -c %a /data/bearer_token; cat /data/bearer_token"],
                           capture_output=True, text=True, check=True)
        mode, token = r.stdout.split("\n", 1)
        token = token.strip()
        assert mode == "600" and len(token) >= 60
        logs = a.logs()
        assert "Generated a new random bearer token" in logs
        assert token not in logs
        ok = httpx.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
        assert ok.status_code == 200
        assert httpx.post(URL, json={}, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
        # Restart keeps the same token
        docker("restart", NAME)
        time.sleep(4)
        assert "Using previously generated bearer token" in a.logs()
        ok2 = httpx.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
        assert ok2.status_code == 200
        assert tok_file.exists()
    finally:
        a.stop()
