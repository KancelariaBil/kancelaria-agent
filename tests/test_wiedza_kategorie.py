"""Kategorie bazy wiedzy, rozpoznawanie i odporność wyszukiwania. Wyłącznie dane fikcyjne."""

import urllib.parse
import urllib.request
from urllib.parse import quote

import pytest

from kancelaria_agent.agent import _knowledge_subject
from kancelaria_agent.integrations import IncomingMail
from kancelaria_agent.wiedza import KnowledgeBase, KnowledgeError, category_from_word, classify

from test_wiedza import BASIA, _agent, _multipart, _post, panel  # noqa: F401  (fixture)

INTERP = ("INTERPRETACJA INDYWIDUALNA\nDyrektor Krajowej Informacji Skarbowej, sygn. 0111-KDIB1-3.4010.123.2026.1.AB\n"
          "Opis stanu faktycznego: Wnioskodawca prowadzi działalność… Stanowisko Wnioskodawcy: …")
WYROK = ("WYROK\nW IMIENIU RZECZYPOSPOLITEJ POLSKIEJ\nSąd Apelacyjny w Testowie, I ACa 123/25\n"
         "po rozpoznaniu sprawy z powództwa X przeciwko Y … Uzasadnienie: powód wniósł …")
USTAWA = "USTAWA\nz dnia 1 stycznia 2026 r. o testowaniu\n" + "".join(f"Art. {i}. Treść przepisu {i}.\n" for i in range(1, 12))
KOMENTARZ = ("Komentarz do art. 238 k.s.h., Nb. 12. Teza 3. Zob. też glosa do wyroku. Legalis 2026.")
ARTYKUL = ("Jan Testowy, Opodatkowanie fundacji rodzinnej. Streszczenie: … Słowa kluczowe: fundacja rodzinna, CIT.\n"
           "Przegląd Testowy 2026, nr 3, ISSN 1234-5678. Bibliografia: …")
WZOR = ("UMOWA POŻYCZKI\nzawarta w dniu ………… w ………… pomiędzy:\n[imię i nazwisko] zamieszkałym …………, "
        "a [nazwa spółki] z siedzibą …………\nKwota pożyczki: ………… zł\n\n(miejscowość, data)            (podpis)")
NOTATKA = "Notatka z narady: omówić z klientem termin spotkania."


@pytest.mark.parametrize("name,text,cat", [
    ("dokument.txt", INTERP, "interpretacje"),
    ("dokument.txt", WYROK, "wyroki"),
    ("dokument.txt", USTAWA, "przepisy"),
    ("dokument.txt", KOMENTARZ, "komentarze"),
    ("dokument.txt", ARTYKUL, "artykuly"),
    ("dokument.txt", NOTATKA, "inne"),
    ("dokument.docx", WZOR, "wzory"),
    ("ii-fsk-638-22.pdf", "", "wyroki"),
    ("ii-fsk-1605-24-zastosowanie-uproszczenia-safe.pdf", "", "wyroki"),
    ("I ACa 123-25.pdf", "", "wyroki"),
    ("i10410-o1.pdf", "", "inne"),
    ("Wzór pełnomocnictwa.docx", "", "wzory"),
    ("szablon wezwania do zaplaty.docx", "", "wzory"),
    ("Wzór pozwu – wyrok zaoczny.docx", "", "wzory"),
    ("Monografia fundacja rodzinna.pdf", "", "artykuly"),
    ("artykul MP 2026.pdf", "", "artykuly"),
    ("Ordynacja-podatkowa.pdf", "", "przepisy"),
    ("wyrok NSA II FSK 1.pdf", "", "wyroki"),
    ("Komentarz KSH art 299.pdf", "", "komentarze"),
    ("interpretacja VAT.pdf", "", "interpretacje"),
    ("art-299-ksh.pdf", "", "komentarze"),
    ("Art. 201 KSH – zarząd spółki.docx", "", "komentarze"),
])
def test_classify(name, text, cat):
    assert classify(name, text) == cat


def test_category_words():
    assert category_from_word("Wyroki") == "wyroki"
    assert category_from_word("komentarz") == "komentarze"
    assert category_from_word("artykuły") == "artykuly" and category_from_word("Książki") == "artykuly"
    assert category_from_word("wzory") == "wzory" and category_from_word("Szablony") == "wzory" and category_from_word("wzór") == "wzory"
    assert category_from_word("auto") is None and category_from_word("") is None
    assert category_from_word("cokolwiek") is None


def test_save_auto_and_forced_and_move(tmp_path):
    kb = KnowledgeBase(tmp_path)
    assert kb.save("a.txt", WYROK.encode()) == "wyroki/a.txt"
    assert kb.save("b.txt", NOTATKA.encode()) == "b.txt"                      # „inne” = katalog główny
    assert kb.save("c.txt", b"Inna notatka testowa.", "komentarze") == "komentarze/c.txt"
    assert {(e.name, e.category) for e in kb.entries()} == {
        ("wyroki/a.txt", "wyroki"), ("b.txt", "inne"), ("komentarze/c.txt", "komentarze")}
    assert [e.name for e in kb.entries("wyroki")] == ["wyroki/a.txt"]
    assert "RZECZYPOSPOLITEJ" in kb.text("wyroki/a.txt")
    assert (tmp_path / "wyroki" / ".tekst" / "a.txt.txt").exists()            # tekst zapisany przy dodaniu
    assert kb.move("b.txt", "przepisy") == "przepisy/b.txt"
    assert [h.name for h in kb.search("narady", category="przepisy")] == ["przepisy/b.txt"]
    assert kb.search("narady", category="wyroki") == []
    with pytest.raises(KnowledgeError):
        kb.save("d.txt", b"x", "nieznana")
    for bad in ("../a.txt", "obce/a.txt", "wyroki/../b.txt", "wyroki/.tekst"):
        with pytest.raises(KnowledgeError):
            kb.path(bad)


def test_sort_other(tmp_path):
    kb = KnowledgeBase(tmp_path)
    (tmp_path / "wyrok-stary.txt").write_bytes(WYROK.encode())   # plik sprzed kategorii
    (tmp_path / "notatka.txt").write_bytes(NOTATKA.encode())
    assert kb.sort_other() == [("wyrok-stary.txt", "wyroki/wyrok-stary.txt")]
    assert {e.name for e in kb.entries()} == {"wyroki/wyrok-stary.txt", "notatka.txt"}


def test_broken_docx_does_not_break_search(tmp_path):
    """Plik Word z komentarzem XML wywracał odczyt – i całe wyszukiwanie (błąd z bazy Moniki)."""
    from docx import Document
    d = Document()
    d.add_paragraph("Zgromadzenie wspólników – protokół.")
    from lxml import etree
    d.element.body.append(etree.Comment("komentarz w treści"))
    d.save(tmp_path / "z-komentarzem.docx")
    (tmp_path / "zepsuty.pdf").write_bytes(b"to nie jest pdf")
    kb = KnowledgeBase(tmp_path)
    (tmp_path / "zgromadzenie.txt").write_text("Zgromadzenie wspólników spółki.", encoding="utf-8")
    names = {h.name for h in kb.search("zgromadzenie wspolnikow")}
    assert "zgromadzenie.txt" in names and "z-komentarzem.docx" in names


@pytest.mark.parametrize("subject,expected", [
    ("wiedza", (True, None)), ("wiedza wyroki", (True, "wyroki")), ("wiedza książki", (True, "artykuly")), ("wiedza wzory", (True, "wzory")), ("Re: Wiedza Komentarz", (True, "komentarze")),
    ("wiedza o spółkach", (False, None)), ("wiedza cokolwiek", (False, None)), ("umowa", (False, None)),
])
def test_knowledge_subject(subject, expected):
    assert _knowledge_subject({"subject": subject}) == expected


def test_mail_wiedza_with_category(make_env):
    mail = IncomingMail("wk1", BASIA, "wiedza komentarze", "", files=(("notatka.txt", NOTATKA.encode()),))
    cfg, store, integ, agent = _agent(make_env, [mail])
    agent.tick()
    assert [e.name for e in KnowledgeBase(cfg.knowledge_dir).entries()] == ["komentarze/notatka.txt"]
    assert "Komentarze" in integ.mail.sent[-1]["body"]


def test_panel_category_upload_move_sort(panel):  # noqa: F811
    kb, url = panel
    body, ctype = _multipart([("wyrok.txt", WYROK.encode())])
    b = "----granica"
    body = body.replace(f"--{b}--".encode(), (f'--{b}\r\nContent-Disposition: form-data; name="kategoria"\r\n\r\n'
                                               f"komentarze\r\n--{b}--").encode())
    r = _post(url + "/dodaj", body, ctype)
    assert r.status == 303 and [e.name for e in kb.entries()] == ["komentarze/wyrok.txt"]
    r = _post(url + "/przenies", b"plik=" + quote("komentarze/wyrok.txt").encode() + b"&kategoria=wyroki",
              "application/x-www-form-urlencoded")
    assert r.status == 303 and [e.name for e in kb.entries()] == ["wyroki/wyrok.txt"]
    page = urllib.request.urlopen(url + "/?k=wyroki").read().decode()
    assert "Wyroki i orzecznictwo (1)" in page and "wyrok.txt" in page
    assert urllib.request.urlopen(url + "/plik/" + quote("wyroki/wyrok.txt")).read() == WYROK.encode()
    (kb.root / "stary-wyrok.txt").write_bytes(WYROK.encode())
    r = _post(url + "/porzadkuj", b"", "application/x-www-form-urlencoded")
    assert r.status == 303 and {e.name for e in kb.entries()} == {"wyroki/wyrok.txt", "wyroki/stary-wyrok.txt"}


def test_no_duplicates_any_name_or_category(tmp_path):
    kb = KnowledgeBase(tmp_path)
    long = (WYROK + " " + "uzasadnienie " * 80).encode()
    assert kb.save_report("wyrok.txt", long) == ("wyroki/wyrok.txt", False)
    assert kb.save_report("kopia wyroku.txt", long, "komentarze") == ("wyroki/wyrok.txt", True)   # inna nazwa
    # ten sam tekst w innym pliku (np. ponownie pobrany) – też duplikat
    assert kb.save_report("wyrok-ponownie.md", long.replace(b"WYROK", b"wyrok")) == ("wyroki/wyrok.txt", True)
    assert [e.name for e in kb.entries()] == ["wyroki/wyrok.txt"]
    assert kb.save_report("inny.txt", NOTATKA.encode())[1] is False


def test_mail_and_panel_report_duplicates(make_env, panel):  # noqa: F811
    mail = IncomingMail("wd1", BASIA, "wiedza", "", files=(("a.txt", NOTATKA.encode()), ("b.txt", NOTATKA.encode())))
    cfg, store, integ, agent = _agent(make_env, [mail])
    agent.tick()
    assert len(KnowledgeBase(cfg.knowledge_dir).entries()) == 1
    assert "już jest w bazie" in integ.mail.sent[-1]["body"]
    kb, url = panel
    kb.save("a.txt", NOTATKA.encode())
    body, ctype = _multipart([("inna-nazwa.txt", NOTATKA.encode())])
    r = _post(url + "/dodaj", body, ctype)
    assert "już jest w bazie" in urllib.parse.unquote(r.headers["Location"]) and len(kb.entries()) == 1


def test_panel_bulk_move(panel):  # noqa: F811
    kb, url = panel
    kb.save("a.txt", b"Pierwsza notatka testowa.")
    kb.save("b.txt", b"Druga notatka testowa, inna.")
    kb.save("c.txt", b"Trzecia notatka testowa, jeszcze inna.")
    page = urllib.request.urlopen(url + "/").read().decode()
    assert 'action="/przenies-zbiorczo"' in page and 'name="kat:a.txt"' in page and 'id="bulk-info"' in page
    form = (b"kat:a.txt=wyroki&kat:b.txt=inne&kat:c.txt=inne"            # a – zmiana w wierszu
            b"&zaznacz=b.txt&zaznacz=c.txt&kategoria=artykuly")           # b, c – zaznaczone
    r = _post(url + "/przenies-zbiorczo", form, "application/x-www-form-urlencoded")
    assert r.status == 303 and "Przeniesiono 3" in urllib.parse.unquote(r.headers["Location"])
    assert {e.name for e in kb.entries()} == {"wyroki/a.txt", "artykuly/b.txt", "artykuly/c.txt"}
    r = _post(url + "/przenies-zbiorczo", b"kat:wyroki/a.txt=wyroki", "application/x-www-form-urlencoded")
    assert "Nic do przeniesienia" in urllib.parse.unquote(r.headers["Location"])


def test_pdf_and_txt_of_same_document_are_duplicates(tmp_path):
    kb = KnowledgeBase(tmp_path)
    body = "Fundacja rodzinna – opracowanie testowe. " * 40
    kb.save("opracowanie.txt", ("--- strona 1 ---\n" + body[:800] + "\n--- strona 2 ---\n" + body[800:]).encode())
    name, dup = kb.save_report("opracowanie-kopia.md", ("# " + body.upper()).encode())
    assert dup and name == "opracowanie.txt"
    (tmp_path / "stara-kopia.txt").write_text(body.replace(" – ", "\n\n"), encoding="utf-8")   # sprzed wykrywania
    (tmp_path / "rozdzial-2.txt").write_text(body[:1200] + "Inny rozdział. " * 30, encoding="utf-8")  # ten sam początek
    assert kb.duplicates() == [["opracowanie.txt", "stara-kopia.txt"]]


def test_panel_back_link_and_messages(panel):  # noqa: F811
    kb, url = panel
    kb.save("a.txt", b"Zgromadzenie testowe.")
    page = urllib.request.urlopen(url + "/?q=zgromadzenie").read().decode()
    assert "Wróć do listy plików" in page and '<a href="/"' in page
    page = urllib.request.urlopen(url + "/?msg=" + quote("już jest w bazie") + "&bad=1").read().decode()
    assert page.count("już jest w bazie") == 2          # na górze i przy formularzu dodawania


def test_panel_duplicates_page(panel):  # noqa: F811
    kb, url = panel
    body = "Uchwała wspólników – dokument testowy. " * 30
    kb.root.mkdir(parents=True, exist_ok=True)
    (kb.root / "uchwala.txt").write_text(body, encoding="utf-8")
    (kb.root / "uchwala-2.txt").write_text(body, encoding="utf-8")
    page = urllib.request.urlopen(url + "/duplikaty").read().decode()
    assert "uchwala.txt" in page and "uchwala-2.txt" in page and "(zostaw jeden)" in page
    r = _post(url + "/usun", b"plik=uchwala-2.txt&wroc=%2Fduplikaty", "application/x-www-form-urlencoded")
    assert r.headers["Location"].startswith("/duplikaty?msg=")
    assert "Brak duplikatów" in urllib.request.urlopen(url + "/duplikaty").read().decode()
    r = _post(url + "/usun", b"plik=nie-ma.txt&wroc=https://zly.example", "application/x-www-form-urlencoded")
    assert r.headers["Location"].startswith("/?msg=")
