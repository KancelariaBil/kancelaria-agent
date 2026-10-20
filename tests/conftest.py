import pytest
import yaml

from kancelaria_agent.audit import AuditLog
from kancelaria_agent.config import load_config
from kancelaria_agent.executor import Executor
from kancelaria_agent.integrations import build_mock
from kancelaria_agent.store import Store

BASE = {
    "dry_run": True,
    "integrations": "mock",
    "team": ["ala@example.com", "basia@kancelariabil.pl", "aneta@kancelariabil.pl"],
    "guardrails": {
        "allowed_recipients": ["@klient.pl", "ala@example.com", "basia@kancelariabil.pl", "aneta@kancelariabil.pl"],
        "allowed_clients": ["Klient Sp. z o.o."],
        "forbid_payment_data_changes": True,
        "max_amount_without_approval_pln": 1000,
    },
}


@pytest.fixture
def make_env(tmp_path):
    """Fabryka środowiska; `overrides` nakładane płytko (guardrails łączone)."""
    made = []

    def _make(**overrides):
        raw = {**BASE, "guardrails": {**BASE["guardrails"], **overrides.pop("guardrails", {})}, **overrides}
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
        cfg = load_config(tmp_path / "config.yaml")
        store = Store(cfg.database)
        audit = AuditLog(cfg.audit_log, cfg.audit_max_bytes, cfg.audit_backup_count)
        integ = build_mock()
        made.append((store, audit))
        return cfg, store, audit, integ, Executor(cfg, store, audit, integ)

    yield _make
    for store, audit in made:
        store.close()
        audit.close()
