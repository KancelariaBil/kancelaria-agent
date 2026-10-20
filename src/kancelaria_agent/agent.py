"""Pętla agenta: poczta → kolejka → planer → Executor. Zero bezpośrednich skutków zewnętrznych."""

from __future__ import annotations

import json
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from . import guards
from .audit import AuditLog
from .config import Config
from .executor import Executor
from .integrations import Integrations
from .planner import PROJECT_UPDATE, Planner
from .projects import Projects, now_utc, quote_block, reply_subject, safe_filename, strip_quoted
from .store import Store

EXIT_CONFIG_TAMPERED = 3


@contextmanager
def planner_projects(planner, projects):
    """Planer w wątku roboczym dostaje własne połączenie z bazą (SQLite jest jednowątkowe)."""
    if hasattr(planner, "use_projects"):
        with planner.use_projects(projects):
            yield
    else:
        yield  # w unit systemd: RestartPreventExitStatus=3


class Agent:
    def __init__(self, cfg: Config, store: Store, audit: AuditLog, integrations: Integrations, planner: Planner,
                 projects: Projects | None = None, workers: int = 1, owner: str | None = None):
        self.cfg = cfg
        self.projects = projects or Projects(store, cfg.projects_dir)
        self.store = store
        self.audit = audit
        self.integrations = integrations
        self.planner = planner
        self.executor = Executor(cfg, store, audit, integrations)
        self._stop = threading.Event()
        self._halted = False
        self.workers = max(1, int(workers))
        # Zespół kancelarii (config.yaml). Mail poza zespół nie wychodzi nigdy (także przez bezpieczniki).
        self.team = {t.lower() for t in (cfg.team or ((owner,) if owner else ()))}
        self._running: dict = {}
        self._deferred: set[int] = set()
        self._started: dict[int, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="monika") \
            if self.workers > 1 else None

    def ingest_mail(self) -> int:
        n = 0
        mails = self.integrations.mail.fetch_unread()
        for info in getattr(self.integrations.mail, "unreadable", None) or []:
            self.audit.record("mail_unreadable", info=info)
        for mail in mails:
            payload = {"message_id": mail.message_id, "sender": mail.sender,
                       "subject": mail.subject, "body": mail.body, "trusted": mail.trusted,
                       "reply_to": mail.reply_to, "attachments": list(mail.attachments),
                       "in_reply_to": mail.in_reply_to, "references": list(mail.references), "data": mail.data,
                       "date": mail.date, "headers": mail.headers}
            if mail.files:   # dokumenty od zespołu – na dysk od razu (kolejka trzyma tylko ścieżkę)
                payload["staged"] = self._stage_files(mail.message_id, mail.files)
            if mail.data:   # import pamięci – osobny klucz, więc działa też dla maila przetworzonego wcześniej
                if self.store.enqueue("import", {"sender": mail.sender, "data": mail.data, "trusted": mail.trusted,
                                                 "message_id": mail.message_id, "subject": mail.subject},
                                      source_ref=f"import:{mail.message_id}"):
                    self.audit.record("mail_received", message_id=mail.message_id, kind="import")
                    n += 1
                continue
            if self.store.enqueue("email", payload, source_ref=f"mail:{mail.message_id}"):
                self.audit.record("mail_received", message_id=mail.message_id, sender=mail.sender,
                                  subject=mail.subject, body_chars=len(mail.body), trusted=mail.trusted)
                n += 1
        return n

    def _stage_files(self, message_id: str, files) -> str:
        import hashlib
        d = self.cfg.projects_dir.parent / "przychodzace" / hashlib.sha256(message_id.encode()).hexdigest()[:20]
        d.mkdir(parents=True, exist_ok=True)
        for name, data in files:
            target = d / safe_filename(name)
            n = 2
            while target.exists() and target.read_bytes() != data:
                target = target.with_name(f"{target.stem}-{n}{target.suffix}")
                n += 1
            target.write_bytes(data)
        return str(d)

    def _take_staged(self, project, payload: dict) -> list[str]:
        """Dokumenty z maila → <sprawa>/dokumenty/<data>/ (raz; potem katalog przejściowy znika)."""
        import shutil
        staged = payload.get("staged")
        if not staged:
            return []
        src = __import__("pathlib").Path(staged)
        if not src.is_dir() or src.parent != self.cfg.projects_dir.parent / "przychodzace":
            return []
        dest = self.projects.dir(project) / "dokumenty" / now_utc().strftime("%Y-%m-%d")
        dest.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in sorted(src.iterdir()):
            if f.is_file() and not f.is_symlink():
                target, n = dest / f.name, 2
                while target.exists():
                    target, n = dest / f"{f.stem}-{n}{f.suffix}", n + 1
                shutil.move(str(f), target)
                saved.append(str(target.relative_to(self.projects.dir(project))))
        shutil.rmtree(src, ignore_errors=True)
        return saved

    # ---- sprawy ----
    def _project_for(self, kind: str, payload: dict):
        """Zlecenie właściciela → istniejący projekt (tag/wątek) albo nowy. Obcy nadawca → brak projektu."""
        if kind != "email" or payload.get("trusted") is not True:
            return None
        refs = [r for r in [payload.get("in_reply_to"), *(payload.get("references") or [])] if r]
        project = self.projects.find_for_mail(payload.get("subject", ""), refs, payload.get("sender"))
        if project is None:
            project = self.projects.create(payload["sender"], payload.get("subject", ""), payload.get("reply_to"))
            self.audit.record("project_created", project_id=project["id"], slug=project["slug"])
        elif payload.get("reply_to") and payload["reply_to"] != project["reply_to"]:
            self.store.update_project(project["id"], reply_to=payload["reply_to"])
        self.store.link_message(payload["message_id"], project["id"])
        self.store.update_project(project["id"], last_message_id=payload["message_id"])
        saved = self._take_staged(project, payload)
        if saved:
            payload["saved_files"] = saved
        text = strip_quoted(payload.get("body", ""))
        if saved:
            text += "\n[załączone dokumenty: " + ", ".join(saved) + "]"
        self.store.add_project_log(project["id"], "zespol", f"{payload.get('sender')}:\n{text}")
        # Pełna treść (z przekazanymi mailami) trafia do trwałego archiwum pamięci.
        self.projects.memory.archive(f"Temat: {payload.get('subject', '')}\n{payload.get('body', '')}",
                                     f"mail od {payload.get('sender')}", project["id"])
        return self.store.get_project(project["id"])

    def _apply_project_update(self, project, p: dict, task_id=None):
        pid = project["id"]
        if p.get("klient") or p.get("projekt"):
            self.projects.name(pid, str(p.get("klient") or ""), str(p.get("projekt") or ""))
        fields = {"last_contact_at": now_utc().isoformat()}
        if p.get("ball"):
            fields["ball"] = p["ball"]
        if p.get("koszt_usd"):
            fields["cost_usd"] = float(project["cost_usd"] or 0) + float(p["koszt_usd"])
        self.store.update_project(pid, **fields)
        entries = p.get("czas") or []
        if isinstance(entries, dict):
            entries = [entries]
        if entries:
            started = self._started.pop(task_id, None) if task_id is not None else None
            machine = round(time.monotonic() - started, 1) if started else None
            for i, e in enumerate(entries):   # rzeczywisty czas przypisany do pierwszej pozycji zadania
                self.store.time_add(pid, task_id, e["godziny"], e["czynnosc"], machine if i == 0 else None)
            self.audit.record("time_logged", project_id=pid, hours=sum(e["godziny"] for e in entries))
        status = p.get("status")
        if status:
            self.store.update_project(pid, stage=str(status)[:40])
        if status in ("gotowe", "odmowa"):
            self.store.update_project(pid, status="done")
        elif status and project["status"] != "paused":
            self.store.update_project(pid, status="active")
        self.audit.record("project_updated", project_id=pid, status=status)
        return self.store.get_project(pid)

    def _threaded(self, project, payload: dict, source: dict) -> dict:
        p = dict(payload)
        # Temat bez zmian ("Re: <temat zlecenia>"): Gmail łączy w wątek tylko przy tym samym temacie.
        # Projekt rozpoznajemy po In-Reply-To/References (tag [#id] działa nadal, gdy ktoś go wpisze).
        p["subject"] = reply_subject(str(p.get("subject") or project["subject"]))
        parent = project["last_message_id"]
        if parent:
            p["in_reply_to"] = parent
            p["references"] = [*(source.get("references") or [])[-10:], parent]
        # Historia jak u człowieka: pod odpowiedzią cytat maila, na który odpowiadamy (a w nim – wcześniejsze).
        if source.get("body") and source.get("sender"):
            p["body"] = f"{str(p.get('body') or '').rstrip()}\n\n" + quote_block(
                source["sender"], source.get("date"), source["body"])
        rel = p.pop("project_attachments", None)
        if rel:
            p["attachments"] = [f"{project['slug']}/{r.lstrip('/')}" for r in rel]
        return p

    # ---- przetwarzanie zadań ----
    def _peek_project_id(self, kind: str, payload: dict) -> int | None:
        """Projekt zadania bez skutków ubocznych (do sprawdzenia, czy nad projektem już ktoś pracuje)."""
        if kind != "email" or payload.get("trusted") is not True:
            return None
        refs = [r for r in [payload.get("in_reply_to"), *(payload.get("references") or [])] if r]
        p = self.projects.find_for_mail(payload.get("subject", ""), refs, payload.get("sender"))
        return p["id"] if p else None

    def _prepare(self, task):
        payload = json.loads(task["payload"])
        self.audit.record("task_started", task_id=task["id"], kind=task["kind"])
        self._started[task["id"]] = time.monotonic()   # rzeczywisty czas pracy do ewidencji
        if payload.get("trusted") is True and (task["kind"] == "import" or (
                task["kind"] == "email" and str(payload.get("subject", "")).strip().lower() == "diagnostyka")):
            return None, payload, True
        project = self._project_for(task["kind"], payload)
        if project is not None:
            self.store.set_task_project(task["id"], project["id"])
            if task["kind"] == "email":
                self.store.update_project(project["id"], last_contact_at=now_utc().isoformat())
            payload["project_id"] = project["id"]
        return project, payload, False

    def _apply(self, task, project, payload: dict, actions) -> None:
        self.audit.record("plan", task_id=task["id"], actions=[a.kind for a in actions])
        if project is not None:
            project = self.store.get_project(project["id"])
        # Najpierw zmiany projektu (nazwa katalogu), potem skutki zewnętrzne.
        for a in sorted(actions, key=lambda a: a.kind != PROJECT_UPDATE):
            if a.kind == PROJECT_UPDATE:
                if project is not None:
                    project = self._apply_project_update(project, a.payload, task["id"])
                continue
            act = self._threaded(project, a.payload, payload) if (project and a.kind == guards.SEND_MAIL) \
                else a.payload
            if a.kind == guards.SEND_MAIL:
                act.pop("kategoria", None)
            if a.kind == guards.SEND_MAIL and not self._to_team_only(act):
                # Poza zespół Monika nie pisze (drugi bezpiecznik obok allowed_recipients w guards.py).
                self.audit.record("mail_blocked", reason="adresat spoza zespołu", to=str(act.get("to"))[:200])
                continue
            out = self.executor.submit(a.kind, act, task_id=task["id"])
            if project and a.kind == guards.SEND_MAIL:
                own = strip_quoted(str(a.payload.get("body", "")))   # bez cytatu – tylko słowa Moniki
                self.store.add_project_log(project["id"], "monika", own[:8000])
                self.projects.memory.archive(own, "odpowiedź Moniki", project["id"])
                if out.status == "executed" and out.result:
                    self.store.link_message(out.result, project["id"])
                    self.store.update_project(project["id"], last_message_id=out.result)
                    project = self.store.get_project(project["id"])

    def _paused(self, project_id: int) -> bool:
        p = self.store.get_project(project_id)
        return p is not None and p["status"] == "paused"

    def _to_team_only(self, act: dict) -> bool:
        to = act.get("to")
        rcpts = [to] if isinstance(to, str) else list(to or [])
        rcpts += list(act.get("cc") or []) + list(act.get("bcc") or [])
        return bool(rcpts) and bool(self.team) and all(str(r).lower() in self.team for r in rcpts)

    def _finish(self, task, error: Exception | None) -> None:
        self._started.pop(task["id"], None)
        if error is not None:
            msg = f"{type(error).__name__}: {error}"
            self.store.finish_task(task["id"], ok=False, error=msg)
            self.audit.record("task_failed", task_id=task["id"], error=msg)
        else:
            self.store.finish_task(task["id"], ok=True)
            self.audit.record("task_done", task_id=task["id"])

    def _diagnostics(self, payload: dict) -> list:
        from .diagnostics import report
        from .planner import PlannedAction
        if payload.get("data") is not None or "import" in str(payload.get("subject", "")).lower():
            body, subject = self._import_memory(payload.get("data")), "import pamięci"
        else:
            body, subject = report(self.cfg, self.store), "diagnostyka"
        return [PlannedAction(guards.SEND_MAIL, {"to": payload["sender"], "subject": f"Re: {subject}", "body": body})]

    def _import_memory(self, data: str | None) -> str:
        if not data:
            return "Brak załącznika .json – nic nie zaimportowałem."
        try:
            parsed = json.loads(data)
            stats = self.projects.memory.import_hearsay(parsed)
        except (ValueError, TypeError, AttributeError) as e:
            return f"Import nieudany: {type(e).__name__}: {e}"
        self.audit.record("memory_import", osoby=stats["osoby"], firmy=stats["firmy"], fakty=stats["fakty"],
                          pominiete=len(stats["pominiete"]))
        lines = [f"Zaimportowano jako wiedzę zasłyszaną: osoby {stats['osoby']}, firmy {stats['firmy']}, "
                 f"fakty {stats['fakty']}."]
        if stats["pominiete"]:
            lines += ["Pominięte (niejednoznaczne):", *stats["pominiete"][:50]]
        return "\n".join(lines)

    def process_next(self) -> bool:
        """Jedno zadanie synchronicznie (testy, workers=1)."""
        task = self.store.claim_next(exclude=self._deferred)
        if task is None:
            return False
        pid = self._peek_project_id(task["kind"], json.loads(task["payload"]))
        if pid is not None and self._paused(pid):
            self.store.requeue(task["id"])   # projekt wstrzymany w panelu – zadanie czeka
            self._deferred.add(task["id"])
            return True
        try:
            project, payload, diag = self._prepare(task)
            actions = self._diagnostics(payload) if diag else self.planner.plan(task["kind"], payload)
            self._apply(task, project, payload, actions)
        except Exception as e:
            self._finish(task, e)
            return True
        self._finish(task, None)
        return True

    # ---- równoległość: planowanie (model) w wątkach, wszystko inne w wątku głównym ----
    def _plan_in_thread(self, kind: str, payload: dict):
        local = Store(self.cfg.database)
        try:
            projects = Projects(local, self.projects.root, self.projects.web)
            with planner_projects(self.planner, projects):
                return self.planner.plan(kind, payload)
        finally:
            local.close()

    def collect_done(self) -> int:
        n = 0
        for fut in [f for f in self._running if f.done()]:
            task, project, payload = self._running.pop(fut)
            try:
                self._apply(task, project, payload, fut.result())
            except Exception as e:
                self._finish(task, e)
            else:
                self._finish(task, None)
            n += 1
        return n

    def dispatch(self) -> None:
        busy = {p["id"] for _, p, _ in self._running.values() if p is not None}
        while len(self._running) < self.workers and not self._stop.is_set() \
                and not guards.kill_switch_active(self.cfg):
            task = self.store.claim_next(exclude=self._deferred)
            if task is None:
                break
            payload = json.loads(task["payload"])
            pid = self._peek_project_id(task["kind"], payload)
            if pid is not None and (pid in busy or self._paused(pid)):
                self.store.requeue(task["id"])   # ten projekt jest w pracy – kolejność wiadomości ma znaczenie
                self._deferred.add(task["id"])
                continue
            try:
                project, payload, diag = self._prepare(task)
            except Exception as e:
                self._finish(task, e)
                continue
            if diag:
                try:
                    self._apply(task, None, payload, self._diagnostics(payload))
                    self._finish(task, None)
                except Exception as e:
                    self._finish(task, e)
                continue
            if project is not None:
                busy.add(project["id"])
            fut = self._pool.submit(self._plan_in_thread, task["kind"], payload)
            self._running[fut] = (task, project, payload)
        self._deferred.clear()

    def tick(self) -> str:
        """Jeden cykl. Zwraca: 'halted' | 'tampered' | 'ok'."""
        if guards.config_tampered(self.cfg):
            self.audit.record("halt", reason="config.yaml zmieniony w trakcie działania")
            return "tampered"
        if guards.kill_switch_active(self.cfg):
            if not self._halted:
                self.audit.record("halt", reason="plik STOP")
                self._halted = True
            return "halted"
        if self._halted:
            self.audit.record("resume", reason="plik STOP usunięty przez człowieka")
            self._halted = False
        self.ingest_mail()
        if self.workers <= 1:
            while not self._stop.is_set() and not guards.kill_switch_active(self.cfg) and self.process_next():
                pass
            self._deferred.clear()
        else:
            self.collect_done()
            self.dispatch()
        return "ok"

    def run(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self._stop.set())
        self.audit.record("start", dry_run=self.cfg.dry_run, integrations=self.cfg.integrations)
        while not self._stop.is_set():
            if self.tick() == "tampered":
                self._shutdown()
                return EXIT_CONFIG_TAMPERED
            # Gdy coś się liczy w tle – sprawdzamy częściej, żeby szybko wysłać wynik.
            self._stop.wait(2 if self._running else self.cfg.poll_interval_seconds)
            if self._running:
                self.collect_done()
        self._shutdown()
        self.audit.record("stop", reason="sygnał zakończenia")
        return 0

    def _shutdown(self) -> None:
        """Dokończ rozpoczęte zadania (wyniki trafiają normalnie przez Executor)."""
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self.collect_done()
