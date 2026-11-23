"""Skrzynka DW: mail wysłany przez kogoś z zespołu na zewnątrz, z Moniką w DW → wpis w panelu kancelarii.

Monika nic nie odpisuje i nikomu nie wysyła maila – przekazuje do panelu kopię (nadawca, adresaci, temat,
treść bez cytatu, załączniki-dokumenty), a prawnik w panelu przypisuje mail do sprawy jednym kliknięciem.
Oferty mają własną drogę (oferty.py) – ten moduł obsługuje wszystkie pozostałe maile w DW.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from .projects import strip_quoted
from .oferty import _addr, redact_accounts

DW_FILE_EXTS = {".pdf", ".docx", ".doc", ".odt", ".xlsx", ".xls", ".jpg", ".jpeg", ".png", ".txt", ".rtf"}
MAX_DW_FILE = 25 * 1024 * 1024
MAX_DW_FILES = 10


def is_dw_mail(payload: dict[str, Any], agent_email: str, team: set[str]) -> bool:
    """Zweryfikowana osoba z zespołu pisze do kogoś spoza kancelarii, a Monika jest w DW (nie w „Do”)."""
    if payload.get("trusted") is not True or not agent_email:
        return False
    me = agent_email.lower()
    to = [_addr(a) for a in payload.get("to") or []]
    cc = [_addr(a) for a in payload.get("cc") or []]
    if me not in cc or me in to:
        return False
    return any(a and a not in team and a != me for a in to + cc)


def dw_files_dir(projects_dir: Path, message_id: str) -> Path:
    return projects_dir.parent / "dw" / hashlib.sha256(message_id.encode()).hexdigest()[:20]


def take_dw_files(projects_dir: Path, payload: dict[str, Any]) -> list[str]:
    """Załączniki-dokumenty → data/dw/<id>/; zwraca ścieżki względem data/dw (np. „abc123/pismo.pdf”)."""
    staged = payload.get("staged")
    if not staged:
        return []
    src = Path(staged)
    if not src.is_dir() or src.parent != projects_dir.parent / "przychodzace":
        return []
    out: list[str] = []
    dest = dw_files_dir(projects_dir, payload["message_id"])
    for f in sorted(src.iterdir()):
        if len(out) >= MAX_DW_FILES:
            break
        if f.is_file() and not f.is_symlink() and f.suffix.lower() in DW_FILE_EXTS and 0 < f.stat().st_size <= MAX_DW_FILE:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), dest / f.name)
            out.append(f"{dest.name}/{f.name}")
    shutil.rmtree(src, ignore_errors=True)
    return out


def extract_dw(payload: dict[str, Any], files: list[str] | None = None) -> dict:
    """Dane dla panelu (POST /api/monika/dw). Treść bez cytowanej historii, numery rachunków zamaskowane."""
    return {
        "message_id": str(payload.get("message_id", "")),
        "from": str(payload.get("sender", "")),
        "to": [str(a) for a in payload.get("to") or []],
        "cc": [str(a) for a in payload.get("cc") or []],
        "subject": redact_accounts(str(payload.get("subject", "")))[:500],
        "date": payload.get("date"),
        "body": redact_accounts(strip_quoted(payload.get("body", ""), max_chars=20000)),
        "files": list(files or []),
    }
