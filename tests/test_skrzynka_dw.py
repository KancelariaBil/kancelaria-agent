"""Skrzynka DW: mail z Moniką w DW → panel kancelarii. Dane fikcyjne."""

import pytest

from kancelaria_agent import dw, guards
from kancelaria_agent.agent import Agent
from kancelaria_agent.integrations import IncomingMail, build_mock
from kancelaria_agent.integrations.panel import PanelError
from kancelaria_agent.planner import MockPlanner

ME = "monika@kancelariabil.pl"
TEAM = {"basia@kancelariabil.pl", "aneta@kancelariabil.pl"}
BODY = ("Dzień dobry,\nw załączeniu przesyłam pismo w sprawie 2026/0046.\n"
        "Rachunek: PL61 1090 1014 0000 0712 1981 2874\nZ poważaniem\n\n"
        "W dniu 1.11.2026 Jan napisał:\n> stara treść")


def dw_mail(**kw):
    base = dict(message_id="<dw1@example.com>", sender="basia@kancelariabil.pl", subject="Pismo w sprawie najmu",
                body=BODY, trusted=True, to=("Jan Testowy <jan@firma-testowa.pl>",), cc=(ME,),
                date="Mon, 23 Nov 2026 10:00:00 +0100")
    base.update(kw)
    return IncomingMail(**base)


def payload(m):
    return {"message_id": m.message_id, "sender": m.sender, "subject": m.subject, "body": m.body,
            "trusted": m.trusted, "to": list(m.to), "cc": list(m.cc), "date": m.date}


def test_detects_dw_mail():
    assert dw.is_dw_mail(payload(dw_mail()), ME, TEAM)


@pytest.mark.parametrize("kw", [
    {"to": (ME,), "cc": ()},                       # Monika w „Do” = zlecenie dla niej
    {"cc": ()},                                    # Moniki nie ma w DW
    {"to": ("aneta@kancelariabil.pl",)},           # tylko wewnątrz zespołu
    {"trusted": False},                            # nadawca niezweryfikowany
])
def test_not_dw(kw):
    assert not dw.is_dw_mail(payload(dw_mail(**kw)), ME, TEAM)


def test_extract_strips_quote_and_accounts():
    d = dw.extract_dw(payload(dw_mail()))
    assert "stara treść" not in d["body"] and "PL61" not in d["body"] and "[numer rachunku]" in d["body"]
    assert d["cc"] == [ME] and d["to"] == ["Jan Testowy <jan@firma-testowa.pl>"]


def test_guard(make_env):
    cfg, *_ = make_env()
    ok = {"message_id": "<x>", "trusted_sender": True, "files": ["abc/pismo.pdf"]}
    assert guards.evaluate(cfg, guards.PANEL_DW, ok).verdict is guards.Verdict.ALLOW
    assert guards.evaluate(cfg, guards.PANEL_DW, {**ok, "trusted_sender": False}).verdict is guards.Verdict.DENY
    assert guards.evaluate(cfg, guards.PANEL_DW, {**ok, "message_id": ""}).verdict is guards.Verdict.DENY
    for bad in ("../x/y.pdf", "/etc/passwd", "a/b/c.pdf", "plik.pdf"):
        assert guards.evaluate(cfg, guards.PANEL_DW, {**ok, "files": [bad]}).verdict is guards.Verdict.DENY


def make_agent(make_env, mails, monkeypatch, **ov):
    monkeypatch.setenv("AGENT_EMAIL", ME)
    cfg, store, audit, _, _ = make_env(**ov)
    integ = build_mock(mails)
    return cfg, store, integ, Agent(cfg, store, audit, integ, MockPlanner())


def test_dw_mail_goes_to_panel_without_any_mail(make_env, monkeypatch):
    m = dw_mail(files=(("pismo.pdf", b"%PDF-1.4 test"), ("skrypt.exe", b"MZ")))
    cfg, store, integ, agent = make_agent(make_env, [m], monkeypatch, dry_run=False)
    agent.tick()
    assert len(integ.panel.dw) == 1 and integ.panel.offers == []
    d = integ.panel.dw[0]
    assert d["message_id"] == "<dw1@example.com>" and d["from"] == "basia@kancelariabil.pl"
    assert len(d["files"]) == 1 and d["files"][0].endswith("/pismo.pdf")
    assert open(d["file_paths"][0], "rb").read() == b"%PDF-1.4 test"
    assert integ.mail.sent == [] and integ.board.tasks == {}


def test_offer_in_dw_still_goes_to_offers(make_env, monkeypatch):
    m = dw_mail(subject="Oferta – umowa najmu", body="Przesyłam ofertę: 2 500 zł netto.")
    cfg, store, integ, agent = make_agent(make_env, [m], monkeypatch, dry_run=False)
    agent.tick()
    assert len(integ.panel.offers) == 1 and integ.panel.dw == []


def test_dw_processed_once(make_env, monkeypatch):
    cfg, store, integ, agent = make_agent(make_env, [dw_mail()], monkeypatch, dry_run=False)
    agent.tick(); agent.tick()
    assert len(integ.panel.dw) == 1


def test_dw_in_dry_run_is_only_a_proposal(make_env, monkeypatch):
    cfg, store, integ, agent = make_agent(make_env, [dw_mail()], monkeypatch)
    agent.tick()
    assert [p["action_kind"] for p in store.list_proposals()] == [guards.PANEL_DW]
    assert integ.panel.dw == []


def test_panel_failure_tells_the_sender(make_env, monkeypatch):
    cfg, store, integ, agent = make_agent(make_env, [dw_mail()], monkeypatch, dry_run=False)

    class Down:
        def add_dw(self, *a):
            raise PanelError("panel niedostępny")

    integ.panel = Down()
    agent.tick()
    assert len(integ.mail.sent) == 1
    assert integ.mail.sent[0]["to"] == ["basia@kancelariabil.pl"] and "Skrzynce DW" in integ.mail.sent[0]["subject"]
