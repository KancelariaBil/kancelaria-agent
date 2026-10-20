"""Raport stanu serwera na mail o temacie „diagnostyka” (tylko od zespołu, bez modelu)."""

from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path


def _tools() -> str:
    out = []
    for name in ("soffice", "pdftoppm", "pdftotext"):
        out.append(f"{name}: {'OK' if shutil.which(name) else 'BRAK'}")
    for mod in ("docx", "openpyxl"):
        try:
            __import__(mod)
            out.append(f"{mod}: OK")
        except Exception as e:  # noqa: BLE001
            out.append(f"{mod}: BŁĄD {type(e).__name__}")
    return ", ".join(out)


def _templates(cfg) -> str:
    d = cfg.templates_dir
    n = sum(1 for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0
    return f"{n} plików w {d}"


def _recent_mails(audit_log: Path, n: int = 10) -> list[str]:
    try:
        lines = audit_log.read_text(encoding="utf-8").splitlines()[-5000:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "mail_received" and e.get("kind") != "import":
            who = e.get("sender") or "(brak nadawcy)"
            mark = "" if e.get("trusted") else " [obcy]"
            out.append(f"{e.get('ts', '')[:16]} {who}{mark}: {str(e.get('subject') or '(brak tematu)')[:80]}")
        elif e.get("event") == "mail_unreadable":
            out.append(f"{e.get('ts', '')[:16]} nieczytelna wiadomość pominięta ({e.get('info', '')})")
        if len(out) >= n:
            break
    return out


def _ai_costs(audit_log: Path, hours: int = 24) -> list[str]:
    """Koszt AI z ostatnich godzin: suma, liczba uruchomień, najdroższe (z dziennika ai_cost)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    runs = []
    for f in (audit_log.with_name(audit_log.name + ".1"), audit_log):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"ai_cost"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "ai_cost" and e.get("ts", "") >= cutoff:
                runs.append(e)
    if not runs:
        return ["brak uruchomień modelu w tym czasie"]
    total = sum(e.get("cost_usd") or 0 for e in runs)
    out = [f"razem {total:.2f} USD w {len(runs)} uruchomieniach (średnio {total / len(runs):.2f} USD)"]
    for e in sorted(runs, key=lambda e: -(e.get("cost_usd") or 0))[:5]:
        out.append(f"{e.get('ts', '')[:16]} projekt {e.get('project_id')}: {e.get('cost_usd', 0):.2f} USD, "
                   f"{e.get('turns')} kroków, {e.get('seconds')} s, model {e.get('model')}, "
                   f"tokeny: wejście {e.get('input')}, z pamięci podręcznej {e.get('cache_read')}, "
                   f"zapis do pamięci {e.get('cache_write')}, wyjście {e.get('output')}")
    return out


def _project_costs(store) -> list[str]:
    rows = store.db.execute("SELECT id, client, name, cost_usd FROM projects WHERE cost_usd>0 "
                            "ORDER BY cost_usd DESC LIMIT 8").fetchall()
    return [f"#{r['id']} {r['client'] or ''} {r['name'] or ''}: {r['cost_usd']:.2f} USD" for r in rows]


def _recent_errors(audit_log: Path, n: int = 8) -> list[str]:
    try:
        lines = audit_log.read_text(encoding="utf-8").splitlines()[-3000:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") in ("task_failed", "agent_error", "action_failed"):
            out.append(f"{e.get('ts', '')[:19]} {e['event']}: {str(e.get('error', ''))[:300]}")
            if len(out) >= n:
                break
    return out


def report(cfg, store) -> str:
    disk = shutil.disk_usage(cfg.database.parent)
    counts = {s: len(store.tasks_by_status(s)) for s in ("queued", "running", "failed")}
    lines = [
        f"Python {sys.version.split()[0]} na {platform.platform()}",
        f"Dysk: wolne {disk.free // 2**30} GB z {disk.total // 2**30} GB",
        f"Zadania: w kolejce {counts['queued']}, w trakcie {counts['running']}, nieudane łącznie {counts['failed']}",
        f"Narzędzia dokumentów: {_tools()}",
        f"Baza wzorów: {_templates(cfg)}",
        f"Zespół: {', '.join(cfg.team) or 'BRAK'}",
        "Koszt AI, ostatnie 24 h:",
        *_ai_costs(cfg.audit_log),
        "Koszt AI wg spraw:",
        *(_project_costs(store) or ["brak"]),
        "Ostatnie odebrane maile (czas UTC):",
        *(_recent_mails(cfg.audit_log) or ["brak"]),
        "Ostatnie błędy:",
        *(_recent_errors(cfg.audit_log) or ["brak"]),
    ]
    return "\n".join(lines)
