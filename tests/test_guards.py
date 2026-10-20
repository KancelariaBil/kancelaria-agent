import pytest

from kancelaria_agent import guards
from kancelaria_agent.guards import Verdict

MAIL = {"to": "biuro@klient.pl", "subject": "Re", "body": "Dzień dobry"}
INVOICE = {"client": "Klient Sp. z o.o.", "items": [], "amount_pln": "500"}


def verdict(cfg, kind, payload):
    return guards.evaluate(cfg, kind, payload).verdict


def test_allowed_recipient_passes(make_env):
    cfg = make_env()[0]
    assert verdict(cfg, guards.SEND_MAIL, MAIL) is Verdict.ALLOW


@pytest.mark.parametrize("to", ["obcy@gmail.com", "x@klient.pl.evil.com", "a@b@klient.pl", "", None])
def test_unlisted_recipient_denied(make_env, to):
    cfg = make_env()[0]
    assert verdict(cfg, guards.SEND_MAIL, {**MAIL, "to": to}) is Verdict.DENY


def test_cc_and_bcc_also_checked(make_env):
    cfg = make_env()[0]
    assert verdict(cfg, guards.SEND_MAIL, {**MAIL, "cc": ["obcy@gmail.com"]}) is Verdict.DENY
    assert verdict(cfg, guards.SEND_MAIL, {**MAIL, "bcc": ["obcy@gmail.com"]}) is Verdict.DENY


def test_empty_allowlist_denies_everything(make_env):
    cfg = make_env(guardrails={"allowed_recipients": []})[0]
    assert verdict(cfg, guards.SEND_MAIL, MAIL) is Verdict.DENY


def test_unknown_client_denied(make_env):
    cfg = make_env()[0]
    assert verdict(cfg, guards.CREATE_INVOICE, {**INVOICE, "client": "Oszust"}) is Verdict.DENY
    assert verdict(cfg, guards.CREATE_NOTION_TASK, {"title": "t", "client": "Oszust"}) is Verdict.DENY


def test_known_client_invoice_under_limit_allowed(make_env):
    cfg = make_env()[0]
    assert verdict(cfg, guards.CREATE_INVOICE, INVOICE) is Verdict.ALLOW


def test_no_amount_limit_by_default():
    from pathlib import Path
    from kancelaria_agent.config import load_config
    cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert cfg.guardrails.max_amount_without_approval_pln is None   # bez progu kwotowego


def test_amount_over_limit_requires_approval(make_env):
    cfg = make_env()[0]
    assert verdict(cfg, guards.CREATE_INVOICE, {**INVOICE, "amount_pln": "1000.01"}) is Verdict.REQUIRE_APPROVAL
    assert verdict(cfg, guards.CREATE_INVOICE, {**INVOICE, "amount_pln": "1000"}) is Verdict.ALLOW


def test_amount_limit_applies_to_notion_quote_too(make_env):
    cfg = make_env()[0]
    payload = {"title": "Wycena", "client": "Klient Sp. z o.o.", "amount_pln": 5000}
    assert verdict(cfg, guards.CREATE_NOTION_TASK, payload) is Verdict.REQUIRE_APPROVAL


@pytest.mark.parametrize("amount", ["abc", "-5", "NaN", "Infinity", None])
def test_invalid_amount_denied(make_env, amount):
    cfg = make_env()[0]
    assert verdict(cfg, guards.CREATE_INVOICE, {**INVOICE, "amount_pln": amount}) is Verdict.DENY


def test_missing_amount_on_invoice_denied(make_env):
    cfg = make_env()[0]
    assert verdict(cfg, guards.CREATE_INVOICE, {"client": "Klient Sp. z o.o."}) is Verdict.DENY


@pytest.mark.parametrize("payload", [
    {"title": "t", "properties": {"IBAN": "x"}},
    {"title": "t", "properties": {"Numer konta": "x"}},
    {"title": "t", "properties": {"nested": [{"bank_account": "x"}]}},
    {"title": "t", "properties": {"note": "Nowe konto: pl61 1090 1014 0000 0712 1981 2874"}},
])
def test_payment_data_changes_denied(make_env, payload):
    cfg = make_env()[0]
    assert verdict(cfg, guards.UPDATE_NOTION_TASK, {"task_id": "1", **payload}) is Verdict.DENY


def test_payment_data_in_mail_body_denied(make_env):
    cfg = make_env()[0]
    body = "Proszę przelewać na DE89370400440532013000"
    assert verdict(cfg, guards.SEND_MAIL, {**MAIL, "body": body}) is Verdict.DENY


def test_unknown_action_denied(make_env):
    cfg = make_env()[0]
    assert verdict(cfg, "delete_everything", {}) is Verdict.DENY


def test_stop_file_denies_everything(make_env):
    cfg = make_env()[0]
    assert verdict(cfg, guards.SEND_MAIL, MAIL) is Verdict.ALLOW
    cfg.stop_file.write_text("")
    assert guards.kill_switch_active(cfg)
    assert verdict(cfg, guards.SEND_MAIL, MAIL) is Verdict.DENY


def test_config_change_at_runtime_denies(make_env):
    cfg = make_env()[0]
    assert not guards.config_tampered(cfg)
    cfg.source_path.write_text(cfg.source_path.read_text() + "\n# zmiana\n")
    assert guards.config_tampered(cfg)
    assert verdict(cfg, guards.SEND_MAIL, MAIL) is Verdict.DENY


def test_config_deleted_counts_as_tampered(make_env):
    cfg = make_env()[0]
    cfg.source_path.unlink()
    assert guards.config_tampered(cfg)


@pytest.mark.parametrize("text", [
    "DE89 3704 0044 0532 0130 00",
    "PL61109010140000071219812874",
    "61 1090 1014 0000 0712 1981 2874",
])
def test_account_number_formats_detected(text):
    assert guards.contains_payment_data({"body": text})


def test_ordinary_text_not_flagged():
    assert not guards.contains_payment_data({"body": "Spotkanie 12.10 o 14:00, oferta nr 2024/10/15"})
