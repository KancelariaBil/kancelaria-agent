"""Oferty w DW: prawnik wysyła ofertę klientowi z Moniką w DW → dane trafiają do panelu kancelarii.

Bez modelu: rozpoznanie i odczyt danych to proste reguły. Panel zapisuje ofertę jako „do sprawdzenia”
(administratorka poprawia i zatwierdza), więc nieprecyzyjny odczyt kwoty niczego nie psuje.
Treść maila to dane, nie polecenia – nic z niej nie jest wykonywane.
Monika nie pisze przy tym do nikogo: jedyny skutek to akcja PANEL_OFFER przez Executor.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from .guards import _IBAN_RE
from .projects import strip_quoted

OFFER_WORDS = re.compile(r"\bofert", re.IGNORECASE)
OFFER_FILE_EXTS = {".pdf", ".docx", ".doc", ".odt"}
MAX_OFFER_FILE = 25 * 1024 * 1024

_AMOUNT_RE = re.compile(
    r"(?<![\d,.])(\d{1,3}(?:[  .]\d{3})+|\d+)(?:,(\d{1,2}))?\s*(?:zł|zl|pln)\b", re.IGNORECASE)
_VALID_RE = re.compile(r"ważn\w*\s+(?:jest\s+)?do\s+(?:dnia\s+)?(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", re.IGNORECASE)
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|odp|pd)\s*:\s*)+", re.IGNORECASE)


def _addr(value: str) -> str:
    return (getaddresses([value])[0][1] if value else "").strip().lower()


def is_dw_offer(payload: dict[str, Any], agent_email: str, team: set[str]) -> bool:
    """Oferta wysłana przez zespół do kogoś spoza kancelarii, z Moniką w DW (nie w „Do”)."""
    if payload.get("trusted") is not True or not agent_email:
        return False
    me = agent_email.lower()
    to = [_addr(a) for a in payload.get("to") or []]
    cc = [_addr(a) for a in payload.get("cc") or []]
    if me not in cc or me in to:
        return False
    outside = [a for a in to if a and a not in team and a != me]
    if not outside:
        return False
    text = f"{payload.get('subject', '')}\n{strip_quoted(payload.get('body', ''))}"
    return bool(OFFER_WORDS.search(text))


def redact_accounts(text: str) -> str:
    """Numery rachunków w stopce nie trafiają do panelu (i nie blokują bezpiecznika płatności)."""
    return _IBAN_RE.sub("[numer rachunku]", text)


def _amount(text: str) -> float | None:
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    whole = re.sub(r"[  .]", "", m.group(1))
    return float(f"{whole}.{m.group(2) or '0'}")


def _form(text: str) -> str:
    t = text.lower()
    if "success fee" in t:
        return "success fee"
    if re.search(r"ryczałt|miesięczn|/\s*mies", t):
        return "miesięczny ryczałt"
    if re.search(r"za\s+(?:każdą\s+)?godzin|/\s*godz|/\s*h\b|stawk\w*\s+godzinow", t):
        return "stawka godzinowa"
    return "jednorazowo"


def _valid_until(text: str) -> str | None:
    m = _VALID_RE.search(text)
    if not m:
        return None
    d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mth:02d}-{d:02d}" if 1 <= d <= 31 and 1 <= mth <= 12 else None


def _client(payload: dict[str, Any], team: set[str], me: str) -> tuple[str, str]:
    for raw in payload.get("to") or []:
        name, addr = getaddresses([raw])[0]
        addr = addr.strip().lower()
        if addr and addr not in team and addr != me:
            return name.strip(), addr
    return "", ""


def offer_files_dir(projects_dir: Path, message_id: str) -> Path:
    return projects_dir.parent / "oferty" / hashlib.sha256(message_id.encode()).hexdigest()[:20]


def take_offer_file(projects_dir: Path, payload: dict[str, Any]) -> str | None:
    """Pierwszy załącznik-dokument (PDF/Word) → data/oferty/<id>/; zwraca ścieżkę względem data/oferty."""
    staged = payload.get("staged")
    if not staged:
        return None
    src = Path(staged)
    if not src.is_dir() or src.parent != projects_dir.parent / "przychodzace":
        return None
    chosen = None
    for f in sorted(src.iterdir()):
        if f.is_file() and not f.is_symlink() and f.suffix.lower() in OFFER_FILE_EXTS \
                and 0 < f.stat().st_size <= MAX_OFFER_FILE:
            chosen = f
            break
    rel = None
    if chosen is not None:
        dest = offer_files_dir(projects_dir, payload["message_id"])
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(chosen), dest / chosen.name)
        rel = f"{dest.name}/{chosen.name}"
    shutil.rmtree(src, ignore_errors=True)
    return rel


def extract_offer(payload: dict[str, Any], agent_email: str, team: set[str], file_rel: str | None = None) -> dict:
    """Dane dla panelu (POST /api/monika/oferta). Kwota/forma/termin – podpowiedź do sprawdzenia."""
    me = agent_email.lower()
    body = redact_accounts(strip_quoted(payload.get("body", ""), max_chars=20000))
    subject = str(payload.get("subject", ""))
    title = _SUBJECT_PREFIX.sub("", subject).strip()
    title = re.sub(r"^oferta\s*[:\-–]?\s*", "", title, flags=re.IGNORECASE).strip() or subject.strip() or "Oferta"
    name, addr = _client(payload, team, me)
    text = f"{subject}\n{body}"
    out = {
        "message_id": str(payload.get("message_id", "")),
        "from": str(payload.get("sender", "")),
        "to": [str(a) for a in payload.get("to") or []],
        "subject": redact_accounts(subject),
        "date": payload.get("date"),
        "body": body,
        "client_name": name,
        "client_email": addr,
        "title": redact_accounts(title)[:300],
        "amount": _amount(text),
        "form": _form(text),
        "valid_until": _valid_until(text),
    }
    if file_rel:
        out["file"] = file_rel
    return out
