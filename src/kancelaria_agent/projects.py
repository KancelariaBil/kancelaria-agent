"""Sprawy: katalog per klient/sprawa, wątek maili, bezpieczne ścieżki.

W kodzie sprawa nazywa się „project” (nazwa historyczna); w promptach – „sprawa”.
Układ na dysku (data/projects/<slug>/):
- dokumenty/  – załączniki z maili zespołu (<data>/<plik>); Monika tylko czyta,
- pisma/      – dokumenty przygotowane przez Monikę (.docx, notatki) do wysłania zespołowi.
Slug (klient-sprawa) to nazwa katalogu: [a-z0-9-], maks. 50 znaków.
"""

from __future__ import annotations

import json
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import Store

SUBDIRS = ("dokumenty", "pisma")
TOKEN_DAYS = 14
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_TAG_RE = re.compile(r"\[#(\d{1,9})\]")
_PL = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")
# Początek cytatu w odpowiedzi – do historii trafia tylko nowa treść.
# Przekazane wiadomości (Fwd) zostają – to treść od klienta, której Monika potrzebuje w historii.
_QUOTE_RE = re.compile(r"^\s*(?:>|w dniu .+ napisał|on .+ wrote:|-{2,}\s*(?:original|oryginalna))", re.IGNORECASE)


class PathError(ValueError):
    pass


def slugify(text: str, max_len: int = 50) -> str:
    text = unicodedata.normalize("NFKD", str(text).translate(_PL)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-")


def safe_filename(name: str, default: str = "plik") -> str:
    """Nazwa pliku z załącznika: bez katalogów, polskich znaków i znaków specjalnych; rozszerzenie zostaje."""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = slugify(stem, 80) or default
    ext = re.sub(r"[^a-z0-9]", "", ext.lower())[:8]
    return f"{stem}.{ext}" if ext else stem


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def safe_relpath(rel: str) -> Path:
    """Ścieżka względna bez '..', ukrytych plików i znaków specjalnych (fail closed)."""
    rel = str(rel).replace("\\", "/").strip()
    if not rel or rel.startswith("/") or len(rel) > 300:
        raise PathError(f"niedozwolona ścieżka: {rel!r}")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or len(parts) > 8 or not all(_SEGMENT_RE.match(p) and ".." not in p for p in parts):
        raise PathError(f"niedozwolona ścieżka: {rel!r}")
    return Path(*parts)


def resolve_inside(base: Path, rel: str) -> Path:
    """Pełna ścieżka wewnątrz `base`; odrzuca wyjście poza katalog, także przez dowiązania."""
    target = (base / safe_relpath(rel)).resolve()
    root = base.resolve()
    if target != root and root not in target.parents:
        raise PathError(f"ścieżka poza katalogiem projektu: {rel!r}")
    return target


def subject_tag(project_id: int) -> str:
    return f"[#{project_id}]"


def tag_in(subject: str) -> int | None:
    m = _TAG_RE.search(subject or "")
    return int(m.group(1)) if m else None


_REPLY_PREFIX_RE = re.compile(r"^\s*(?:re|odp|aw|sv)\s*(?:\[\d+\])?\s*:\s*", re.IGNORECASE)
_ANY_PREFIX_RE = re.compile(r"^\s*(?:re|odp|aw|sv|fwd?|pd|wg|tr)\s*(?:\[\d+\])?\s*:\s*", re.IGNORECASE)


def is_reply_subject(subject: str) -> bool:
    return bool(_REPLY_PREFIX_RE.match(subject or ""))


def base_subject(subject: str) -> str:
    """Temat bez Re:/Odp:/Fwd:/PD: i tagu [#id] – do rozpoznania wątku."""
    s = _TAG_RE.sub("", subject or "")
    while True:
        new = _ANY_PREFIX_RE.sub("", s, count=1)
        if new == s:
            break
        s = new
    return " ".join(s.split()).lower()


def reply_subject(subject: str) -> str:
    """„Re: <temat>” bez narastającego „Re: Re:”."""
    s = subject or ""
    while (new := _REPLY_PREFIX_RE.sub("", s, count=1)) != s:
        s = new
    return f"Re: {s.strip()}"[:200]


def strip_quoted(body: str, max_chars: int = 8000) -> str:
    out = []
    for line in str(body).splitlines():
        if _QUOTE_RE.match(line):
            break
        out.append(line)
    return "\n".join(out).strip()[:max_chars]


MAX_QUOTE = 40000


def quote_block(sender: str, date: str | None, body: str) -> str:
    """Cytat jak w Gmailu: „W dniu …, … napisał:” i linie z „>”. Numery kont w cytacie są maskowane –
    bezpiecznik danych płatniczych blokowałby inaczej każdą odpowiedź na mail ze stopką z kontem."""
    from email.utils import parsedate_to_datetime
    from zoneinfo import ZoneInfo

    from .guards import _IBAN_RE
    when = ""
    if date:
        try:
            d = parsedate_to_datetime(date).astimezone(ZoneInfo("Europe/Warsaw"))
            when = f"W dniu {d:%d.%m.%Y} o {d:%H:%M} "
        except (TypeError, ValueError):
            when = ""
    text = _IBAN_RE.sub("[numer konta ukryty]", str(body).replace("\r\n", "\n").strip())
    if len(text) > MAX_QUOTE:
        text = text[:MAX_QUOTE] + "\n[…]"
    quoted = "\n".join(">" + (" " + line if line and not line.startswith(">") else line)
                       for line in text.split("\n"))
    return f"{when}{sender} napisał:\n{quoted}"


class Projects:
    """Rejestr projektów: baza (Store) + katalogi na dysku."""

    def __init__(self, store: Store, root: Path, web=None):
        self.store = store
        self.root = Path(root)
        self._memory = None

    @property
    def memory(self):
        """Pamięć Moniki (ludzie, firmy, archiwum) – w tej samej bazie, ponad sprawami."""
        if self._memory is None:
            from .memory import Memory
            self._memory = Memory(self.store.db)
        return self._memory

    # -- ścieżki --
    def dir(self, project) -> Path:
        return self.root / project["slug"]

    def ensure_dirs(self, project) -> Path:
        d = self.dir(project)
        for sub in SUBDIRS:
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d

    # -- cykl życia --
    def create(self, sender: str, subject: str, reply_to: str | None = None):
        expires = (now_utc() + timedelta(days=TOKEN_DAYS)).isoformat()
        tmp = "nowy-" + secrets.token_hex(6)
        pid = self.store.create_project(tmp, sender, subject, reply_to, secrets.token_urlsafe(24), expires)
        self.store.update_project(pid, slug=f"sprawa-{pid}")
        project = self.store.get_project(pid)
        self.ensure_dirs(project)
        return project

    def find_for_mail(self, subject: str, refs: list[str], sender: str | None = None):
        """Projekt z tagu [#id], z nagłówków In-Reply-To/References, a dla odpowiedzi (Re:/Odp:)
        bez tych nagłówków – z tematu wątku (ten sam nadawca, ostatnie 90 dni)."""
        # Tagu [#id] w temacie NIE używamy: systemy zgłoszeń wstawiają takie tagi do tematów, a mail
        # trafiłby do cudzej sprawy (z dokumentami i pamięcią tamtej sprawy).
        p = self.store.project_by_message(refs) if refs else None
        if p is None and sender and is_reply_subject(subject):
            base = base_subject(subject)
            since = (now_utc() - timedelta(days=90)).isoformat()
            rows = [r for r in self.store.db.execute(
                "SELECT * FROM projects WHERE sender=? AND updated_at>=? ORDER BY updated_at DESC", (sender, since))
                if base and base_subject(r["subject"]) == base]
            # Projekt założony od zwykłego tematu ma pierwszeństwo przed założonym od samej odpowiedzi.
            rows.sort(key=lambda r: is_reply_subject(r["subject"]))
            if rows:
                return rows[0]
        return p

    def name(self, project_id: int, client: str, name: str):
        """Nadaje projektowi docelową nazwę katalogu (raz). Zwraca aktualny projekt."""
        p = self.store.get_project(project_id)
        if p["named"]:
            return p
        base = slugify(f"{client} {name}") or f"sprawa-{project_id}"
        slug, n = base, 2
        while (other := self.store.project_by_slug(slug)) is not None and other["id"] != project_id \
                or (self.root / slug).exists() and slug != p["slug"]:
            slug = f"{base[:46]}-{n}"
            n += 1
        old, new = self.dir(p), self.root / slug
        if old != new:
            if old.exists():
                old.rename(new)
        self.store.update_project(project_id, slug=slug, client=str(client)[:120], name=str(name)[:120], named=1)
        if str(client).strip():
            self.memory.link_project_org(project_id, str(client))
        p = self.store.get_project(project_id)
        self.ensure_dirs(p)
        return p

    # -- pliki --
    def list_files(self, project, sub: str | None = None, limit: int = 300) -> list[tuple[str, int]]:
        d = self.dir(project)
        out = []
        for s in ([sub] if sub else SUBDIRS):
            base = d / s
            if not base.is_dir():
                continue
            for f in sorted(base.rglob("*")):
                if f.is_file() and not f.is_symlink() and not any(x.startswith(".") for x in f.relative_to(d).parts):
                    out.append((str(f.relative_to(d)), f.stat().st_size))
                    if len(out) >= limit:
                        return out
        return out

    def context(self, project) -> str:
        """Opis sprawy dla modelu: historia, dokumenty."""
        lines = [f"Sprawa #{project['id']} – klient: {project['client'] or '(nieustalony)'}, "
                 f"nazwa: {project['name'] or '(nieustalona)'}, katalog: {project['slug']}",
                 f"Etap: {project['stage']}",
                 f"Zlecił(a): {project['sender']}",
                 *([f"NOTATKA ZESPOŁU do tej sprawy (stosuj się do niej): {project['note']}"]
                   if project["note"] else []),
                 *(["Sprawa WSTRZYMANA przez zespół."] if project["status"] == "paused" else [])]
        files = self.list_files(project)
        lines.append("Pliki w katalogu sprawy:" + ("" if files else " brak"))
        lines += [f"- {name} ({size // 1024} KB)" for name, size in files[:150]]
        log = self.store.project_log(project["id"], limit=16)
        if log:
            lines.append("Historia (najstarsze na górze):")
            for r in log:
                who = {"zespol": "Zespół", "lukasz": "Zespół", "monika": "Ty (Monika)", "marek": "Ty (Monika)"
                       }.get(r["role"], "system")
                lines.append(f"[{r['created_at'][:16]}] {who}:\n{r['text'][:4000]}")
        return "\n".join(lines)
