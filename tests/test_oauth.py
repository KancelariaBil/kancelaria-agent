import base64
import json
import threading
import time
import urllib.parse
import urllib.request

import pytest

from kancelaria_agent.integrations import gmail as gmail_mod
from kancelaria_agent.integrations import oauth
from kancelaria_agent.integrations.gmail import GmailMail, GmailSettings, MailConfigError


def _id_token(email, verified=True):
    claims = base64.urlsafe_b64encode(json.dumps({"email": email, "email_verified": verified}).encode()).decode()
    return f"x.{claims.rstrip('=')}.sig"


def _env(tmp_path, **kv):
    base = {"AGENT_EMAIL": "marek@brandwise.pl", "IMAP_HOST": "imap.gmail.com",
            "SMTP_HOST": "smtp.gmail.com", "OWNER_EMAIL": "lukasz@brandwise.pl"}
    base.update(kv)
    f = tmp_path / ".env"
    f.write_text("".join(f"{k}={v}\n" for k, v in base.items()))
    return f


def test_settings_prefer_oauth_and_hide_secrets(tmp_path, monkeypatch):
    for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "AGENT_EMAIL_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    s = GmailSettings.from_env(_env(tmp_path, GMAIL_CLIENT_ID="cid", GMAIL_CLIENT_SECRET="sekret-xyz",
                                    GMAIL_REFRESH_TOKEN="1//refresh-abc"))
    assert s.uses_oauth and "sekret-xyz" not in repr(s) and "refresh-abc" not in repr(s)


def test_settings_require_some_credentials(tmp_path, monkeypatch):
    for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "AGENT_EMAIL_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(MailConfigError):
        GmailSettings.from_env(_env(tmp_path, GMAIL_CLIENT_ID="cid"))


def test_app_password_still_supported(tmp_path, monkeypatch):
    for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    s = GmailSettings.from_env(_env(tmp_path, AGENT_EMAIL_APP_PASSWORD="abcd efgh ijkl mnop"))
    assert not s.uses_oauth and s.password == "abcdefghijklmnop"


def test_token_source_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(oauth, "_post", lambda url, data: calls.append(data) or {"access_token": "t1", "expires_in": 3600})
    ts = oauth.TokenSource("cid", "sec", "ref")
    assert ts.access_token() == "t1" and ts.access_token() == "t1" and len(calls) == 1
    assert calls[0]["grant_type"] == "refresh_token" and "sec" not in repr(ts)


def test_xoauth2_imap_payload(monkeypatch):
    s = GmailSettings("marek@brandwise.pl", "", "imap.test", "smtp.test", "lukasz@brandwise.pl", "c", "s", "r")
    g = GmailMail(s)
    monkeypatch.setattr(g._tokens, "access_token", lambda: "TOK")
    assert g._xoauth2() == b"user=marek@brandwise.pl\x01auth=Bearer TOK\x01\x01"


def test_id_token_email_requires_verified():
    assert oauth.id_token_email(_id_token("Marek@Brandwise.pl")) == "marek@brandwise.pl"
    with pytest.raises(oauth.OAuthError):
        oauth.id_token_email(_id_token("marek@brandwise.pl", verified=False))


def test_set_env_value_replaces_and_restricts(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\nGMAIL_REFRESH_TOKEN=old\n")
    oauth.set_env_value(f, "GMAIL_REFRESH_TOKEN", "new")
    assert f.read_text().count("GMAIL_REFRESH_TOKEN") == 1 and "new" in f.read_text() and "A=1" in f.read_text()
    assert (f.stat().st_mode & 0o777) == 0o600


def _run_setup_flow(tmp_path, monkeypatch, email, port):
    env_file = tmp_path / ".env"
    env_file.write_text("AGENT_EMAIL=marek@brandwise.pl\n")
    monkeypatch.setattr(oauth, "_post", lambda url, data: {
        "access_token": "a", "refresh_token": "1//R", "id_token": _id_token(email)})
    out = {}

    def target():
        try:
            out["email"] = oauth.run_setup("cid", "sec", "marek@brandwise.pl", env_file, port=port, timeout_s=10)
        except oauth.OAuthError as e:
            out["error"] = str(e)

    t = threading.Thread(target=target)
    t.start()
    time.sleep(0.3)
    # "/" przekierowuje do Google z PKCE i podpowiedzią konta
    req = urllib.request.Request(f"http://127.0.0.1:{port}/")
    opener = urllib.request.build_opener(type("NoRedir", (urllib.request.HTTPRedirectHandler,), {
        "redirect_request": lambda *a, **k: None}))
    try:
        opener.open(req)
    except urllib.error.HTTPError as e:
        loc = e.headers["Location"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    assert q["code_challenge_method"] == ["S256"] and q["login_hint"] == ["marek@brandwise.pl"]
    cb = f"http://127.0.0.1:{port}/callback?" + urllib.parse.urlencode({"state": q["state"][0], "code": "C"})
    try:
        urllib.request.urlopen(cb)
    except urllib.error.HTTPError:
        pass
    t.join(5)
    return out, env_file.read_text()


def test_setup_saves_token_for_agent_account(tmp_path, monkeypatch):
    out, env = _run_setup_flow(tmp_path, monkeypatch, "marek@brandwise.pl", 18765)
    assert out == {"email": "marek@brandwise.pl"} and "GMAIL_REFRESH_TOKEN=1//R" in env


def test_setup_refuses_wrong_account(tmp_path, monkeypatch):
    out, env = _run_setup_flow(tmp_path, monkeypatch, "lukasz@brandwise.pl", 18766)
    assert "lukasz@brandwise.pl" in out["error"] and "GMAIL_REFRESH_TOKEN" not in env
