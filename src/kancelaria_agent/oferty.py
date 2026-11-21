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


_FWD_START = re.compile(r"^-{3,}\s*(?:forwarded message|wiadomość przekazana dalej|przekazana wiadomość|original message|wiadomość oryginalna)\s*-{3,}\s*$", re.IGNORECASE)
_FWD_HEAD = re.compile(r"^\s*(od|from|data|date|wysłano|sent|temat|subject|do|to|dw|cc)\s*:\s*(.*)$", re.IGNORECASE)
_HEAD_KEY = {"od": "from", "from": "from", "data": "date", "date": "date", "wysłano": "date", "sent": "date",
             "temat": "subject", "subject": "subject", "do": "to", "to": "to", "dw": "cc", "cc": "cc"}


def parse_forwarded(body: str) -> dict[str, Any] | None:
    """Przekazana dalej wiadomość w treści maila (Gmail/Outlook): nagłówki Od/Data/Temat/Do/DW i treść."""
    lines = str(body or "").replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if not _FWD_START.match(line.strip()):
            continue
        head: dict[str, str] = {}
        j = i + 1
        while j < len(lines):
            m = _FWD_HEAD.match(lines[j])
            if not m:
                if lines[j].strip() == "" and head:
                    j += 1
                    break
                if head:
                    break
                j += 1
                continue
            head[_HEAD_KEY[m.group(1).lower()]] = m.group(2).strip()
            j += 1
        if not head:
            return None
        split = lambda v: [a.strip() for a in re.split(r"[,;](?![^<]*>)", v or "") if a.strip()]
        return {"from": head.get("from", ""), "date": head.get("date", ""), "subject": head.get("subject", ""),
                "to": split(head.get("to", "")), "cc": split(head.get("cc", "")), "body": "\n".join(lines[j:]).strip()}
    return None


def _bare_subject(subject: str) -> str:
    return _SUBJECT_PREFIX.sub("", str(subject or "")).strip()


def is_forwarded_offer(payload: dict[str, Any], agent_email: str, team: set[str]) -> bool:
    """Wysłana wcześniej oferta przekazana Monice (biuro@ w „Do”) z tematem zaczynającym się od „Oferta”."""
    if payload.get("trusted") is not True or not agent_email:
        return False
    me = agent_email.lower()
    if me not in [_addr(a) for a in payload.get("to") or []]:
        return False
    if not re.match(r"oferta\b", _bare_subject(payload.get("subject", "")), re.IGNORECASE):
        return False
    # Prośba do Moniki z tematem „Oferta dla X – przygotuj…” to zlecenie, nie wpis: wymagamy przekazanej
    # wiadomości albo pliku oferty (PDF/Word) w załączniku.
    has_file = any(Path(str(n)).suffix.lower() in OFFER_FILE_EXTS for n in payload.get("attachments") or [])
    return has_file or parse_forwarded(payload.get("body", "")) is not None


def forwarded_view(payload: dict[str, Any], agent_email: str, team: set[str]) -> dict[str, Any]:
    """Payload przekazanej oferty widziany tak, jak oryginał: adresat = klient, data i treść z przekazanego maila."""
    fwd = parse_forwarded(payload.get("body", "")) or {}
    subject = re.sub(r"^oferta\s*[:\-–]?\s*", "", _bare_subject(payload.get("subject", "")), flags=re.IGNORECASE)
    subject = _bare_subject(subject) or fwd.get("subject") or "Oferta"
    date = payload.get("date")
    if fwd.get("date"):
        from email.utils import parsedate_to_datetime
        try:
            date = parsedate_to_datetime(fwd["date"]).isoformat()
        except (TypeError, ValueError):
            date = _pl_date(fwd["date"]) or date
    me = agent_email.lower()
    return {**payload, "subject": f"Oferta {subject}" if not re.match(r"oferta\b", subject, re.I) else subject,
            "to": [a for a in fwd.get("to") or [] if _addr(a) != me], "cc": fwd.get("cc") or [],
            "body": fwd.get("body") or payload.get("body", ""), "date": date, "forwarded": True}


_PL_MONTHS = {"sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6, "lip": 7, "sie": 8, "wrz": 9, "paź": 10, "paz": 10, "lis": 11, "gru": 12}


def _pl_date(v: str) -> str | None:
    """„czw., 20 lis 2026 o 10:15” / „20.11.2026 10:15” → ISO (czas lokalny bez strefy)."""
    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]{3})\w*\.?\s+(\d{4})(?:\D+(\d{1,2}):(\d{2}))?", v, re.IGNORECASE)
    if m and m.group(2).lower()[:3] in _PL_MONTHS:
        d, mth, y = int(m.group(1)), _PL_MONTHS[m.group(2).lower()[:3]], int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})(?:\D+(\d{1,2}):(\d{2}))?", v)
        if not m:
            return None
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh, mm = int(m.group(4) or 12), int(m.group(5) or 0)
    return f"{y:04d}-{mth:02d}-{d:02d}T{hh:02d}:{mm:02d}:00" if 1 <= d <= 31 and 1 <= mth <= 12 else None


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
