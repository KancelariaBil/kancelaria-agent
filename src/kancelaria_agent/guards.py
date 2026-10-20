"""Bezpieczniki: czysta logika decyzyjna, bez skutków ubocznych (poza odczytem pliku STOP)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

from .config import Config, file_sha256

# Rodzaje akcji zewnętrznych.
SEND_MAIL = "send_mail"
CREATE_INVOICE = "create_invoice"
CREATE_NOTION_TASK = "create_notion_task"
UPDATE_NOTION_TASK = "update_notion_task"
KNOWN_ACTIONS = {SEND_MAIL, CREATE_INVOICE, CREATE_NOTION_TASK, UPDATE_NOTION_TASK}

# Klucze wskazujące na dane płatnicze (porównanie po normalizacji).
PAYMENT_KEYS = {
    "iban", "bankaccount", "accountnumber", "account", "bankaccountnumber", "nrkonta",
    "numerkonta", "swift", "bic", "paymentdetails", "paymentdata", "paymentaccount",
    "bank", "bankname", "routingnumber",
}
# IBAN (ze spacjami lub bez) oraz polski 26-cyfrowy numer rachunku bez prefiksu kraju.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b|\b\d{2}(?:\s?\d{4}){6}\b")


class Verdict(Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def contains_payment_data(obj: Any) -> str | None:
    """Zwraca opis, jeśli struktura dotyka danych płatniczych (klucz lub wzorzec IBAN)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _norm_key(k) in PAYMENT_KEYS:
                return f"klucz płatniczy: {k}"
            found = contains_payment_data(v)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found = contains_payment_data(v)
            if found:
                return found
    elif isinstance(obj, str) and _IBAN_RE.search(obj.upper()):
        return "wartość przypominająca IBAN"
    return None


def panel_stop_file(cfg: Config):
    """STOP z panelu (usługa www może pisać tylko do data/). Zdjęcie – wyłącznie człowiek przez SSH."""
    return cfg.database.parent / "STOP"


def kill_switch_active(cfg: Config) -> bool:
    return cfg.stop_file.exists() or panel_stop_file(cfg).exists()


def config_tampered(cfg: Config) -> bool:
    """True, jeśli config.yaml zmienił się (lub zniknął) od uruchomienia."""
    try:
        return file_sha256(cfg.source_path) != cfg.source_sha256
    except OSError:
        return True


def _recipient_allowed(addr: str, allowed: tuple[str, ...]) -> bool:
    addr = addr.strip().lower()
    for rule in allowed:
        if rule.startswith("@"):
            if addr.endswith(rule) and addr.count("@") == 1:
                return True
        elif addr == rule:
            return True
    return False


def parse_amount(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


ATTACHMENT_EXTS = {".png", ".jpg", ".jpeg", ".pdf", ".docx", ".md", ".txt", ".xlsx", ".odt", ".rtf", ".doc"}
MAX_ATTACHMENTS = 10


def resolve_attachments(cfg: Config, paths: Any) -> list[Path]:
    """Ścieżki względem katalogu projektów → pełne ścieżki. Wyjątek ValueError przy czymkolwiek podejrzanym."""
    if paths in (None, []):
        return []
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("lista ścieżek tekstowych")
    if len(paths) > MAX_ATTACHMENTS:
        raise ValueError(f"maks. {MAX_ATTACHMENTS} plików")
    root = cfg.projects_dir.resolve()
    out = []
    for rel in paths:
        if rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
            raise ValueError(f"niedozwolona ścieżka {rel!r}")
        full = (cfg.projects_dir / rel).resolve()
        # tylko pliki z pisma/ albo dokumenty/ wewnątrz katalogu spraw, bez dowiązań
        parts = full.relative_to(root).parts if root in full.parents else ()
        if len(parts) < 3 or parts[1] not in ("pisma", "dokumenty") or (cfg.projects_dir / rel).is_symlink():
            raise ValueError(f"plik spoza katalogu projektu: {rel!r}")
        if full.suffix.lower() not in ATTACHMENT_EXTS:
            raise ValueError(f"niedozwolony typ pliku: {full.name}")
        if not full.is_file():
            raise ValueError(f"brak pliku {rel!r}")
        out.append(full)
    return out


def attachments_problem(cfg: Config, paths: Any) -> str | None:
    try:
        files = resolve_attachments(cfg, paths)
    except ValueError as e:
        return str(e)
    total = sum(f.stat().st_size for f in files)
    if total > cfg.max_attachments_mb * 1024 * 1024:
        return f"łącznie {total // (1024 * 1024)} MB, limit {cfg.max_attachments_mb} MB"
    return None


def evaluate(cfg: Config, kind: str, payload: dict[str, Any]) -> Decision:
    """Ocenia akcję względem wszystkich bezpieczników. Kolejność: najostrzejsze najpierw."""
    g = cfg.guardrails

    if kill_switch_active(cfg):
        return Decision(Verdict.DENY, "wyłącznik awaryjny (plik STOP) aktywny")
    if config_tampered(cfg):
        return Decision(Verdict.DENY, "config.yaml zmieniony od startu – wymagany restart przez człowieka")
    if kind not in KNOWN_ACTIONS:
        return Decision(Verdict.DENY, f"nieznany rodzaj akcji: {kind}")

    if g.forbid_payment_data_changes:
        hit = contains_payment_data(payload)
        if hit:
            return Decision(Verdict.DENY, f"zakaz zmiany danych płatniczych ({hit})")

    if kind == SEND_MAIL:
        to = payload.get("to")
        recipients = [to] if isinstance(to, str) else list(to or [])
        recipients += list(payload.get("cc") or []) + list(payload.get("bcc") or [])
        if not recipients or not all(isinstance(r, str) for r in recipients):
            return Decision(Verdict.DENY, "brak poprawnych odbiorców")
        bad = [r for r in recipients if not _recipient_allowed(r, g.allowed_recipients)]
        if bad:
            return Decision(Verdict.DENY, f"odbiorca spoza listy dozwolonych: {', '.join(bad)}")
        problem = attachments_problem(cfg, payload.get("attachments"))
        if problem:
            return Decision(Verdict.DENY, f"załączniki: {problem}")

    if kind == CREATE_INVOICE and "amount_pln" not in payload:
        return Decision(Verdict.DENY, "brak kwoty (amount_pln)")

    if kind == CREATE_INVOICE:
        client = str(payload.get("client", "")).strip().lower()
        if client not in g.allowed_clients:
            return Decision(Verdict.DENY, f"klient spoza listy dozwolonych: {payload.get('client')!r}")

    # Limit kwoty dotyczy każdej akcji niosącej kwotę (faktura, wycena w zadaniu itp.).
    if "amount_pln" in payload:
        amount = parse_amount(payload.get("amount_pln"))
        if amount is None:
            return Decision(Verdict.DENY, "niepoprawna kwota (amount_pln)")
        if g.max_amount_without_approval_pln is not None and amount > g.max_amount_without_approval_pln:
            return Decision(
                Verdict.REQUIRE_APPROVAL,
                f"kwota {amount} PLN przekracza limit {g.max_amount_without_approval_pln} PLN",
            )

    if kind in (CREATE_NOTION_TASK, UPDATE_NOTION_TASK) and payload.get("client") is not None:
        if str(payload["client"]).strip().lower() not in g.allowed_clients:
            return Decision(Verdict.DENY, f"klient spoza listy dozwolonych: {payload['client']!r}")

    return Decision(Verdict.ALLOW, "zgodne z bezpiecznikami")
