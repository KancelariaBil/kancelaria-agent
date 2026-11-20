"""Baza wiedzy Moniki: pliki kancelarii (procedury, stanowiska, notatki, orzecznictwo), które Monika
przeszukuje i czyta przy każdym zleceniu.

- katalog domyślnie data/wiedza z podkatalogami kategorii: interpretacje/, wyroki/, przepisy/, komentarze/,
  artykuly/ (artykuły i książki);
  pliki bez rozpoznanej kategorii leżą w katalogu głównym („inne”). Nowy plik trafia do kategorii wskazanej
  przez człowieka (panel, temat maila „wiedza wyroki”) albo rozpoznanej z nazwy i treści (classify),
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
INCOMING = ".przyjecie"

# Kategorie bazy wiedzy (podkatalogi). „inne” = katalog główny.
CATEGORIES = {
    "interpretacje": "Interpretacje podatkowe",
    "wyroki": "Wyroki i orzecznictwo",
    "przepisy": "Przepisy ustaw",
    "komentarze": "Komentarze",
    "artykuly": "Artykuły i książki",
}
OTHER = "inne"
ALL_CATEGORIES = {**CATEGORIES, OTHER: "Inne"}
# Słowa w temacie maila „wiedza …” i w formularzu → kategoria.
CATEGORY_WORDS = {
    "interpretacje": "interpretacje", "interpretacja": "interpretacje", "interpretacji": "interpretacje",
    "wyroki": "wyroki", "wyrok": "wyroki", "orzeczenia": "wyroki", "orzecznictwo": "wyroki", "orzeczenie": "wyroki",
    "przepisy": "przepisy", "przepis": "przepisy", "ustawy": "przepisy", "ustawa": "przepisy",
    "komentarze": "komentarze", "komentarz": "komentarze", "glosa": "komentarze", "glosy": "komentarze",
    "artykuly": "artykuly", "artykul": "artykuly", "ksiazki": "artykuly", "ksiazka": "artykuly",
    "publikacje": "artykuly", "publikacja": "artykuly", "monografia": "artykuly", "monografie": "artykuly",
    "inne": OTHER,
}


class KnowledgeError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    name: str        # „kategoria/plik” albo sam „plik” dla kategorii „inne”
    size: int
    mtime: float
    category: str = OTHER

    @property
    def filename(self) -> str:
        return self.name.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Hit:
    name: str
    score: int
    snippets: tuple[str, ...]


def fold(text: str) -> str:
    """Małe litery, bez polskich znaków – do porównań."""
    t = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def category_from_word(word: str | None) -> str | None:
    """„wyroki”, „Komentarz”, „auto” → klucz kategorii; None = rozpoznaj automatycznie."""
    w = fold(str(word or "")).strip()
    if not w or w in ("auto", "automatycznie"):
        return None
    if w in CATEGORIES or w == OTHER:
        return w
    return CATEGORY_WORDS.get(w)


_SYGN_RE = re.compile(r"\b[ivx]{1,4} [a-z]{1,6} \d{1,6}/\d{2}\b")
_KIS_RE = re.compile(r"\b0\d{3}-kd[a-z]{1,4}\d?\b")
_ART_RE = re.compile(r"(?m)^\s*art\. ?\d+[a-z]*\.")


def classify(filename: str, text: str) -> str:
    """Kategoria z nazwy pliku i treści (proste reguły, bez modelu). Brak pewności → „inne”."""
    t = fold(text[:40000])
    n = fold(filename)
    score = {k: 0 for k in CATEGORIES}
    # interpretacje podatkowe
    if re.search(r"interpretac", n): score["interpretacje"] += 6
    score["interpretacje"] += 5 * bool(re.search(r"interpretacj\w+ indywidualn", t))
    score["interpretacje"] += 4 * bool("krajowej informacji skarbowej" in t)
    score["interpretacje"] += 5 * bool(_KIS_RE.search(t))
    score["interpretacje"] += 3 * bool("stanowisko wnioskodawcy" in t or "opis stanu faktycznego" in t
                                       or "opis zdarzenia przyszlego" in t)
    # wyroki i orzecznictwo
    if re.search(r"wyrok|orzeczen|uchwala sn|postanowienie s", n): score["wyroki"] += 6
    score["wyroki"] += 6 * bool("w imieniu rzeczypospolitej" in t)
    score["wyroki"] += 3 * bool(_SYGN_RE.search(t))
    score["wyroki"] += 2 * bool(re.search(r"sad najwyzszy|naczelny sad administracyjny|wojewodzki sad administracyjny"
                                          r"|sad apelacyjny|sad okregowy|sad rejonowy|trybunal", t))
    score["wyroki"] += min(t.count("wyrok"), 3)
    score["wyroki"] += 2 * bool("uzasadnienie" in t and ("powod" in t or "skarzac" in t or "pozwan" in t))
    # przepisy ustaw
    if re.search(r"ustawa|kodeks|dz\.? ?u|rozporzadzen|ordynacja|tekst jednolity", n): score["przepisy"] += 6
    score["przepisy"] += 4 * bool(re.search(r"tekst jednolity|obwieszczenie marszalka", t))
    score["przepisy"] += 3 * bool(re.search(r"^\W*(ustawa|rozporzadzenie)\b", t.strip()[:300]))
    arts = len(_ART_RE.findall(t))
    score["przepisy"] += 6 if arts >= 8 else 3 if arts >= 4 else 0
    # komentarze
    if re.search(r"komentarz|glosa", n): score["komentarze"] += 6
    score["komentarze"] += 3 * bool("komentarz" in t)
    score["komentarze"] += 4 * bool(re.search(r"\bglosa\b", t))
    score["komentarze"] += 2 * bool(re.search(r"\bnb\.? ?\d+", t))
    score["komentarze"] += 3 * bool(re.search(r"legalis|lex/el|wolters kluwer|c\.? ?h\.? ?beck", t))
    # artykuły i książki (publikacje naukowe, czasopisma, monografie)
    if re.search(r"artykul|ksiazk|monografi|publikacj|czasopism|przeglad podatkowy|monitor podatkowy", n):
        score["artykuly"] += 6
    score["artykuly"] += 5 * bool(re.search(r"\bisbn\b|\bissn\b|\bdoi:? ?10\.", t))
    score["artykuly"] += 3 * bool(re.search(r"slowa kluczowe|keywords|streszczenie|abstract", t))
    score["artykuly"] += 2 * bool(re.search(r"\bbibliografia\b|\bliteratura\b|\bspis tresci\b", t))
    score["artykuly"] += 2 * bool(re.search(r"\bwydawnictwo\b|\bwydanie [ivx0-9]", t))
    best = max(score, key=lambda k: score[k])
    ordered = sorted(score.values(), reverse=True)
    if score[best] < 5 or ordered[0] - ordered[1] < 2:
        return OTHER
    return best


def _terms(query: str) -> list[str]:
    return [w for w in re.findall(r"\w+", fold(query)) if len(w) >= 3][:12]


class KnowledgeBase:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---- pliki ----
    def _dir(self, category: str) -> Path:
        return self.root if category == OTHER else self.root / category

    def entries(self, category: str | None = None) -> list[Entry]:
        out = []
        cats = [category] if category else [OTHER, *CATEGORIES]
        for cat in cats:
            d = self._dir(cat)
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file() and not f.is_symlink() and not f.name.startswith(".") and f.suffix.lower() in EXTS:
                    st = f.stat()
                    out.append(Entry(f.name if cat == OTHER else f"{cat}/{f.name}", st.st_size, st.st_mtime, cat))
        return sorted(out, key=lambda e: e.name)

    def path(self, name: str) -> Path:
        name = str(name)
        cat, _, base = name.rpartition("/")
        if "\\" in name or (cat and cat not in CATEGORIES) or not base or base.startswith("."):
            raise KnowledgeError(f"niepoprawna nazwa pliku: {name!r}")
        p = self._dir(cat or OTHER) / base
        if not p.is_file() or p.is_symlink() or p.suffix.lower() not in EXTS:
            raise KnowledgeError(f"brak pliku w bazie wiedzy: {name}")
        return p

    def _place(self, src: Path, name: str, category: str) -> str:
        """Przeniesienie do katalogu kategorii; ta sama treść = bez duplikatu, inna = nowa wersja (-2, -3…)."""
        d = self._dir(category)
        d.mkdir(parents=True, exist_ok=True)
        ext = Path(name).suffix.lower()
        target, n = d / name, 2
        data = src.read_bytes()
        while target.exists():
            if target.read_bytes() == data:
                src.unlink()
                return target.name if category == OTHER else f"{category}/{target.name}"
            target, n = d / f"{Path(name).stem}-{n}{ext}", n + 1
        src.replace(target)
        return target.name if category == OTHER else f"{category}/{target.name}"

    def find_duplicate(self, data: bytes, text: str = "") -> str | None:
        """Ten sam dokument już w bazie (dowolna nazwa i kategoria): identyczne bajty albo identyczny tekst
        (np. ten sam PDF pobrany drugi raz). Zwraca nazwę istniejącego pliku albo None."""
        norm = " ".join(fold(text).split())
        for e in self.entries():
            p = self.path(e.name)
            if e.size == len(data) and p.read_bytes() == data:
                return e.name
            if len(norm) >= 500:
                cache = self._cache_file(p)
                if cache.is_file() and " ".join(fold(cache.read_text(encoding="utf-8", errors="replace")).split()) == norm:
                    return e.name
        return None

    def save(self, filename: str, data: bytes, category: str | None = None) -> str:
        """Zapis nowego pliku (None = kategoria rozpoznana z nazwy i treści). Zwraca nazwę w bazie; gdy ten sam
        dokument już jest – nazwę istniejącego pliku (bez duplikatu)."""
        return self.save_report(filename, data, category)[0]

    def save_report(self, filename: str, data: bytes, category: str | None = None) -> tuple[str, bool]:
        """Jak save, ale zwraca też, czy dokument już był w bazie (True = nic nie dodano)."""
        name = safe_filename(filename, "dokument")
        ext = Path(name).suffix.lower()
        if ext not in EXTS:
            raise KnowledgeError(f"nieobsługiwany typ pliku {ext or '(brak rozszerzenia)'} – dozwolone: "
                                 + ", ".join(sorted(EXTS)))
        if len(data) > MAX_FILE:
            raise KnowledgeError(f"plik {filename} większy niż {MAX_FILE // 2**20} MB")
        if not data:
            raise KnowledgeError(f"plik {filename} jest pusty")
        if category is not None and category not in ALL_CATEGORIES:
            raise KnowledgeError(f"nieznana kategoria: {category}")
        self.root.mkdir(parents=True, exist_ok=True)
        if len(self.entries()) >= MAX_FILES:
            raise KnowledgeError(f"limit {MAX_FILES} plików w bazie wiedzy")
        tmp_dir = self.root / INCOMING
        tmp_dir.mkdir(exist_ok=True)
        tmp = tmp_dir / name
        tmp.write_bytes(data)
        try:
            txt = extract_text(tmp)
            dup = self.find_duplicate(data, txt)
            if dup:
                return dup, True
            saved = self._place(tmp, name, category or classify(name, txt))
            try:   # tekst od razu do pamięci podręcznej – pierwsze wyszukiwanie nie musi czytać pliku
                c = self._cache_file(self.path(saved))
                c.parent.mkdir(parents=True, exist_ok=True)
                c.write_text(txt, encoding="utf-8")
            except OSError:
                pass
            return saved, False
        finally:
            if tmp.exists():
                tmp.unlink()

    def move(self, name: str, category: str) -> str:
        """Zmiana kategorii pliku (panel). Zwraca nową nazwę."""
        if category not in ALL_CATEGORIES:
            raise KnowledgeError(f"nieznana kategoria: {category}")
        p = self.path(name)
        cache = self._cache_file(p)
        if cache.exists():
            cache.unlink()
        return self._place(p, p.name, category)

    def sort_other(self) -> list[tuple[str, str]]:
        """Pliki z „inne” → rozpoznana kategoria. Zwraca listę (stara nazwa, nowa nazwa) przeniesionych."""
        moved = []
        for e in self.entries(OTHER):
            cat = classify(e.name, self.text(e.name))
            if cat != OTHER:
                moved.append((e.name, self.move(e.name, cat)))
        return moved

    def delete(self, name: str) -> None:
        p = self.path(name)
        p.unlink()
        cache = self._cache_file(p)
        if cache.exists():
            cache.unlink()

    # ---- tekst ----
    def _cache_file(self, p: Path) -> Path:
        return p.parent / CACHE / (p.name + ".txt")

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

    def search(self, query: str, limit: int = 8, context: int = 160, category: str | None = None) -> list[Hit]:
        terms = _terms(query)
        if not terms:
            return []
        hits = []
        for e in self.entries(category):
            try:
                raw = self.text(e.name)
            except Exception:
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
    except Exception:   # jeden uszkodzony / nietypowy plik nie może wywrócić wyszukiwania w całej bazie
        return ""
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
