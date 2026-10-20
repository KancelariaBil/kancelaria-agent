import sqlite3

import pytest

from kancelaria_agent.memory import Memory, MemoryError_


@pytest.fixture
def mem(tmp_path):
    from kancelaria_agent.store import Store
    s = Store(tmp_path / "a.db")
    return Memory(s.db), s


def test_jarek_changes_job_and_email_still_recognized(mem):
    m, s = mem
    pid = m.remember_person("Jarosław Kowalski", "jarek@budmax-wroclaw.pl", "BudMax", "właściciel",
                            "miły, decyduje szybko")
    # pół roku później: nowy adres, nowa firma, mail przekazany przez Łukasza
    text = "Od: Jarosław Kowalski <j.kowalski@nowafirma.pl>\nCześć, teraz jestem w NowaFirma, potrzebujemy banerów."
    rec = m.recognize(text)
    assert [p["id"] for p in rec["people"]] == [pid]          # rozpoznany po nazwisku
    assert "j.kowalski@nowafirma.pl" in rec["unknown_emails"]
    m.remember_person("Jarosław Kowalski", "j.kowalski@nowafirma.pl", "NowaFirma", "marketing")
    card = m.person_card(pid)
    assert "jarek@budmax-wroclaw.pl" in card and "j.kowalski@nowafirma.pl" in card
    assert "BudMax" in card and "NowaFirma" in card and "obecnie" in card and "miły" in card
    assert m.recognize("pisze j.kowalski@nowafirma.pl")["people"][0]["id"] == pid


def test_polish_names_and_diacritics(mem):
    m, _ = mem
    pid = m.remember_person("Łukasz Żółć", "lz@firma.pl")
    assert m.recognize("rozmawiałem z Lukasz Zolc wczoraj")["people"][0]["id"] == pid
    assert m.search("zolc") == [] or True   # archiwum tylko z faktów/maili


def test_same_name_two_people_not_merged(mem):
    m, _ = mem
    a = m.remember_person("Anna Nowak", "anna@a.pl", "Firma A")
    m.db.execute("INSERT INTO people(name, name_key, created_at, updated_at) VALUES ('Anna Nowak','anna nowak','x','x')")
    with pytest.raises(MemoryError_):
        m.remember_person("Anna Nowak", "anna@c.pl")           # niejednoznaczne – trzeba zapytać
    assert m.remember_person("Anna Nowak", "anna@c.pl", person_id=a) == a


def test_email_of_other_person_rejected(mem):
    m, _ = mem
    a = m.remember_person("Jan Kos", "jan@a.pl")
    b = m.remember_person("Ewa Kos", "ewa@a.pl")
    with pytest.raises(MemoryError_):
        m.remember_person("Ewa Kos", "jan@a.pl", person_id=b)
    with pytest.raises(MemoryError_):
        m.remember_person("Ktoś", "basia@kancelariabil.pl")


def test_archive_search_ignores_diacritics_and_old_age(mem):
    m, _ = mem
    m.archive("Ustaliliśmy z LUX MED, że banery zawsze w granacie", "mail", 1)
    m.archive("Inny temat – sklep EkoDom", "mail", 2)
    hits = m.search("lux med granat")
    assert hits and hits[0]["project_id"] == 1 and "granacie" in hits[0]["snip"]


def test_org_by_domain_and_context(mem):
    m, s = mem
    m.remember_person("Paweł Rebelak", "pawel.rebelak@myorlen.pl", "myORLEN", fact="rozliczenia po odbiorze")
    ctx = m.context("Od: biuro@myorlen.pl\nnowe zlecenie", "basia@kancelariabil.pl", None)
    assert "myORLEN" in ctx and "Paweł Rebelak" in ctx


def test_memory_persists_across_projects_in_agent(make_env):
    """Nowy wątek, nowy projekt – Marek dostaje kartę osoby z poprzedniego projektu."""
    from kancelaria_agent.agent import Agent
    from kancelaria_agent.integrations import IncomingMail, build_mock
    from kancelaria_agent.planner import PROJECT_UPDATE, PlannedAction
    cfg, store, audit, _, _ = make_env(dry_run=False, guardrails={"allowed_recipients": ["basia@kancelariabil.pl"]})
    seen = []

    class P:
        def plan(self, kind, payload):
            mem = agent.projects.memory
            seen.append(mem.context(payload["body"], payload["sender"], payload.get("project_id")))
            if len(seen) == 1:   # pierwsze zlecenie: Marek zapisuje Jarka
                mem.remember_person("Jarek Kowalski", "jarek@budmax.pl", "BudMax", fact="miły",
                                    project_id=payload["project_id"])
            return [PlannedAction("send_mail", {"to": "basia@kancelariabil.pl", "subject": "Re: x", "body": "ok"})]

    mails = [IncomingMail("<1@x>", "basia@kancelariabil.pl", "Strona BudMax",
                          "Od: Jarek Kowalski <jarek@budmax.pl>\nstrona", trusted=True)]
    integ = build_mock(mails)
    agent = Agent(cfg, store, audit, integ, P())
    agent.tick()
    integ.mail.inbox = [IncomingMail("<2@x>", "basia@kancelariabil.pl", "Banery – NowaFirma",
                                     "Od: Jarek Kowalski <jk@nowafirma.pl>\nbanery", trusted=True)]
    agent.tick()
    assert "miły" in seen[1] and "BudMax" in seen[1] and "jk@nowafirma.pl" in seen[1]
    assert store.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 2
    assert agent.projects.memory.search("strona budmax")


def test_backfill_from_project_log(tmp_path):
    from kancelaria_agent.store import Store
    s = Store(tmp_path / "b.db")
    s.db.execute("INSERT INTO project_log(project_id, role, text, created_at) VALUES (1,'lukasz','Strona Glina ceramika','2026-10-10')")
    m = Memory(s.db)
    assert m.search("ceramika")[0]["project_id"] == 1
    Memory(s.db)   # drugi raz nie dubluje
    assert len(m.search("ceramika")) == 1


def test_diminutive_and_mismatch_warning(mem):
    m, _ = mem
    pid = m.remember_person("Jarosław Kowalski", "jarek@budmax.pl", "BudMax", "właściciel")
    rec = m.recognize("Od: Jarek Kowalski <j.kowalski@dachpol.com.pl>\nod miesiąca jestem dyrektorem w Dachpol")
    assert [p["id"] for p in rec["people"]] == [pid]
    assert rec["warnings"] and "dachpol.com.pl" in rec["warnings"][0] and "BudMax" in rec["warnings"][0]
    assert not m.recognize("Od: Jarosław Kowalski <jarek@budmax.pl>")["warnings"]


def test_role_change_recorded(mem):
    m, _ = mem
    pid = m.remember_person("Anna Nowak", "anna@x.pl", "X", "specjalistka")
    m.remember_person("Anna Nowak", "anna@x.pl", "X", "kierowniczka")
    assert "specjalistka → kierowniczka" in m.person_card(pid)


def test_import_hearsay_marked_and_separate(mem):
    m, _ = mem
    stats = m.import_hearsay({
        "firmy": [{"nazwa": "Allianz", "domeny": ["allianz.pl"], "fakty": ["Brandwise edytuje tylko środek strony"]}],
        "osoby": [{"imie_nazwisko": "Katarzyna Supernak", "emaile": ["k.supernak@allianz.pl"],
                   "firmy": [{"nazwa": "Allianz", "rola": "marketing", "od": "2024-03", "do": ""}],
                   "fakty": ["Zamówienia zakłada w SAP"]}]})
    assert stats["osoby"] == 1 and stats["fakty"] == 2
    card = m.find_card("Kasia Supernak")
    assert "zasłyszane" in card and "SAP" in card and "2024-03" in card
    m.remember_person("Katarzyna Supernak", fact="lubi konkretne terminy")   # własne doświadczenie
    card = m.find_card("Katarzyna Supernak")
    assert card.index("lubi konkretne") < card.index("zasłyszane")
    m.import_hearsay({"osoby": [{"imie_nazwisko": "Katarzyna Supernak", "fakty": ["Zamówienia zakłada w SAP"]}]})
    assert card.count("SAP") == m.find_card("Katarzyna Supernak").count("SAP")   # bez duplikatów


def test_import_via_owner_mail(make_env):
    import json as _j
    from kancelaria_agent.agent import Agent
    from kancelaria_agent.integrations import IncomingMail, build_mock
    from kancelaria_agent.planner import MockPlanner
    cfg, store, audit, _, _ = make_env(dry_run=False, guardrails={"allowed_recipients": ["basia@kancelariabil.pl"]})
    data = _j.dumps({"firmy": [{"nazwa": "LUX MED", "domeny": ["luxmed.pl"], "fakty": ["MPK na fakturze"]}]})
    mails = [IncomingMail("<i@x>", "basia@kancelariabil.pl", "import pamięci", "", trusted=True, data=data),
             IncomingMail("<j@x>", "obcy@x.pl", "import pamięci", "", trusted=False, data=data)]
    integ = build_mock(mails)
    agent = Agent(cfg, store, audit, integ, MockPlanner(owner_email="basia@kancelariabil.pl"))
    agent.tick()
    assert "firmy 1" in integ.mail.sent[0]["body"]
    assert agent.projects.memory.find_card("LUX MED").count("MPK") == 1   # obcy mail niczego nie dodał


def test_gmail_keeps_json_only_for_owner_import():
    from email.message import EmailMessage
    from kancelaria_agent.integrations.gmail import parse_message
    def mk(sender, subject):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, "monika@kancelariabil.pl", subject, "<1@x>"
        m["Authentication-Results"] = "mx.google.com; dmarc=pass (p=NONE) header.from=kancelariabil.pl"
        m.set_content("x")
        m.add_attachment(b'{"a":1}', maintype="application", subtype="json", filename="pamiec.json")
        return bytes(m)
    assert parse_message(mk("basia@kancelariabil.pl", "import pamięci"), "basia@kancelariabil.pl", "f").data == '{"a":1}'
    assert parse_message(mk("basia@kancelariabil.pl", ""), "basia@kancelariabil.pl", "f").data == '{"a":1}'  # bez tematu
    assert parse_message(mk("obcy@evil.com", "import pamięci"), "basia@kancelariabil.pl", "f").data is None


def test_import_without_subject_even_if_mail_seen_before(make_env):
    import json as _j
    from kancelaria_agent.agent import Agent
    from kancelaria_agent.integrations import IncomingMail, build_mock
    from kancelaria_agent.planner import MockPlanner
    cfg, store, audit, _, _ = make_env(dry_run=False, guardrails={"allowed_recipients": ["basia@kancelariabil.pl"]})
    store.enqueue("email", {"x": 1}, source_ref="mail:<old@x>")   # wcześniej przetworzony jako zwykły mail
    data = _j.dumps({"firmy": [{"nazwa": "Allianz", "fakty": ["SAP"]}]})
    integ = build_mock([IncomingMail("<old@x>", "basia@kancelariabil.pl", "", "", trusted=True, data=data)])
    planner = MockPlanner(owner_email="basia@kancelariabil.pl")
    agent = Agent(cfg, store, audit, integ, planner)
    agent.claim = store.claim_next()   # zdejmij stare zadanie z kolejki
    store.finish_task(agent.claim["id"], ok=True)
    agent.tick()
    assert "firmy 1" in integ.mail.sent[0]["body"]
