"""Unit tests for the add-on entrypoint's option handling (no Docker needed)."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

RUN = Path(__file__).resolve().parents[2] / "email_mcp/rootfs/opt/email_mcp/run.py"


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    spec = importlib.util.spec_from_file_location("email_mcp_run", RUN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["email_mcp_run"] = mod
    spec.loader.exec_module(mod)
    return mod


def acct(**kw):
    base = {"name": "me", "email": "me@icloud.com", "provider": "icloud", "password": "fake-pw"}
    base.update(kw)
    return base


def test_icloud_preset(run):
    cfg = run.build_config({"accounts": [acct()]})
    e = cfg["emails"][0]
    assert e["incoming"] == {"user_name": "me@icloud.com", "password": "fake-pw", "host": "imap.mail.me.com",
                             "port": 993, "use_ssl": True, "start_ssl": False, "verify_ssl": True}
    assert (e["outgoing"]["host"], e["outgoing"]["port"], e["outgoing"]["use_ssl"], e["outgoing"]["start_ssl"]) == (
        "smtp.mail.me.com", 587, False, True)


def test_yahoo_preset(run):
    e = run.build_config({"accounts": [acct(email="me@yahoo.com", provider="yahoo")]})["emails"][0]
    assert (e["incoming"]["host"], e["incoming"]["port"], e["incoming"]["use_ssl"]) == ("imap.mail.yahoo.com", 993, True)
    assert (e["outgoing"]["host"], e["outgoing"]["port"], e["outgoing"]["use_ssl"], e["outgoing"]["start_ssl"]) == (
        "smtp.mail.yahoo.com", 465, True, False)


def test_overrides_and_usernames(run):
    e = run.build_config({"accounts": [acct(username="me", smtp_username="me@icloud.com", imap_port=1993,
                                            smtp_security="ssl", smtp_port=465, verify_ssl=False)]})["emails"][0]
    assert e["incoming"]["user_name"] == "me" and e["incoming"]["port"] == 1993
    assert e["outgoing"]["user_name"] == "me@icloud.com"
    assert (e["outgoing"]["port"], e["outgoing"]["use_ssl"], e["outgoing"]["start_ssl"]) == (465, True, False)
    assert e["incoming"]["verify_ssl"] is False


def test_custom_requires_imap_host_and_smtp_optional(run):
    with pytest.raises(run.ConfigError):
        run.build_config({"accounts": [acct(provider="custom")]})
    e = run.build_config({"accounts": [acct(provider="custom", imap_host="imap.example.test")]})["emails"][0]
    assert "outgoing" not in e  # IMAP-only


def test_default_is_read_only(run):
    cfg = run.build_config({"accounts": [acct()], "allowed_recipients": ["x@example.test"]})
    assert cfg["allowed_mutations"] == []
    assert cfg["allowed_recipients"] == []  # ignored while allow_send is false
    assert cfg["enable_attachment_download"] is False


def test_allow_send_grants_only_draft_and_send(run):
    cfg = run.build_config({"accounts": [acct()], "allow_send": True, "allowed_recipients": ["a@b.test", " "]})
    assert cfg["allowed_mutations"] == ["draft", "send"]
    assert cfg["allowed_recipients"] == ["a@b.test"]


def test_duplicate_names_rejected(run):
    with pytest.raises(run.ConfigError):
        run.build_config({"accounts": [acct(), acct()]})
    with pytest.raises(run.ConfigError):
        run.build_config({"accounts": []})


def test_token_generated_persisted_0600_and_reused(run, tmp_path, caplog):
    caplog.set_level("INFO")
    t1 = run.resolve_token({"bearer_token": ""})
    f = tmp_path / "bearer_token"
    assert f.read_text() == t1 and len(t1) >= 60
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert run.resolve_token({}) == t1
    assert t1 not in caplog.text


def test_configured_token_min_length(run):
    with pytest.raises(run.ConfigError):
        run.resolve_token({"bearer_token": "short"})
    assert run.resolve_token({"bearer_token": "x" * 40}) == "x" * 40


def test_push_token_to_options(run, monkeypatch):
    sent = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        sent["url"] = req.full_url
        sent["auth"] = req.headers["Authorization"]
        sent["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-supervisor-token")
    monkeypatch.setattr(run.urllib.request, "urlopen", fake_urlopen)
    tok = run.resolve_token({"accounts": [], "bearer_token": "", "allow_send": False})
    assert sent["url"] == "http://supervisor/addons/self/options"
    assert sent["body"]["options"]["bearer_token"] == tok
    assert sent["body"]["options"]["allow_send"] is False


def test_blank_optional_fields_keep_presets(run):
    e = run.build_config({"accounts": [acct(
        full_name="", username="", smtp_username="",
        imap_host="", imap_port="", imap_security="",
        smtp_host="", smtp_port="", smtp_security="",
        verify_ssl="", save_to_sent="", drafts_mailbox="",
        sent_folder_name="",
    )]})["emails"][0]
    assert e["incoming"]["host"] == "imap.mail.me.com"
    assert e["incoming"]["port"] == 993
    assert e["incoming"]["user_name"] == "me@icloud.com"
    assert e["incoming"]["verify_ssl"] is True
    assert (e["outgoing"]["host"], e["outgoing"]["port"], e["outgoing"]["start_ssl"]) == (
        "smtp.mail.me.com", 587, True)
    assert e["save_to_sent"] is True
    assert "drafts_mailbox" not in e
    assert "sent_folder_name" not in e


def test_security_defaults_stay_read_only(run):
    cfg = run.build_config({"accounts": [acct()]})
    assert cfg["enable_attachment_download"] is False
    assert cfg["enable_attachment_content"] is False
    assert cfg["allowed_senders"] == []
    assert cfg["report_blocked_mutations"] is True
    assert cfg["allowed_mutations"] == []
    cfg = run.build_config({
        "accounts": [acct(sent_folder_name="Sent Items")],
        "allow_send": True,
        "allowed_recipients": ["a@example.com"],
        "allowed_senders": ["boss@example.com"],
        "enable_attachment_content": True,
        "report_blocked_mutations": False,
    })
    assert cfg["emails"][0]["sent_folder_name"] == "Sent Items"
    assert cfg["allowed_senders"] == ["boss@example.com"]
    assert cfg["allowed_recipients"] == ["a@example.com"]
    assert cfg["enable_attachment_content"] is True
    assert cfg["enable_attachment_download"] is False
    assert cfg["report_blocked_mutations"] is False
