"""Wysłana wcześniej oferta przekazana Monice (biuro@ w „Do”, temat „Oferta…”) → panel. Dane fikcyjne."""
from kancelaria_agent import oferty
from kancelaria_agent.integrations import IncomingMail

from test_oferty import ME, TEAM, make_agent, payload

FWD = ("Proszę wpisać do panelu.\n\n"
       "---------- Forwarded message ---------\n"
       "Od: Barbara Bil <basia@kancelariabil.pl>\n"
       "Date: czw., 12 lis 2026 o 10:15\n"
       "Subject: Oferta – obsługa spółki\n"
       "To: Jan Testowy <jan@firma-testowa.pl>\n"
       "Cc: Monika <monika@kancelariabil.pl>\n\n"
       "Dzień dobry,\nprzesyłam ofertę stałej obsługi. Ryczałt 3 000 zł netto miesięcznie.\n"
       "Oferta jest ważna do 30.11.2026.\nZ poważaniem")


def fwd_mail(**kw):
    base = dict(message_id="<fw1@example.com>", sender="basia@kancelariabil.pl", subject="Oferta: Fwd: Oferta – obsługa spółki",
                body=FWD, trusted=True, to=(ME,), cc=(), date="Sat, 21 Nov 2026 09:00:00 +0100")
    base.update(kw)
    return IncomingMail(**base)


def test_forwarded_offer_detected():
    assert oferty.is_forwarded_offer(payload(fwd_mail()), ME, TEAM)
    assert not oferty.is_dw_offer(payload(fwd_mail()), ME, TEAM)


def test_not_forwarded_offer():
    assert not oferty.is_forwarded_offer(payload(fwd_mail(subject="Fwd: umowa")), ME, TEAM)       # temat bez „Oferta”
    assert not oferty.is_forwarded_offer(payload(fwd_mail(trusted=False)), ME, TEAM)              # niezweryfikowany
    assert not oferty.is_forwarded_offer(payload(fwd_mail(to=("jan@firma-testowa.pl",))), ME, TEAM)   # Monika nie w „Do”
    # zlecenie „przygotuj ofertę” – bez przekazanej wiadomości i bez pliku → zwykłe zadanie dla Moniki
    ask = fwd_mail(subject="Oferta dla firmy X", body="Przygotuj proszę ofertę na obsługę spółki.")
    assert not oferty.is_forwarded_offer(payload(ask), ME, TEAM)


def test_forwarded_with_pdf_only_counts():
    m = fwd_mail(subject="Oferta Kowalski", body="W załączniku.", attachments=("oferta.pdf",))
    p = payload(m) | {"attachments": list(m.attachments)}
    assert oferty.is_forwarded_offer(p, ME, TEAM)


def test_forwarded_view_takes_client_date_and_body():
    v = oferty.forwarded_view(payload(fwd_mail()), ME, TEAM)
    d = oferty.extract_offer(v, ME, TEAM)
    assert d["client_email"] == "jan@firma-testowa.pl" and d["client_name"] == "Jan Testowy"
    assert d["date"].startswith("2026-11-12T10:15")
    assert d["amount"] == 3000.0 and d["form"] == "miesięczny ryczałt" and d["valid_until"] == "2026-11-30"
    assert d["title"] == "obsługa spółki"
    assert d["from"] == "basia@kancelariabil.pl"
    assert ME not in [a.lower() for a in d["to"]]


def test_forwarded_offer_goes_to_panel(make_env, monkeypatch):
    cfg, store, integ, agent = make_agent(make_env, [fwd_mail()], monkeypatch, dry_run=False)
    agent.tick()
    assert len(integ.panel.offers) == 1
    o = integ.panel.offers[0]
    assert o["client_email"] == "jan@firma-testowa.pl" and o["amount"] == 3000.0
    assert integ.mail.sent == []


def test_outlook_style_headers():
    body = ("-----Original Message-----\nFrom: Basia <basia@kancelariabil.pl>\nSent: 12.11.2026 10:15\n"
            "To: Jan <jan@firma-testowa.pl>; Ewa <ewa@firma-testowa.pl>\nSubject: Oferta\n\nTreść oferty 500 zł")
    f = oferty.parse_forwarded(body)
    assert f["to"] == ["Jan <jan@firma-testowa.pl>", "Ewa <ewa@firma-testowa.pl>"] and "500 zł" in f["body"]
    assert oferty._pl_date(f["date"]) == "2026-11-12T10:15:00"
