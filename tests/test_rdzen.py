import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from kancelaria_agent import guards
from kancelaria_agent.agent import Agent
from kancelaria_agent.integrations import IncomingMail, build_mock
from kancelaria_agent.planner import PROJECT_UPDATE, PlannedAction
from kancelaria_agent.projects import PathError, Projects, resolve_inside, slugify, strip_quoted, tag_in

OWNER = "basia@kancelariabil.pl"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


class ScriptedPlanner:
    """Planer testowy: zwraca zaplanowane akcje i zapamiętuje wywołania."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def plan(self, kind, payload):
        self.calls.append((kind, payload))
        return self.responses.pop(0) if self.responses else []


def mail(mid, subject="Baner dla Kowalski", body="Zrób baner", **kw):
    return IncomingMail(mid, OWNER, subject, body, trusted=True, **kw)


def reply(text="Gotowe", status="gotowe", klient="", projekt="", files=None):
    acts = [PlannedAction(PROJECT_UPDATE, {"klient": klient, "projekt": projekt, "status": status})]
    p = {"to": OWNER, "subject": "Re: Baner", "body": text}
    if files:
        p["project_attachments"] = files
    return acts + [PlannedAction(guards.SEND_MAIL, p)]


@pytest.fixture
def env(make_env):
    def _make(mails, planner, **ov):
        cfg, store, audit, _, _ = make_env(dry_run=False, guardrails={"allowed_recipients": [OWNER]}, **ov)
        integ = build_mock(mails)
        agent = Agent(cfg, store, audit, integ, planner)
        return cfg, store, integ, agent
    return _make


# ---------- projekty i wątki ----------

def test_slugify_polish_and_dns_safe():
    assert slugify("Zakład Pogrzebowy Łódź – strona WWW!") == "zaklad-pogrzebowy-lodz-strona-www"
    assert len(slugify("a" * 200)) <= 50


@pytest.mark.parametrize("rel", ["../x", "a/../../b", "/etc/passwd", ".env", "site/.git/config", "a\\..\\b", ""])
def test_resolve_inside_rejects(tmp_path, rel):
    with pytest.raises(PathError):
        resolve_inside(tmp_path, rel)


def test_resolve_inside_rejects_symlink_escape(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "link").symlink_to("/etc")
    with pytest.raises(PathError):
        resolve_inside(tmp_path, "site/link/passwd")


def test_strip_quoted_and_tag():
    assert strip_quoted("Popraw logo\n\nW dniu pon., 6 paź napisał Marek:\n> stare") == "Popraw logo"
    assert tag_in("Re: Re: Baner [#12]") == 12 and tag_in("Baner") is None






def test_untrusted_mail_gets_no_project(env):
    planner = ScriptedPlanner([])
    evil = IncomingMail("<e@x>", "obcy@x.pl", "Baner [#1]", "x", trusted=False)
    cfg, store, integ, agent = env([evil], planner)
    agent.tick()
    assert "project_id" not in planner.calls[0][1]
    assert store.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0








# ---------- załączniki ----------

def _export(cfg, slug="kowalski-dental-baner", name="baner.png", size=100):
    d = cfg.projects_dir / slug / "exports"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"\x89PNG" + b"0" * size)
    return f"{slug}/exports/{name}"




@pytest.mark.parametrize("rel", ["../config.yaml", "p/../../.env", "/etc/passwd", "p/inne/index.html",
                                 "p/pisma/brak.docx", "p/pisma/skrypt.sh", "p"])
def test_attachment_guard_denies(make_env, rel):
    cfg, *_ = make_env()
    d = cfg.projects_dir / "p"
    (d / "exports").mkdir(parents=True)
    (d / "site").mkdir()
    (d / "site" / "index.html").write_text("x")
    (d / "exports" / "skrypt.sh").write_text("x")
    d2 = guards.evaluate(cfg, guards.SEND_MAIL, {"to": "ala@example.com", "subject": "x", "body": "x",
                                                  "attachments": [rel]})
    assert d2.verdict is guards.Verdict.DENY




# ---------- narzędzia projektu ----------









# ---------- planer-agent z projektem ----------



# ---------- poczta ----------













def test_store_migration_adds_public_id(tmp_path):
    import sqlite3
    from kancelaria_agent.store import Store
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL,
        client TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', named INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active', sender TEXT NOT NULL, reply_to TEXT, subject TEXT NOT NULL DEFAULT '',
        last_message_id TEXT, figma_files TEXT NOT NULL DEFAULT '[]', upload_token TEXT UNIQUE NOT NULL,
        token_expires TEXT NOT NULL, last_upload_at TEXT, upload_done_at TEXT, resume_seq INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO projects(slug, sender, upload_token, token_expires, created_at, updated_at)
        VALUES ('glina', 'a@b.pl', 't', 'x', 'x', 'x');""")
    con.close()
    s = Store(db)
    row = s.get_project(1)
    assert len(row["public_id"]) == 6 and row["stage"] == "nowy"


def test_subject_helpers():
    from kancelaria_agent.projects import base_subject, is_reply_subject, reply_subject
    assert reply_subject("Re: Re: RE: Baner") == "Re: Baner"
    assert reply_subject("Fwd: Landing") == "Re: Fwd: Landing"
    assert base_subject("Odp: Re: Fwd: Landing [#3]") == "landing"
    assert is_reply_subject("Odp: x") and not is_reply_subject("Fwd: x")


def test_reply_without_thread_headers_matched_by_subject(env):
    planner = ScriptedPlanner(reply(), reply(), reply())
    mails = [mail("<m1@x>", subject="LUX MED – banery"),
             mail("<m2@x>", subject="Re: LUX MED – banery", body="Akceptują"),   # bez In-Reply-To
             mail("<m3@x>", subject="LUX MED – banery", body="nowe zlecenie")]    # nie odpowiedź → nowy projekt
    cfg, store, integ, agent = env(mails, planner)
    agent.tick()
    assert [c[1]["project_id"] for c in planner.calls] == [1, 1, 2]
    assert all(m["subject"] == "Re: Baner" for m in integ.mail.sent)  # bez narastającego „Re: Re:”


def test_history_keeps_forwarded_client_mail():
    body = "Zerknij.\n\n---------- Forwarded message ---------\nOd: Kasia\n\nChcę wizytówkę Google.\n\nW dniu wt. napisał X:\n> stare"
    out = strip_quoted(body)
    assert "Chcę wizytówkę Google" in out and "stare" not in out


def test_site_screenshot_rejects_outside_paths(tmp_path):
    import asyncio
    pytest.importorskip("playwright")
    from kancelaria_agent.site_preview import PreviewError, screenshot
    from kancelaria_agent.projects import PathError
    (tmp_path / "site").mkdir()
    with pytest.raises((PreviewError, PathError)):
        asyncio.run(screenshot(tmp_path / "site", "../config.yaml"))
    with pytest.raises(PreviewError):
        asyncio.run(screenshot(tmp_path / "site", "index.html", "tablet"))






def test_untrusted_diagnostics_not_answered_with_report(env):
    planner = ScriptedPlanner([])
    evil = IncomingMail("<e@x>", "obcy@x.pl", "diagnostyka", "x", trusted=False)
    cfg, store, integ, agent = env([evil], planner)
    agent.tick()
    assert planner.calls and not any("Przeglądarka" in m["body"] for m in integ.mail.sent)






