"""Operacje plikowe Moniki w katalogu jednej sprawy i w bazie wzorów (czysta logika; narzędzia SDK je wołają).

Granice:
- każda ścieżka jest względna i rozwiązywana wewnątrz katalogu sprawy (bez '..', ukrytych plików,
  dowiązań); zapis wyłącznie do pisma/ (nowe pliki .docx i .txt/.md),
- dokumenty/ (załączniki z maili zespołu) i baza wzorów – wyłącznie do odczytu,
- brak sieci i brak wykonywania czegokolwiek z plików.
"""

from __future__ import annotations

from pathlib import Path

from .documents import DocumentError, build_docx, read_document
from .projects import PathError, resolve_inside, safe_relpath, slugify

READABLE = ("dokumenty", "pisma")
TEMPLATE_EXTS = {".docx", ".doc", ".odt", ".rtf", ".pdf", ".txt"}
MAX_NOTE = 500 * 1024
MAX_DOCX_SOURCE = 400 * 1024
MAX_PISMA = 200


class ToolError(Exception):
    pass


def _in(base: Path, rel: str, allowed_top: tuple[str, ...] | None) -> Path:
    try:
        path = safe_relpath(rel)
        if allowed_top is not None and path.parts[0] not in allowed_top:
            raise ToolError(f"dozwolone katalogi: {', '.join(allowed_top)}")
        return resolve_inside(base, str(path))
    except PathError as e:
        raise ToolError(str(e)) from None


def read_file(project_dir: Path, rel: str, offset: int = 0, page: int | None = None) -> list[dict]:
    path = _in(project_dir, rel, READABLE)
    if not path.is_file():
        raise ToolError(f"brak pliku {rel}")
    try:
        return read_document(path, max(0, int(offset)), page)
    except DocumentError as e:
        raise ToolError(str(e)) from None


def _new_target(project_dir: Path, name: str, ext: str) -> Path:
    base = slugify(name, 70) or "pismo"
    out = project_dir / "pisma"
    out.mkdir(parents=True, exist_ok=True)
    if sum(1 for _ in out.iterdir()) >= MAX_PISMA:
        raise ToolError(f"limit {MAX_PISMA} plików w pisma/")
    target, n = out / f"{base}{ext}", 2
    while target.exists():          # nowa wersja nie nadpisuje poprzedniej
        target, n = out / f"{base}-v{n}{ext}", n + 1
    return target


def create_docx(project_dir: Path, name: str, content: str, templates_dir: Path | None = None,
                template: str | None = None) -> str:
    if len(str(content).encode()) > MAX_DOCX_SOURCE:
        raise ToolError("treść pisma większa niż 400 KB")
    tpl = None
    if template:
        if templates_dir is None:
            raise ToolError("baza wzorów niedostępna")
        tpl = _in(templates_dir, template, None)
        if not tpl.is_file() or tpl.suffix.lower() != ".docx":
            raise ToolError("wzór musi być plikiem .docx z bazy wzorów (wzory_lista)")
    target = _new_target(project_dir, name, ".docx")
    try:
        build_docx(content, target, tpl)
    except Exception as e:   # noqa: BLE001 – uszkodzony wzór nie może przerwać zadania
        target.unlink(missing_ok=True)
        raise ToolError(f"nie udało się utworzyć pliku: {type(e).__name__}: {e}"[:300]) from None
    return str(target.relative_to(project_dir))


def write_note(project_dir: Path, name: str, content: str) -> str:
    data = str(content).encode("utf-8")
    if len(data) > MAX_NOTE:
        raise ToolError("notatka większa niż 500 KB")
    target = _new_target(project_dir, name, ".md")
    target.write_bytes(data)
    return str(target.relative_to(project_dir))


def list_templates(templates_dir: Path, query: str = "", limit: int = 200) -> list[tuple[str, int]]:
    if not templates_dir.is_dir():
        return []
    q = slugify(query)
    out = []
    for f in sorted(templates_dir.rglob("*")):
        rel = f.relative_to(templates_dir)
        if not f.is_file() or f.is_symlink() or any(p.startswith(".") for p in rel.parts) \
                or f.suffix.lower() not in TEMPLATE_EXTS:
            continue
        if q and not all(w in slugify(str(rel)) for w in q.split("-")):
            continue
        out.append((str(rel), f.stat().st_size))
        if len(out) >= limit:
            break
    return out


def read_template(templates_dir: Path, rel: str, offset: int = 0) -> list[dict]:
    path = _in(templates_dir, rel, None)
    if not path.is_file() or path.suffix.lower() not in TEMPLATE_EXTS:
        raise ToolError(f"brak wzoru {rel}")
    try:
        return read_document(path, max(0, int(offset)))
    except DocumentError as e:
        raise ToolError(str(e)) from None
