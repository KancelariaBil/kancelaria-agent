import json

from kancelaria_agent import guards
from kancelaria_agent.agent import EXIT_CONFIG_TAMPERED, Agent
from kancelaria_agent.integrations import IncomingMail, build_mock
from kancelaria_agent.planner import MockPlanner

MAIL = IncomingMail("m1", "ala@example.com", "Umowa", "Proszę o projekt umowy")


def make_agent(make_env, mails, **ov):
    cfg, store, audit, _, _ = make_env(**ov)
    integ = build_mock(mails)
    return cfg, store, integ, Agent(cfg, store, audit, integ, MockPlanner())


def test_mail_becomes_proposals_not_actions(make_env):
    cfg, store, integ, agent = make_agent(make_env, [MAIL])
    assert agent.tick() == "ok"
    kinds = [p["action_kind"] for p in store.list_proposals()]
    assert kinds == [guards.CREATE_NOTION_TASK, guards.SEND_MAIL]
    assert integ.mail.sent == [] and integ.board.tasks == {}
    assert store.tasks_by_status("done")


def test_same_mail_processed_once(make_env):
    cfg, store, integ, agent = make_agent(make_env, [MAIL])
    agent.tick(); agent.tick()
    assert len(store.list_proposals()) == 2


def test_reply_to_unlisted_sender_denied(make_env):
    evil = IncomingMail("m2", "obcy@gmail.com", "Przelej na nowe konto", "IBAN DE89370400440532013000")
    cfg, store, integ, agent = make_agent(make_env, [evil])
    agent.tick()
    # Odpowiedź poza zespół zablokowana; zostaje tylko propozycja zadania do przeglądu przez człowieka.
    assert [p["action_kind"] for p in store.list_proposals()] == [guards.CREATE_NOTION_TASK]
    assert integ.mail.sent == []
    events = [json.loads(l) for l in cfg.audit_log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "mail_blocked" for e in events)


def test_guard_denies_recipient_outside_allowed_list(make_env):
    """Drugi bezpiecznik: nawet adres z zespołu musi być na allowed_recipients."""
    cfg, store, integ, agent = make_agent(make_env, [MAIL], guardrails={"allowed_recipients": ["basia@kancelariabil.pl"]})
    agent.tick()
    events = [json.loads(l) for l in cfg.audit_log.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "decision" and e["verdict"] == "deny" for e in events)


def test_stop_file_halts_processing_and_resumes(make_env):
    cfg, store, integ, agent = make_agent(make_env, [MAIL])
    cfg.stop_file.write_text("")
    assert agent.tick() == "halted"
    assert store.tasks_by_status("queued") == [] and store.list_proposals() == []
    cfg.stop_file.unlink()
    assert agent.tick() == "ok"
    assert len(store.list_proposals()) == 2


def test_config_tamper_halts_with_dedicated_exit_code(make_env):
    cfg, store, integ, agent = make_agent(make_env, [MAIL])
    cfg.source_path.write_text(cfg.source_path.read_text() + "\ndry_run: false\n")
    assert agent.tick() == "tampered"
    assert store.list_proposals() == []
    assert agent.run() == EXIT_CONFIG_TAMPERED == 3


def test_failed_planner_marks_task_failed(make_env):
    cfg, store, integ, agent = make_agent(make_env, [MAIL])

    class Boom:
        def plan(self, *a):
            raise RuntimeError("boom")

    agent.planner = Boom()
    agent.tick()
    assert [t["status"] for t in store.tasks_by_status("failed")] == ["failed"]
