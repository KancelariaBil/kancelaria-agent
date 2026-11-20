"""Poczta: bez ponownego ściągania całych maili co 30 s i bez pętli restartów przy limicie Gmaila (OVERQUOTA)."""
import imaplib

from kancelaria_agent.integrations.base import IncomingMail

from test_wiedza import BASIA, _agent  # noqa: F401


GOOD = b"From: Jan <jan@firma.pl>\r\nSubject: Oferta\r\nMessage-ID: <a@x>\r\n\r\nDzien dobry"


def _conn(log):
    class Conn:
        def select(self, *a, **k):
            return "OK", [b"1"]

        def uid(self, cmd, *args):
            log.append((cmd, *args))
            if cmd == "SEARCH":
                return "OK", [b"7"]
            if "HEADER.FIELDS" in args[1]:
                return "OK", [(b"7 (BODY[HEADER.FIELDS (MESSAGE-ID)]", b"Message-ID: <a@x>\r\n\r\n"), b")"]
            return "OK", [(b"7 (BODY[]", GOOD)]
    return Conn()


def _gm(monkeypatch, log):
    from kancelaria_agent.integrations import gmail as g
    from test_gmail import SETTINGS
    gm = g.GmailMail(SETTINGS)
    monkeypatch.setattr(gm, "_imap", lambda: _conn(log))
    monkeypatch.setattr(g, "_quiet_logout", lambda c: None)
    return gm


def test_known_mail_is_not_downloaded_again(monkeypatch):
    log = []
    gm = _gm(monkeypatch, log)
    assert gm.fetch_unread(skip=lambda mid: mid == "<a@x>") == []
    assert not any(c[0] == "FETCH" and c[2] == "(BODY.PEEK[])" for c in log)   # tylko nagłówek


def test_new_mail_is_downloaded(monkeypatch):
    log = []
    gm = _gm(monkeypatch, log)
    mails = gm.fetch_unread(skip=lambda mid: False)
    assert [m.message_id for m in mails] == ["<a@x>"]
    assert any(c[0] == "FETCH" and c[2] == "(BODY.PEEK[])" for c in log)


def test_overquota_pauses_instead_of_crashing(make_env):
    cfg, store, integ, agent = _agent(make_env, [])
    calls = []

    def boom(skip=None):
        calls.append(1)
        raise imaplib.IMAP4.abort("command: UID => [OVERQUOTA] Account exceeded command or bandwidth limits.")

    integ.mail.fetch_unread = boom
    assert agent.tick() == "ok"          # nie wywraca się
    assert agent.tick() == "ok"
    assert len(calls) == 1               # przerwa – nie dobija się do skrzynki co 30 s
    assert agent._mail_pause_until > 0


def test_processed_mail_is_skipped_next_time(make_env):
    mail = IncomingMail("m1", BASIA, "Pytanie", "Dzień dobry")
    cfg, store, integ, agent = _agent(make_env, [mail])
    agent.ingest_mail()
    seen = []
    orig = integ.mail.fetch_unread
    integ.mail.fetch_unread = lambda skip=None: (seen.append(skip("m1")), orig(skip=skip))[1]
    assert agent.ingest_mail() == 0 and seen == [True]
