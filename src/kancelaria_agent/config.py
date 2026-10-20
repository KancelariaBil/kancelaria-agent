"""Ładowanie config.yaml. Konfiguracja jest niezmienna po wczytaniu."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Guardrails:
    allowed_recipients: tuple[str, ...]
    allowed_clients: tuple[str, ...]
    forbid_payment_data_changes: bool
    max_amount_without_approval_pln: Decimal | None
    client_replies: bool = False


@dataclass(frozen=True)
class Config:
    dry_run: bool
    integrations: str
    poll_interval_seconds: float
    database: Path
    audit_log: Path
    stop_file: Path
    guardrails: Guardrails
    audit_max_bytes: int
    audit_backup_count: int
    source_path: Path
    source_sha256: str
    planner: str = "mock"
    projects_dir: Path = Path("data/projects")
    templates_dir: Path = Path("data/wzory")
    max_attachments_mb: int = 20
    workers: int = 1
    # Zespół kancelarii: zaufani nadawcy (zlecenia), pierwszy = osoba odpowiedzialna (raporty, zgłoszenia).
    team: tuple[str, ...] = ()
    web_research: bool = True


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else root / p


def load_config(path: Path | None = None) -> Config:
    path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"Nie można wczytać {path}: {e}") from e

    root = path.parent
    paths = raw.get("paths") or {}
    g = raw.get("guardrails") or {}
    audit = raw.get("audit") or {}

    integrations = str(raw.get("integrations", "mock"))
    if integrations not in ("mock", "gmail"):
        raise ConfigError("Dozwolone integrations: mock albo gmail")
    planner = str(raw.get("planner", "mock"))
    if planner not in ("mock", "agent"):
        raise ConfigError("Dozwolone planner: mock albo agent")
    team = tuple(str(x).strip().lower() for x in raw.get("team") or ())
    if any("@" not in t or t.startswith("@") for t in team):
        raise ConfigError("team: pełne adresy e-mail (bez samych domen)")

    # Opcjonalny próg kwotowy (akceptacja w terminalu powyżej kwoty). Brak klucza = brak progu.
    try:
        raw_limit = g.get("max_amount_without_approval_pln")
        limit = None if raw_limit is None else Decimal(str(raw_limit))
    except Exception as e:
        raise ConfigError("Niepoprawny max_amount_without_approval_pln") from e

    return Config(
        # Fail-safe: tylko dosłowne `false` wyłącza tryb próbny.
        dry_run=raw.get("dry_run") is not False,
        integrations=integrations,
        planner=planner,
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 30)),
        database=_resolve(root, paths.get("database", "data/agent.db")),
        audit_log=_resolve(root, paths.get("audit_log", "logs/audit.jsonl")),
        stop_file=_resolve(root, paths.get("stop_file", "STOP")),
        projects_dir=_resolve(root, paths.get("projects", "data/projects")),
        templates_dir=_resolve(root, paths.get("templates", "data/wzory")),
        max_attachments_mb=int(raw.get("max_attachments_mb", 20)),
        workers=max(1, min(5, int(raw.get("workers", 1)))),
        team=team,
        # Fail-safe w drugą stronę niż dry_run: wyłącza tylko dosłowne `false`.
        web_research=raw.get("web_research") is not False,
        guardrails=Guardrails(
            allowed_recipients=tuple(str(x).lower() for x in g.get("allowed_recipients") or ()),
            allowed_clients=tuple(str(x).lower() for x in g.get("allowed_clients") or ()),
            # Fail-safe: zakaz zmian płatności wyłącza tylko dosłowne `false`.
            forbid_payment_data_changes=g.get("forbid_payment_data_changes") is not False,
            max_amount_without_approval_pln=limit,
            # Fail-safe: odpowiedzi bezpośrednio do klientów włącza tylko dosłowne `true`.
            client_replies=g.get("client_replies") is True,
        ),
        audit_max_bytes=int(audit.get("max_bytes", 5_000_000)),
        audit_backup_count=int(audit.get("backup_count", 10)),
        source_path=path,
        source_sha256=file_sha256(path),
    )
