"""Baza wiedzy Moniki: pliki kancelarii (procedury, stanowiska, notatki, orzecznictwo), które Monika
przeszukuje i czyta przy każdym zleceniu.

- jeden płaski katalog (domyślnie data/wiedza), pliki wrzucane przez panel WWW albo mailem „wiedza”,
- tekst wyciągany raz i trzymany w ukrytym podkatalogu .tekst/ (odświeżany po zmianie pliku),
- wyszukiwanie słownikowe (bez polskich znaków i wielkości liter), wynik z fragmentami,
- Monika ma do bazy wyłącznie odczyt; zapis/usuwanie robi człowiek (panel, mail od zespołu).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .documents import DocumentError, docx_text, pdf_text, read_document
from .projects import safe_filename

EXTS = {".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".xlsx", ".xls", ".csv", ".html", ".htm"}
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 2000
CACHE = ".tekst"


class KnowledgeError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    size: int
    mtime: float


@dataclass(frozen=True)
class Hit:
    name: str
    score: int
    snippets: tuple[str, ...]


def fold(text: str) -> str:
    """Małe litery, bez polskich znaków – do porównań."""
    t = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def _terms(query: str) -> list[str]:
    return [w for w in re.findall(r"\w+", fold(query)) if len(w) >= 3][:12]


class KnowledgeBase:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- pliki ----
    def entries(self) -> list[Entry]:
        if not self.root.is_dir():
            return []
        out = []
        for f in self.root.iterdir():
            if f.is_file() and not f.is_symlink() and not f.name.startswith(".") and f.suffix.lower() in EXTS:
                st = f.stat()
                out.append(Entry(f.name, st.st_size, st.st_mtime))
        return sorted(out, key=lambda e: e.name)

    def path(self, name: str) -> Path:
        base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
        if not base or base.startswith(".") or base != name:
            raise KnowledgeError(f"niepoprawna nazwa pliku: {name!r}")
        p = self.root / base
        if not p.is_file() or p.is_symlink() or p.suffix.lower() not in EXTS:
            raise KnowledgeError(f"brak pliku w bazie wiedzy: {name}")
        return p

    def save(self, filename: str, data: bytes) -> str:
        """Zapis nowego pliku; ta sama nazwa i inna treść = nowa wersja (-2, -3…), nic nie jest nadpisywane."""
        name = safe_filename(filename, "dokument")
        ext = Path(name).suffix.lower()
        if ext not in EXTS:
            raise KnowledgeError(f"nieobsługiwany typ pliku {ext or '(brak rozszerzenia)'} – dozwolone: "
                                 + ", ".join(sorted(EXTS)))
        if len(data) > MAX_FILE:
            raise KnowledgeError(f"plik {filename} większy niż {MAX_FILE // 2**20} MB")
        if not data:
            raise KnowledgeError(f"plik {filename} jest pusty")
        self.root.mkdir(parents=True, exist_ok=True)
        if len(self.entries()) >= MAX_FILES:
            raise KnowledgeError(f"limit {MAX_FILES} plików w bazie wiedzy")
        target, n = self.root / name, 2
        while target.exists():
            if target.read_bytes() == data:
                return target.name          # ten sam plik drugi raz – bez duplikatu
            target, n = self.root / f"{Path(name).stem}-{n}{ext}", n + 1
        target.write_bytes(data)
        return target.name

    def delete(self, name: str) -> None:
        p = self.path(name)
        p.unlink()
        cache = self._cache_file(p)
        if cache.exists():
            cache.unlink()

    # ---- tekst ----
    def _cache_file(self, p: Path) -> Path:
        return self.root / CACHE / (p.name + ".txt")

    def text(self, name: str) -> str:
        p = self.path(name)
        cache = self._cache_file(p)
        if cache.is_file() and cache.stat().st_mtime >= p.stat().st_mtime:
            return cache.read_text(encoding="utf-8", errors="replace")
        txt = extract_text(p)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(txt, encoding="utf-8")
        except OSError:
            pass                            # tylko odczyt (np. w procesie Moniki) – bez pamięci podręcznej
        return txt

    def read(self, name: str, offset: int = 0) -> list[dict]:
        """Treść dla modelu (bloki MCP) – jak czytaj_dokument, z obsługą skanów jako obrazów."""
        try:
            return read_document(self.path(name), max(0, int(offset)))
        except DocumentError as e:
            raise KnowledgeError(str(e)) from None

    def search(self, query: str, limit: int = 8, context: int = 160) -> list[Hit]:
        terms = _terms(query)
        if not terms:
            return []
        hits = []
        for e in self.entries():
            try:
                raw = self.text(e.name)
            except (KnowledgeError, DocumentError, OSError):
                raw = ""
            body, title = fold(raw), fold(e.name)
            found = [t for t in terms if t in body or t in title]
            if not found:
                continue
            score = len(found) * 100 + sum(min(body.count(t), 20) for t in found) + 50 * sum(t in title for t in found)
            snippets, used = [], []
            for t in found:
                i = body.find(t)
                if i < 0 or any(abs(i - u) < context for u in used):
                    continue
                used.append(i)
                a, b = max(0, i - context // 2), min(len(raw), i + context)
                snippets.append(("…" if a else "") + " ".join(raw[a:b].split()) + ("…" if b < len(raw) else ""))
                if len(snippets) >= 3:
                    break
            hits.append(Hit(e.name, score, tuple(snippets)))
        return sorted(hits, key=lambda h: (-h.score, h.name))[:limit]


def extract_text(path: Path) -> str:
    """Sam tekst (bez obrazów) do wyszukiwania. Skan bez warstwy tekstu = pusty tekst (czytany jako obraz)."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return pdf_text(path)[0]
        if ext == ".docx":
            return docx_text(path)
        blocks = read_document(path, 0)
    except (DocumentError, OSError):
        return ""
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
