import json

import pytest
import yaml

from kancelaria_agent import guards
from kancelaria_agent.config import ConfigError, load_config

MAIL = {"to": "biuro@klient.pl", "subject": "Re", "body": "Dzień dobry"}
INVOICE = {"client": "Klient Sp. z o.o.", "items": [], "amount_pln": "500"}


def audit_events(cfg):
    return [json.loads(l) for l in cfg.audit_log.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("value", [None, "false", "no", 0, "", [], "False"])
def test_only_literal_false_disables_dry_run(tmp_path, value):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"dry_run": value}))
    assert load_config(tmp_path / "config.yaml").dry_run is True


def test_dry_run_default_when_key_missing(tmp_path):
    (tmp_path / "config.yaml").write_text("integrations: mock\n")
    assert load_config(tmp_path / "config.yaml").dry_run is True


def test_literal_false_disables(tmp_path):
    (tmp_path / "config.yaml").write_text("dry_run: false\n")
    assert load_config(tmp_path / "config.yaml").dry_run is False


def test_repo_config_sends_only_to_team():
    """Konfiguracja w repo: wysyłka wyłącznie do zespołu kancelarii, bez odpowiedzi do klientów.

    Dopuszczenie odbiorców spoza kancelarii albo odpowiedzi do klientów wymaga świadomej zmiany tego
    testu w PR i zgody osoby zatwierdzającej (CLAUDE.md, zasada 1).
    """
    from kancelaria_agent.config import PROJECT_ROOT
    cfg = load_config(PROJECT_ROOT / "config.yaml")
    assert cfg.guardrails.client_replies is False
    assert cfg.integrations in ("mock", "gmail")
    assert cfg.team and all(t.endswith("@kancelariabil.pl") for t in cfg.team)
    assert all(r in cfg.team for r in cfg.guardrails.allowed_recipients)


def test_real_integrations_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text("integrations: real\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "config.yaml")


@pytest.mark.parametrize("kind,payload", [
    (guards.SEND_MAIL, MAIL),
    (guards.CREATE_INVOICE, INVOICE),
    (guards.CREATE_NOTION_TASK, {"title": "Zadanie", "client": "Klient Sp. z o.o."}),
])
def test_dry_run_proposes_instead_of_executing(make_env, kind, payload):
    cfg, store, audit, integ, ex = make_env()
    out = ex.submit(kind, payload, task_id=1)
    assert out.status == "proposed" and out.proposal_id
    assert integ.mail.sent == [] and integ.board.tasks == {} and integ.invoices.invoices == []
    assert [p["id"] for p in store.list_proposals("pending")] == [out.proposal_id]


def test_denied_action_creates_no_proposal(make_env):
    cfg, store, audit, integ, ex = make_env()
    out = ex.submit(guards.SEND_MAIL, {**MAIL, "to": "obcy@gmail.com"})
    assert out.status == "denied"
    assert store.list_proposals(None) == []
    assert integ.mail.sent == []


def test_approve_executes_once(make_env):
    cfg, store, audit, integ, ex = make_env()
    pid = ex.submit(guards.SEND_MAIL, MAIL).proposal_id
    out = ex.approve(pid)
    assert out.status == "executed" and len(integ.mail.sent) == 1
    assert store.get_proposal(pid)["status"] == "executed"
    # Drugie zatwierdzenie nie wysyła ponownie.
    assert ex.approve(pid).status == "failed"
    assert len(integ.mail.sent) == 1


def test_reject_prevents_execution(make_env):
    cfg, store, audit, integ, ex = make_env()
    pid = ex.submit(guards.SEND_MAIL, MAIL).proposal_id
    assert ex.reject(pid) is True
    assert ex.approve(pid).status == "failed"
    assert integ.mail.sent == []


def test_stop_file_blocks_approval(make_env):
    cfg, store, audit, integ, ex = make_env()
    pid = ex.submit(guards.SEND_MAIL, MAIL).proposal_id
    cfg.stop_file.write_text("")
    assert ex.approve(pid).status == "denied"
    assert integ.mail.sent == []


def test_live_mode_executes_allowed_but_still_proposes_over_limit(make_env):
    cfg, store, audit, integ, ex = make_env(dry_run=False)
    assert cfg.dry_run is False
    assert ex.submit(guards.SEND_MAIL, MAIL).status == "executed"
    assert len(integ.mail.sent) == 1
    assert ex.submit(guards.CREATE_INVOICE, INVOICE).status == "executed"
    big = ex.submit(guards.CREATE_INVOICE, {**INVOICE, "amount_pln": "5000"})
    assert big.status == "proposed" and len(integ.invoices.invoices) == 1
    assert ex.approve(big.proposal_id).status == "executed"
    assert len(integ.invoices.invoices) == 2


def test_live_mode_still_denies_payment_data(make_env):
    cfg, store, audit, integ, ex = make_env(dry_run=False)
    out = ex.submit(guards.UPDATE_NOTION_TASK, {"task_id": "1", "properties": {"iban": "x"}})
    assert out.status == "denied" and integ.board.tasks == {}


def test_every_decision_is_audited(make_env):
    cfg, store, audit, integ, ex = make_env()
    ex.submit(guards.SEND_MAIL, MAIL, task_id=7)
    ex.submit(guards.SEND_MAIL, {**MAIL, "to": "obcy@gmail.com"}, task_id=7)
    ev = audit_events(cfg)
    assert [e["event"] for e in ev] == ["decision", "proposal_created", "decision"]
    assert ev[0]["verdict"] == "allow" and ev[0]["dry_run"] is True
    assert ev[2]["verdict"] == "deny" and "gmail" in ev[2]["reason"]
    assert all("ts" in e for e in ev)


def test_audit_log_rotates(tmp_path):
    from kancelaria_agent.audit import AuditLog
    log = AuditLog(tmp_path / "a.jsonl", max_bytes=500, backup_count=2)
    for i in range(50):
        log.record("x", i=i, pad="y" * 50)
    log.close()
    assert (tmp_path / "a.jsonl.1").exists()
    assert not (tmp_path / "a.jsonl.3").exists()
