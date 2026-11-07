"""Baza wiedzy: zapis, wyszukiwanie, narzędzia Moniki, mail „wiedza” i panel WWW. Wyłącznie dane fikcyjne."""

import asyncio
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

import pytest

from kancelaria_agent.agent import Agent, _service_subject
from kancelaria_agent.documents import build_docx
from kancelaria_agent.integrations import IncomingMail, build_mock
from kancelaria_agent.wiedza import KnowledgeBase, KnowledgeError, fold
from kancelaria_agent.www import make_handler, parse_multipart

BASIA = "basia@kancelariabil.pl"
TXT = ("Procedura: zwyczajne zgromadzenie wspólników spółki z o.o. zwołuje zarząd listami poleconymi "
       "co najmniej dwa tygodnie przed terminem (art. 238 § 1 k.s.h.).").encode()


def test_save_versions_and_no_duplicates(tmp_path):
    kb = KnowledgeBase(tmp_path / "wiedza")
    assert kb.save("Zgromadzenie Wspólników.txt", TXT) == "zgromadzenie-wspolnikow.txt"
    assert kb.save("Zgromadzenie Wspólników.txt", TXT) == "zgromadzenie-wspolnikow.txt"   # ten sam plik
    assert kb.save("Zgromadzenie Wspólników.txt", b"inna wersja") == "zgromadzenie-wspolnikow-2.txt"
    assert [e.name for e in kb.entries()] == ["zgromadzenie-wspolnikow-2.txt", "zgromadzenie-wspolnikow.txt"]


@pytest.mark.parametrize("name,data", [("wirus.exe", b"x"), ("pusty.txt", b""), ("bez", b"x")])
def test_save_rejects(tmp_path, name, data):
    with pytest.raises(KnowledgeError):
        KnowledgeBase(tmp_path).save(name, data)


@pytest.mark.parametrize("name", ["../config.yaml", ".tekst", "a/b.txt", "brak.txt", ""])
def test_path_stays_inside(tmp_path, name):
    (tmp_path / "config.yaml").write_text("x")
    kb = KnowledgeBase(tmp_path / "wiedza")
    kb.save("a.txt", b"tresc")
    with pytest.raises(KnowledgeError):
        kb.path(name)


def test_search_folds_polish_and_ranks(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.save("zgromadzenie.txt", TXT)
    kb.save("fundacja.txt", "Fundacja rodzinna – statut, beneficjenci.".encode())
    build_docx("# Notatka\nZgromadzenie wspólników – protokół i uchwały.", tmp_path / "protokol.docx")
    hits = kb.search("zgromadzenie wspolnikow")
    assert [h.name for h in hits][:2] == ["zgromadzenie.txt", "protokol.docx"] or \
        {h.name for h in hits} == {"zgromadzenie.txt", "protokol.docx"}
    assert "fundacja.txt" not in {h.name for h in hits}
    assert any("238" in s for s in hits[0].snippets) or hits[0].name == "protokol.docx"
    assert kb.search("ab") == []                       # za krótkie słowa
    assert fold("Łódź ŻÓŁW") == "lodz zolw"


def test_delete_removes_cache(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.save("a.txt", TXT)
    kb.search("zgromadzenie")
    assert (tmp_path / ".tekst" / "a.txt.txt").exists()
    kb.delete("a.txt")
    assert kb.entries() == [] and not (tmp_path / ".tekst" / "a.txt.txt").exists()


def test_planner_knowledge_tools(make_env):
    pytest.importorskip("claude_agent_sdk")
    from kancelaria_agent.planner_agent import AgentPlanner
    cfg, *_ = make_env()
    KnowledgeBase(cfg.knowledge_dir).save("zgromadzenie.txt", TXT)
    planner = AgentPlanner(BASIA, api_key="k", knowledge_dir=cfg.knowledge_dir)
    tools = {t.name: t for t in planner._tools(None)}
    run = lambda name, args: asyncio.run(tools[name].handler(args))   # noqa: E731
    found = run("wiedza_szukaj", {"zapytanie": "zgromadzenie"})["content"][0]["text"]
    assert "zgromadzenie.txt" in found and "238" in found
    assert "zgromadzenie.txt" in run("wiedza_szukaj", {})["content"][0]["text"]
    assert "238" in run("wiedza_czytaj", {"plik": "zgromadzenie.txt"})["content"][0]["text"]
    assert run("wiedza_czytaj", {"plik": "../config.yaml"}).get("is_error")


@pytest.mark.parametrize("subject,expected", [("wiedza", "wiedza"), ("Re: Fwd: WIEDZA ", "wiedza"),
                                              ("Odp: diagnostyka", "diagnostyka"), ("wiedza o spółkach", "wiedza o spółkach")])
def test_service_subject(subject, expected):
    assert _service_subject({"subject": subject}) == expected


def _agent(make_env, mails):
    from kancelaria_agent.planner import MockPlanner
    cfg, store, audit, _, _ = make_env(dry_run=False)
    integ = build_mock(mails)
    return cfg, store, integ, Agent(cfg, store, audit, integ, MockPlanner())


def test_mail_wiedza_adds_files_without_model(make_env):
    mail = IncomingMail("w1", BASIA, "wiedza", "", attachments=("Procedura.txt", "program.exe"),
                        files=(("Procedura.txt", TXT), ("program.exe", b"MZ")))
    cfg, store, integ, agent = _agent(make_env, [mail])
    assert agent.tick() == "ok"
    assert [e.name for e in KnowledgeBase(cfg.knowledge_dir).entries()] == ["procedura.txt"]
    sent = integ.mail.sent[-1]
    assert sent["to"] == [BASIA] and "procedura.txt" in sent["body"] and "Pominięte" in sent["body"]
    assert not (cfg.projects_dir.parent / "przychodzace").exists() or \
        not any((cfg.projects_dir.parent / "przychodzace").iterdir())


def test_mail_wiedza_from_stranger_is_not_imported(make_env):
    mail = IncomingMail("w2", "obcy@example.com", "wiedza", "", trusted=False,
                        files=(("a.txt", TXT),))
    cfg, store, integ, agent = _agent(make_env, [mail])
    agent.tick()
    assert KnowledgeBase(cfg.knowledge_dir).entries() == []


# ---- panel WWW ----
@pytest.fixture
def panel(tmp_path):
    kb = KnowledgeBase(tmp_path / "wiedza")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kb))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield kb, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _post(url, data, ctype):
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype}, method="POST")
    try:
        return urllib.request.build_opener(NoRedirect).open(req)
    except urllib.error.HTTPError as e:
        return e


def _multipart(files):
    b = "----granica"
    body = b"".join(
        f'--{b}\r\nContent-Disposition: form-data; name="pliki"; filename="{n}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + d + b"\r\n" for n, d in files)
    return body + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def test_panel_upload_search_download_delete(panel):
    kb, url = panel
    body, ctype = _multipart([("Zgromadzenie Wspólników.txt", TXT), ("x.exe", b"MZ")])
    r = _post(url + "/dodaj", body, ctype)
    assert r.status == 303 and "Dodano" in urllib.parse.unquote(r.headers["Location"])
    assert [e.name for e in kb.entries()] == ["zgromadzenie-wspolnikow.txt"]
    page = urllib.request.urlopen(url + "/?q=" + quote("zgromadzenie wspólników")).read().decode()
    assert "zgromadzenie-wspolnikow.txt" in page and "238" in page
    assert urllib.request.urlopen(url + "/plik/zgromadzenie-wspolnikow.txt").read() == TXT
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(url + "/plik/" + quote("../../config.yaml"))
    r = _post(url + "/usun", b"plik=zgromadzenie-wspolnikow.txt", "application/x-www-form-urlencoded")
    assert r.status == 303 and kb.entries() == []
    assert urllib.request.urlopen(url + "/zdrowie").read() == b"ok"


def test_parse_multipart_keeps_polish_names():
    body, ctype = _multipart([("Umowa spółki.pdf", b"%PDF")])
    assert parse_multipart(ctype, body) == [("Umowa spółki.pdf", b"%PDF")]
