"""CLI: python -m kancelaria_agent {run|once|status|proposals|approve|reject}"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import Agent
from .audit import AuditLog
from .config import PROJECT_ROOT, load_config
from .executor import Executor
from .integrations import IncomingMail, build_gmail, build_mock
from .planner import MockPlanner
from .store import Store

DEMO_MAIL = IncomingMail(
    message_id="demo-1", sender="biuro@example.com",
    subject="Pytanie o umowę", body="Dzień dobry, prosimy o projekt umowy.",
)


def _build(args, with_demo: bool = False):
    cfg = load_config(Path(args.config) if args.config else None)
    store = Store(cfg.database)
    audit = AuditLog(cfg.audit_log, cfg.audit_max_bytes, cfg.audit_backup_count)
    if cfg.integrations == "gmail":
        integ = build_gmail(cfg.dry_run, PROJECT_ROOT / ".env", cfg.team)
    else:
        integ = build_mock([DEMO_MAIL] if with_demo else [])
    return cfg, store, audit, integ


def _mailcheck(args) -> int:
    from .integrations.gmail import GmailMail, GmailSettings, MailConfigError
    try:
        cfg = load_config(Path(args.config) if args.config else None)
        settings = GmailSettings.from_env(PROJECT_ROOT / ".env", cfg.team)
        for line in GmailMail(settings, dry_run=True).check_connection():
            print(line)
    except MailConfigError as e:
        print(f"Błąd konfiguracji: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # komunikat bez haseł: imaplib/smtplib nie zawierają ich w wyjątkach
        print(f"Połączenie nieudane: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


def _oauth_setup() -> int:
    """Jednorazowa autoryzacja skrzynki agenta (uruchamia człowiek; wymaga terminala)."""
    from .integrations.gmail import read_env_file
    from .integrations.oauth import SETUP_PORT, OAuthError, run_setup
    if not sys.stdin.isatty():
        print("Odmowa: autoryzację uruchamia człowiek w terminalu.", file=sys.stderr)
        return 2
    env_file = PROJECT_ROOT / ".env"
    env = {**read_env_file(env_file), **os.environ}
    missing = [k for k in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "AGENT_EMAIL") if not env.get(k, "").strip()]
    if missing:
        print(f"Brak w .env: {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"Na Macu otwórz w przeglądarce:  http://localhost:{SETUP_PORT}\n"
          f"i zaloguj się jako {env['AGENT_EMAIL']}. Czekam do 10 minut...", flush=True)
    try:
        email = run_setup(env["GMAIL_CLIENT_ID"].strip(), env["GMAIL_CLIENT_SECRET"].strip(),
                          env["AGENT_EMAIL"].strip(), env_file)
    except OAuthError as e:
        print(f"Autoryzacja nieudana: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Nie można uruchomić serwera na porcie {SETUP_PORT}: {e}", file=sys.stderr)
        return 1
    print(f"Gotowe: zapisano token dla {email}. Teraz uruchom: .venv/bin/python -m kancelaria_agent mailcheck")
    return 0


def _templates_import(args) -> int:
    """Import wzorów z ZIP-a do data/wzory (bezpieczne nazwy, tylko dokumenty; uruchamia człowiek)."""
    import zipfile

    from .project_tools import TEMPLATE_EXTS
    from .projects import safe_filename, slugify
    cfg = load_config(Path(args.config) if args.config else None)
    dest = cfg.templates_dir
    dest.mkdir(parents=True, exist_ok=True)
    n = skipped = 0
    with zipfile.ZipFile(args.zip) as zf:
        for info in zf.infolist():
            raw = info.filename.replace("\\", "/")
            parts = [p for p in raw.split("/") if p not in ("", ".")]
            if info.is_dir() or not parts or any(p == ".." or p.startswith((".", "__MACOSX", "~$")) for p in parts):
                continue
            name = safe_filename(parts[-1])
            if "." + name.rpartition(".")[2] not in TEMPLATE_EXTS or info.file_size > 30 * 1024 * 1024:
                skipped += 1
                continue
            sub = [slugify(p, 60) or "x" for p in parts[:-1]][:5]
            target = dest.joinpath(*sub, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src:
                target.write_bytes(src.read())
            n += 1
    print(f"zaimportowano {n} wzorów do {dest} (pominięte: {skipped})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kancelaria_agent")
    ap.add_argument("--config", help="ścieżka do config.yaml (domyślnie: katalog projektu)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="pętla agenta (usługa systemd)")
    once = sub.add_parser("once", help="jeden przebieg i wyjście")
    once.add_argument("--demo-mail", action="store_true", help="dodaj przykładowy mail do skrzynki testowej")
    sub.add_parser("mailcheck", help="test połączenia IMAP/SMTP (logowanie; nic nie wysyła)")
    sub.add_parser("oauth-setup", help="jednorazowa autoryzacja skrzynki agenta przez Google (człowiek)")
    sub.add_parser("status", help="tryb, wyłącznik, liczba propozycji")
    sub.add_parser("sprawy", help="lista spraw (katalog, etap, zlecający)")
    sub.add_parser("wzory-import", help="import wzorów dokumentów z pliku ZIP").add_argument("zip")
    p = sub.add_parser("proposals", help="lista propozycji")
    p.add_argument("--all", action="store_true")
    for name in ("approve", "reject"):
        sub.add_parser(name, help=f"{name} propozycji (tylko człowiek, w terminalu)").add_argument("id", type=int)
    args = ap.parse_args(argv)

    if args.cmd == "mailcheck":
        return _mailcheck(args)
    if args.cmd == "oauth-setup":
        return _oauth_setup()
    if args.cmd == "wzory-import":
        return _templates_import(args)

    cfg, store, audit, integ = _build(args, with_demo=getattr(args, "demo_mail", False))
    try:
        if args.cmd in ("run", "once"):
            owner = getattr(getattr(integ.mail, "s", None), "owner", None)
            from .integrations.gmail import read_env_file
            env = {**read_env_file(PROJECT_ROOT / ".env"), **os.environ}
            from .projects import Projects
            projects = Projects(store, cfg.projects_dir)
            if cfg.planner == "agent":
                from .planner_agent import AgentPlanner
                planner = AgentPlanner(owner, api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
                                       model=env.get("ANTHROPIC_MODEL", "").strip() or None, audit=audit,
                                       projects=projects, templates_dir=cfg.templates_dir, web=cfg.web_research)
            else:
                planner = MockPlanner(owner_email=owner, client_replies=cfg.guardrails.client_replies)
            audit.record("planner", name=cfg.planner, detail=repr(planner))
            agent = Agent(cfg, store, audit, integ, planner, projects,
                          workers=cfg.workers if args.cmd == "run" else 1)
            if args.cmd == "run":
                n = store.requeue_running()
                if n:
                    audit.record("requeue", count=n, reason="zadania przerwane restartem")
                return agent.run()
            print(f"tick: {agent.tick()} (dry_run={cfg.dry_run})")
            return 0

        if args.cmd == "status":
            from . import guards
            print(f"dry_run: {cfg.dry_run}\nintegracje: {cfg.integrations}\n"
                  f"STOP aktywny: {guards.kill_switch_active(cfg)}\n"
                  f"propozycje oczekujące: {len(store.list_proposals('pending'))}")
            return 0

        if args.cmd == "sprawy":
            for r in store.db.execute("SELECT * FROM projects ORDER BY id DESC LIMIT 50"):
                print(f"#{r['id']} {r['slug']} [{r['status']}/{r['stage']}] {r['client']} – {r['name']} "
                      f"(zlecił(a): {r['sender']})")
            return 0

        if args.cmd == "proposals":
            for r in store.list_proposals(None if args.all else "pending"):
                print(f"#{r['id']} [{r['status']}] {r['action_kind']} ({r['reason']})\n    {r['payload']}")
            return 0

        # approve / reject: wymagają człowieka przy terminalu (agent uruchamiany bez TTY nie zatwierdzi).
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("Odmowa: zatwierdzanie wymaga interaktywnego terminala (człowiek).", file=sys.stderr)
            return 2
        ex = Executor(cfg, store, audit, integ)
        if args.cmd == "reject":
            print("odrzucono" if ex.reject(args.id) else "nie można odrzucić")
            return 0
        row = store.get_proposal(args.id)
        if row is None:
            print("brak takiej propozycji", file=sys.stderr)
            return 1
        print(f"{row['action_kind']}: {row['payload']}")
        if input("Zatwierdzić? [t/N] ").strip().lower() != "t":
            print("anulowano")
            return 0
        out = ex.approve(args.id)
        print(f"{out.status}: {out.reason} {out.result or ''}")
        return 0 if out.status == "executed" else 1
    finally:
        store.close()
        audit.close()


if __name__ == "__main__":
    sys.exit(main())
