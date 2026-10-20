"""Kolejka zadań i propozycji w SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ref TEXT UNIQUE,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    action_kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|executing|executed|rejected|failed
    result TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,               -- nazwa katalogu i subdomeny podglądu
    client TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    named INTEGER NOT NULL DEFAULT 0,        -- 1 = klient/projekt ustalone (slug już się nie zmienia)
    status TEXT NOT NULL DEFAULT 'active',   -- active|waiting|done
    sender TEXT NOT NULL,                    -- zleceniodawca (właściciel)
    reply_to TEXT,                           -- adres klienta z linii "odpisz do klienta:"
    subject TEXT NOT NULL DEFAULT '',
    last_message_id TEXT,
    figma_files TEXT NOT NULL DEFAULT '[]',
    upload_token TEXT UNIQUE NOT NULL,
    token_expires TEXT NOT NULL,
    last_upload_at TEXT,
    upload_done_at TEXT,
    resume_seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    project_id INTEGER,
    payload TEXT NOT NULL,
    send_after TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',  -- waiting|sent|failed
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS time_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    task_id INTEGER,
    hours REAL NOT NULL,            -- godziny (ewidencja; nieużywana w kancelarii)
    activity TEXT NOT NULL,
    machine_seconds REAL,           -- rzeczywisty czas pracy Marka (informacyjnie)
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_refs (
    source_ref TEXT PRIMARY KEY,      -- np. mail:<Message-ID> – przetworzone RAZ na zawsze (nawet po usunięciu projektu)
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS panel_logins (
    token_hash TEXT PRIMARY KEY,     -- jednorazowy link logowania (w bazie tylko skrót)
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending (do wysłania) | sent | used
    link TEXT                         -- pełny link tylko do czasu wysłania, potem czyszczony
);
CREATE TABLE IF NOT EXISTS panel_users (
    email TEXT PRIMARY KEY,
    pw_hash TEXT NOT NULL,            -- scrypt
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS panel_sessions (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_messages (
    message_id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS project_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    role TEXT NOT NULL,                      -- lukasz|marek|system
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Kolumny dodane później – dokładane do istniejących baz przy starcie.
MIGRATIONS = {"projects": [("public_id", "TEXT"), ("stage", "TEXT NOT NULL DEFAULT 'nowy'"),
                           ("offer_hours", "REAL"), ("wp_site", "INTEGER NOT NULL DEFAULT 0"),
                           ("ball", "TEXT"), ("offer_net", "REAL"), ("offer_sent_at", "TEXT"), ("note", "TEXT"),
                           ("cost_usd", "REAL NOT NULL DEFAULT 0"), ("last_contact_at", "TEXT")],
              "tasks": [("project_id", "INTEGER")]}

PROJECT_FIELDS = {"ball", "offer_net", "offer_sent_at", "note", "cost_usd", "last_contact_at", "wp_site", "offer_hours", "public_id", "stage", "slug", "client", "name", "named", "status", "reply_to", "subject", "last_message_id",
                  "figma_files", "upload_token", "token_expires", "last_upload_at", "upload_done_at"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path, shared: bool = False):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # shared=True: jedno połączenie dla wielu wątków (serwer plików), dostęp chroniony self.lock
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=not shared)  # autocommit
        self.lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")  # serwer plików pisze do tej samej bazy
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS projects_public_id ON projects(public_id)")
        self.db.execute("INSERT OR IGNORE INTO processed_refs(source_ref, created_at) "
                        "SELECT source_ref, created_at FROM tasks WHERE source_ref IS NOT NULL")
        for r in self.db.execute("SELECT id FROM projects WHERE public_id IS NULL").fetchall():
            self.db.execute("UPDATE projects SET public_id=? WHERE id=?", (self.new_public_id(), r["id"]))

    def new_public_id(self) -> str:
        """Losowy 6-cyfrowy numer podglądu (nie kolejny – żeby nie dało się przeglądać cudzych)."""
        import secrets
        while True:
            pid = str(100000 + secrets.randbelow(900000))
            if not self.db.execute("SELECT 1 FROM projects WHERE public_id=?", (pid,)).fetchone():
                return pid

    def close(self) -> None:
        self.db.close()

    # --- kolejka zadań ---
    def seen_ref(self, source_ref: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed_refs WHERE source_ref=?", (source_ref,)).fetchone() is not None

    def enqueue(self, kind: str, payload: dict[str, Any], source_ref: str | None = None) -> int | None:
        """Dodaje zadanie; zwraca None, jeśli source_ref był już kiedykolwiek przyjęty (deduplikacja).

        Deduplikacja opiera się na processed_refs, nie na tabeli zadań – usunięcie projektu (i jego zadań)
        nie może sprawić, że stare maile zostaną przetworzone drugi raz."""
        now = _now()
        if source_ref is not None:
            try:
                self.db.execute("INSERT INTO processed_refs(source_ref, created_at) VALUES (?,?)", (source_ref, now))
            except sqlite3.IntegrityError:
                return None
        try:
            cur = self.db.execute(
                "INSERT INTO tasks(source_ref, kind, payload, created_at, updated_at) VALUES (?,?,?,?,?)",
                (source_ref, kind, json.dumps(payload, ensure_ascii=False), now, now),
            )
        except sqlite3.IntegrityError:
            return None
        return cur.lastrowid

    def claim_next(self, exclude: set[int] | None = None) -> sqlite3.Row | None:
        skip = sorted(exclude or ())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM tasks WHERE status='queued'"
                + (f" AND id NOT IN ({','.join('?' * len(skip))})" if skip else "") + " ORDER BY id LIMIT 1",
                skip).fetchone()
            if row:
                self.db.execute(
                    "UPDATE tasks SET status='running', updated_at=? WHERE id=?", (_now(), row["id"])
                )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return row

    def requeue(self, task_id: int) -> None:
        self.db.execute("UPDATE tasks SET status='queued', updated_at=? WHERE id=?", (_now(), task_id))

    def requeue_running(self) -> int:
        """Po restarcie: zadania przerwane w trakcie wracają do kolejki."""
        return self.db.execute("UPDATE tasks SET status='queued', updated_at=? WHERE status='running'",
                               (_now(),)).rowcount

    def finish_task(self, task_id: int, ok: bool, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE tasks SET status=?, error=?, updated_at=? WHERE id=?",
            ("done" if ok else "failed", error, _now(), task_id),
        )

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def tasks_by_status(self, status: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM tasks WHERE status=? ORDER BY id", (status,)).fetchall()

    # --- propozycje ---
    def add_proposal(self, task_id: int | None, action_kind: str, payload: dict[str, Any], reason: str) -> int:
        cur = self.db.execute(
            "INSERT INTO proposals(task_id, action_kind, payload, reason, created_at) VALUES (?,?,?,?,?)",
            (task_id, action_kind, json.dumps(payload, ensure_ascii=False), reason, _now()),
        )
        return cur.lastrowid

    def get_proposal(self, proposal_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()

    def list_proposals(self, status: str | None = "pending") -> list[sqlite3.Row]:
        if status is None:
            return self.db.execute("SELECT * FROM proposals ORDER BY id").fetchall()
        return self.db.execute("SELECT * FROM proposals WHERE status=? ORDER BY id", (status,)).fetchall()

    def decide_proposal(self, proposal_id: int, status: str, result: str | None = None) -> bool:
        """Zmienia status tylko z 'pending' (chroni przed podwójnym wykonaniem)."""
        cur = self.db.execute(
            "UPDATE proposals SET status=?, result=?, decided_at=? WHERE id=? AND status='pending'",
            (status, result, _now(), proposal_id),
        )
        return cur.rowcount == 1

    def set_proposal_status(self, proposal_id: int, status: str, result: str | None = None) -> None:
        self.db.execute(
            "UPDATE proposals SET status=?, result=?, decided_at=? WHERE id=?",
            (status, result, _now(), proposal_id),
        )

    # --- stan ---
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO state(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --- projekty ---
    def create_project(self, slug: str, sender: str, subject: str, reply_to: str | None,
                       upload_token: str, token_expires: str) -> int:
        now = _now()
        cur = self.db.execute(
            "INSERT INTO projects(slug, public_id, sender, subject, reply_to, upload_token, token_expires, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (slug, self.new_public_id(), sender, subject, reply_to, upload_token, token_expires, now, now))
        return cur.lastrowid

    def get_project(self, project_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()

    def project_by_slug(self, slug: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()

    def project_by_public_id(self, public_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM projects WHERE public_id=?", (public_id,)).fetchone()

    def project_by_token(self, token: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM projects WHERE upload_token=?", (token,)).fetchone()

    def project_by_message(self, message_ids: list[str]) -> sqlite3.Row | None:
        for mid in message_ids:
            row = self.db.execute(
                "SELECT p.* FROM project_messages m JOIN projects p ON p.id=m.project_id WHERE m.message_id=?",
                (mid.strip(),)).fetchone()
            if row:
                return row
        return None

    def update_project(self, project_id: int, **fields: Any) -> None:
        bad = set(fields) - PROJECT_FIELDS
        if bad:
            raise ValueError(f"nieznane pola projektu: {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE projects SET {cols}, updated_at=? WHERE id=?",
                        (*fields.values(), _now(), project_id))

    def link_message(self, message_id: str, project_id: int) -> None:
        if message_id:
            self.db.execute("INSERT OR IGNORE INTO project_messages(message_id, project_id) VALUES (?,?)",
                            (message_id.strip(), project_id))

    def add_project_log(self, project_id: int, role: str, text: str) -> None:
        self.db.execute("INSERT INTO project_log(project_id, role, text, created_at) VALUES (?,?,?,?)",
                        (project_id, role, text, _now()))

    def project_log(self, project_id: int, limit: int = 20) -> list[sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM project_log WHERE project_id=? ORDER BY id DESC LIMIT ?",
                               (project_id, limit)).fetchall()
        return list(reversed(rows))

    def record_upload(self, project_id: int, done: bool = False) -> bool:
        """Wołane przez serwer plików: kolejny plik albo przycisk "Gotowe".

        "Gotowe" działa tylko, gdy od ostatniego wznowienia przyszedł jakiś plik; przełącza projekt
        w 'waiting', więc agent wznowi pracę w najbliższym cyklu."""
        now = _now()
        if done:
            cur = self.db.execute("UPDATE projects SET upload_done_at=?, status='waiting', updated_at=? "
                                  "WHERE id=? AND last_upload_at IS NOT NULL", (now, now, project_id))
            return cur.rowcount == 1
        self.db.execute("UPDATE projects SET last_upload_at=?, updated_at=? WHERE id=?", (now, now, project_id))
        return True

    def claim_resumable(self, idle_cutoff: str) -> list[sqlite3.Row]:
        """Projekty czekające na materiały, w których klient skończył wgrywać (przycisk albo cisza).

        Atomowo przełącza je na 'active' i podbija resume_seq (klucz deduplikacji zadania)."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute(
                "SELECT * FROM projects WHERE status='waiting' AND last_upload_at IS NOT NULL "
                "AND (upload_done_at IS NOT NULL OR last_upload_at < ?)", (idle_cutoff,)).fetchall()
            for r in rows:
                self.db.execute("UPDATE projects SET status='active', upload_done_at=NULL, last_upload_at=NULL, "
                                "resume_seq=resume_seq+1, updated_at=? WHERE id=?", (_now(), r["id"]))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return [self.get_project(r["id"]) for r in rows]


    def delete_project(self, project_id: int) -> None:
        """Usuwa projekt i wszystko, co jest z nim związane w bazie zadań (pamięć – Memory.forget_project)."""
        for sql in ("DELETE FROM project_log WHERE project_id=?", "DELETE FROM project_messages WHERE project_id=?",
                    "DELETE FROM time_log WHERE project_id=?", "DELETE FROM outbox WHERE project_id=?",
                    "UPDATE tasks SET project_id=NULL, payload='{}' WHERE project_id=?",
                    "DELETE FROM projects WHERE id=?"):
            self.db.execute(sql, (project_id,))

    def set_task_project(self, task_id: int, project_id: int) -> None:
        self.db.execute("UPDATE tasks SET project_id=? WHERE id=?", (project_id, task_id))

    def project_tasks(self, project_id: int, statuses=("queued", "running")) -> list[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self.db.execute(f"SELECT * FROM tasks WHERE project_id=? AND status IN ({q}) ORDER BY id",
                               (project_id, *statuses)).fetchall()

    # --- ewidencja czasu pracy ---
    def time_add(self, project_id: int, task_id, hours: float, activity: str, machine_seconds=None) -> int:
        return self.db.execute(
            "INSERT INTO time_log(project_id, task_id, hours, activity, machine_seconds, created_at) "
            "VALUES (?,?,?,?,?,?)", (project_id, task_id, float(hours), str(activity)[:300], machine_seconds,
                                     _now())).lastrowid

    def time_entries(self, project_id: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM time_log WHERE project_id=? ORDER BY id", (project_id,)).fetchall()

    def time_total(self, project_id: int) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(hours),0) FROM time_log WHERE project_id=?",
                                     (project_id,)).fetchone()[0])

    # --- kolejka wysyłki z opóźnieniem (ludzkie tempo odpowiedzi do klientów) ---
    def outbox_add(self, task_id, project_id, payload: dict, send_after: str) -> int:
        return self.db.execute(
            "INSERT INTO outbox(task_id, project_id, payload, send_after, created_at) VALUES (?,?,?,?,?)",
            (task_id, project_id, json.dumps(payload, ensure_ascii=False), send_after, _now())).lastrowid

    def outbox_due(self, now_iso: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM outbox WHERE status='waiting' AND send_after<=? ORDER BY send_after",
                               (now_iso,)).fetchall()

    def outbox_done(self, outbox_id: int, ok: bool) -> None:
        self.db.execute("UPDATE outbox SET status=? WHERE id=?", ("sent" if ok else "failed", outbox_id))
