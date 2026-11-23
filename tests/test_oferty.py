"""Oferty w DW → panel kancelarii. Dane fikcyjne."""

import json

import pytest

from kancelaria_agent import guards, oferty
from kancelaria_agent.agent import Agent
from kancelaria_agent.integrations import IncomingMail, build_mock
from kancelaria_agent.integrations.gmail import parse_message
from kancelaria_agent.integrations.panel import HttpPanel, PanelError, panel_base_url
from kancelaria_agent.planner import MockPlanner

ME = "monika@kancelariabil.pl"
TEAM = {"ala@example.com", "basia@kancelariabil.pl", "aneta@kancelariabil.pl"}
BODY = ("Dzień dobry,\nw nawiązaniu do rozmowy przesyłam ofertę na przygotowanie umowy najmu.\n"
        "Wynagrodzenie: 2 500 zł netto. Oferta jest ważna do 3.12.2026.\n"
        "Rachunek: PL61 1090 1014 0000 0712 1981 2874\nZ poważaniem\n\n"
        "W dniu 1.11.2026 Jan napisał:\n> stara treść 9 999 zł")


def offer_mail(**kw):
    base = dict(message_id="<of1@example.com>", sender="basia@kancelariabil.pl",
                subject="Oferta – umowa najmu lokalu", body=BODY, trusted=True,
                to=("Jan Testowy <jan@firma-testowa.pl>",), cc=(ME,), date="Thu, 19 Nov 2026 10:00:00 +0100")
    base.update(kw)
    return IncomingMail(**base)


def payload(mail):
    return {"message_id": mail.message_id, "sender": mail.sender, "subject": mail.subject, "body": mail.body,
            "trusted": mail.trusted, "to": list(mail.to), "cc": list(mail.cc), "date": mail.date}


# ---- rozpoznanie ----

def test_detects_offer_with_monika_in_cc():
    assert oferty.is_dw_offer(payload(offer_mail()), ME, TEAM)


@pytest.mark.parametrize("kw", [
    {"to": (ME,), "cc": ()},                                    # Monika w „Do” = zlecenie dla niej
    {"cc": ()},                                                 # Moniki nie ma w DW
    {"to": ("aneta@kancelariabil.pl",)},                        # tylko wewnątrz zespołu
    {"subject": "Umowa najmu", "body": "Przesyłam projekt umowy."},   # to nie oferta
    {"trusted": False},                                         # nadawca niezweryfikowany
])
def test_not_an_offer(kw):
    assert not oferty.is_dw_offer(payload(offer_mail(**kw)), ME, TEAM)


def test_word_only_in_quote_does_not_count():
    m = offer_mail(subject="Re: termin", body="Potwierdzam termin.\n\n> w załączniku oferta")
    assert not oferty.is_dw_offer(payload(m), ME, TEAM)


# ---- odczyt danych ----

def test_extracts_offer_fields_and_redacts_accounts():
    d = oferty.extract_offer(payload(offer_mail()), ME, TEAM)
    assert d["title"] == "umowa najmu lokalu"
    assert d["amount"] == 2500.0                    # z treści, nie z cytatu
    assert d["form"] == "jednorazowo"
    assert d["valid_until"] == "2026-12-03"
    assert d["client_name"] == "Jan Testowy" and d["client_email"] == "jan@firma-testowa.pl"
    assert "PL61" not in d["body"] and "[numer rachunku]" in d["body"]
    assert "stara treść" not in d["body"]
    assert guards.contains_payment_data(d) is None


@pytest.mark.parametrize("text,amount,form", [
    ("Ryczałt 1.500,50 zł netto miesięcznie", 1500.5, "miesięczny ryczałt"),
    ("Stawka godzinowa 400 zł netto za godzinę", 400.0, "stawka godzinowa"),
    ("Wynagrodzenie 3000 PLN plus success fee 5%", 3000.0, "success fee"),
    ("Szczegóły w załączniku.", None, "jednorazowo"),
])
def test_amount_and_form(text, amount, form):
    d = oferty.extract_offer(payload(offer_mail(body="Przesyłam ofertę. " + text)), ME, TEAM)
    assert d["amount"] == amount and d["form"] == form


# ---- bezpieczniki ----

def test_guard_requires_trusted_sender_and_safe_file(make_env):
    cfg, *_ = make_env()
    ok = {"message_id": "<a@b>", "title": "x", "trusted_sender": True}
    assert guards.evaluate(cfg, guards.PANEL_OFFER, ok).verdict is guards.Verdict.ALLOW
    assert guards.evaluate(cfg, guards.PANEL_OFFER, {**ok, "trusted_sender": False}).verdict is guards.Verdict.DENY
    assert guards.evaluate(cfg, guards.PANEL_OFFER, {**ok, "title": ""}).verdict is guards.Verdict.DENY
    for bad in ("../x/y.pdf", "/etc/passwd", "a/b/c.pdf", "plik.pdf"):
        assert guards.evaluate(cfg, guards.PANEL_OFFER, {**ok, "file": bad}).verdict is guards.Verdict.DENY
    assert guards.evaluate(cfg, guards.PANEL_OFFER, {**ok, "body": "PL61 1090 1014 0000 0712 1981 2874"}).verdict \
        is guards.Verdict.DENY


def test_panel_url_must_be_local():
    assert panel_base_url(None) == "http://127.0.0.1:3000"
    for bad in ("https://panel.kancelariabil.pl", "http://10.0.0.5:3000", "http://evil.test"):
        with pytest.raises(PanelError):
            panel_base_url(bad)
    with pytest.raises(PanelError, match="PANEL_API_KEY"):
        HttpPanel("").add_offer({"message_id": "x"}, None)
    assert "key" not in repr(HttpPanel("k" * 40)).lower()


# ---- pętla agenta ----

def make_agent(make_env, mails, monkeypatch, **ov):
    monkeypatch.setenv("AGENT_EMAIL", ME)
    cfg, store, audit, _, _ = make_env(**ov)
    integ = build_mock(mails)
    return cfg, store, integ, Agent(cfg, store, audit, integ, MockPlanner())


def test_offer_goes_to_panel_without_any_mail(make_env, monkeypatch):
    cfg, store, integ, agent = make_agent(make_env, [offer_mail()], monkeypatch, dry_run=False)
    agent.tick()
    assert len(integ.panel.offers) == 1
    o = integ.panel.offers[0]
    assert o["message_id"] == "<of1@example.com>" and o["amount"] == 2500.0 and o["from"] == "basia@kancelariabil.pl"
    assert integ.mail.sent == [] and integ.board.tasks == {}
    events = [json.loads(l)["event"] for l in cfg.audit_log.read_text(encoding="utf-8").splitlines()]
    assert "offer_detected" in events and "action_executed" in events


def test_offer_in_dry_run_is_only_a_proposal(make_env, monkeypatch):
    cfg, store, integ, agent = make_agent(make_env, [offer_mail()], monkeypatch)
    agent.tick()
    assert [p["action_kind"] for p in store.list_proposals()] == [guards.PANEL_OFFER]
    assert integ.panel.offers == []


def test_offer_processed_once(make_env, monkeypatch):
    cfg, store, integ, agent = make_agent(make_env, [offer_mail()], monkeypatch, dry_run=False)
    agent.tick(); agent.tick()
    assert len(integ.panel.offers) == 1


def test_panel_failure_tells_the_sender(make_env, monkeypatch):
    cfg, store, integ, agent = make_agent(make_env, [offer_mail()], monkeypatch, dry_run=False)

    class Down:
        def add_offer(self, *a):
            raise PanelError("panel niedostępny")

    integ.panel = Down()
    agent.tick()
    assert len(integ.mail.sent) == 1
    sent = integ.mail.sent[0]
    assert sent["to"] == ["basia@kancelariabil.pl"] and "Nie wpisałam oferty" in sent["subject"]


def test_ordinary_cc_mail_goes_to_dw_inbox(make_env, monkeypatch):
    """Mail w DW, który nie jest ofertą, trafia do Skrzynki DW w panelu (nie do ofert, nie do planera)."""
    m = offer_mail(subject="Termin rozprawy", body="Informuję o terminie rozprawy.")
    cfg, store, integ, agent = make_agent(make_env, [m], monkeypatch)
    agent.tick()
    assert integ.panel.offers == []
    assert [p["action_kind"] for p in store.list_proposals()] == [guards.PANEL_DW]


def test_offer_attachment_moved_and_sent(make_env, monkeypatch):
    m = offer_mail(files=(("oferta.pdf", b"%PDF-1.4 test"), ("logo.png", b"png")))
    cfg, store, integ, agent = make_agent(make_env, [m], monkeypatch, dry_run=False)
    agent.tick()
    o = integ.panel.offers[0]
    assert o["file"].endswith("/oferta.pdf")
    assert o["file_path"] and open(o["file_path"], "rb").read() == b"%PDF-1.4 test"
    assert not (cfg.projects_dir.parent / "przychodzace").exists() or \
        not any((cfg.projects_dir.parent / "przychodzace").iterdir())


# ---- Gmail: adresaci ----

def test_parse_message_reads_to_and_cc():
    raw = ("From: Basia <basia@kancelariabil.pl>\r\nTo: Jan Testowy <jan@firma-testowa.pl>\r\n"
           f"Cc: Monika <{ME}>, aneta@kancelariabil.pl\r\nSubject: Oferta\r\nMessage-ID: <x@y>\r\n\r\nTresc").encode()
    m = parse_message(raw, ("basia@kancelariabil.pl",), "fb")
    assert m.to == ("Jan Testowy <jan@firma-testowa.pl>",)
    assert m.cc == (f"Monika <{ME}>", "aneta@kancelariabil.pl")
