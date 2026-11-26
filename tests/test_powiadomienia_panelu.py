"""Powiadomienia z panelu (np. „klient przesłał pliki”) – Monika wysyła je ze swojego adresu, tylko do dozwolonych odbiorców."""
from kancelaria_agent.agent import Agent
from kancelaria_agent.integrations import build_mock
from kancelaria_agent.planner import MockPlanner


def make_agent(make_env, **ov):
    cfg, store, audit, _, _ = make_env(**ov)
    integ = build_mock([])
    return integ, Agent(cfg, store, audit, integ, MockPlanner())


def note(nid, to, cc=()):
    return {"id": nid, "to": list(to), "cc": list(cc), "subject": "Klient przesłał 3 pliki – sprawa 2026/0008", "body": "Dzień dobry,\nklient przesłał pliki."}


def test_allowed_notification_sent_by_monika_and_acked(make_env):
    integ, agent = make_agent(make_env, dry_run=False)
    integ.panel.notifications = [note("n1", ["aneta@kancelariabil.pl"], ["basia@kancelariabil.pl"])]
    agent.tick()
    assert len(integ.mail.sent) == 1
    s = integ.mail.sent[0]
    assert s["to"] == ["aneta@kancelariabil.pl"] and s["cc"] == ["basia@kancelariabil.pl"] and "2026/0008" in s["subject"]
    assert integ.panel.acks == [("n1", True, "")]


def test_recipient_outside_list_goes_back_to_panel(make_env):
    integ, agent = make_agent(make_env, dry_run=False)
    integ.panel.notifications = [note("n2", ["obcy@gmail.com"], ["basia@kancelariabil.pl"])]
    agent.tick()
    assert integ.mail.sent == []
    nid, ok, err = integ.panel.acks[0]
    assert nid == "n2" and ok is False and "obcy@gmail.com" in err


def test_notifications_checked_at_most_once_a_minute(make_env):
    integ, agent = make_agent(make_env, dry_run=False)
    agent.tick()
    integ.panel.notifications = [note("n3", ["basia@kancelariabil.pl"])]
    agent.tick()   # ta sama minuta – jeszcze nie pobiera
    assert integ.mail.sent == []
    agent._notified_at -= 61
    agent.tick()
    assert len(integ.mail.sent) == 1 and integ.panel.acks == [("n3", True, "")]


def test_panel_down_does_not_break_tick(make_env):
    integ, agent = make_agent(make_env, dry_run=False)

    def boom():
        raise RuntimeError("panel niedostępny")
    integ.panel.pending_notifications = boom
    assert agent.tick() == "ok"
