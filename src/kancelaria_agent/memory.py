"""Pamięć Marka – trwała, ponad projektami: ludzie, firmy, fakty i archiwum wszystkich wiadomości.

- Osoba ma wiele adresów e-mail (dawne i obecne) i historię zatrudnienia (firma, rola, od–do).
- Firma ma domeny, fakty i projekty.
- Archiwum (SQLite FTS5) indeksuje każdy mail przychodzący (z przekazanymi wiadomościami), każdą odpowiedź
  Marka i każdy zapisany fakt – bez limitu czasu.
- Rozpoznawanie w kodzie (adres e-mail, nazwisko w treści, domena firmy) daje modelowi karty znanych osób
  i firm; resztę model uzupełnia narzędziami (zapis faktów, łączenie nowych adresów z osobą).
Nic tu nie jest wykonywane – to dane.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS people_name_key ON people(name_key);
CREATE TABLE IF NOT EXISTS person_emails (
    email TEXT PRIMARY KEY,
    person_id INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS org_domains (
    domain TEXT PRIMARY KEY,
    org_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS person_orgs (
    person_id INTEGER NOT NULL,
    org_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    since TEXT NOT NULL,
    until TEXT,
    PRIMARY KEY (person_id, org_id)
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,            -- osoba|firma|ogolne
    subject_id INTEGER,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'marek',  -- marek | zaslyszane
    project_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_people (
    project_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    PRIMARY KEY (project_id, person_id)
);
CREATE TABLE IF NOT EXISTS project_orgs (
    project_id INTEGER NOT NULL,
    org_id INTEGER NOT NULL,
    PRIMARY KEY (project_id, org_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS archive USING fts5(
    text, kind UNINDEXED, project_id UNINDEXED, created_at UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

# Skrzynki publiczne – domena nie identyfikuje firmy.
FREE_MAIL = {"gmail.com", "googlemail.com", "wp.pl", "o2.pl", "onet.pl", "interia.pl", "op.pl", "yahoo.com",
             "outlook.com", "hotmail.com", "live.com", "icloud.com", "me.com", "gazeta.pl", "tlen.pl", "proton.me",
             "protonmail.com", "poczta.fm", "vp.pl", "example.com"}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_PL = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")
MAX_FACT = 500
SOURCES = {"marek": "Twoje doświadczenie", "zaslyszane": "zasłyszane w firmie (nie Twoje doświadczenie)"}

# Zdrobnienia → forma pełna (rozpoznawanie „Jarek Kowalski” = „Jarosław Kowalski”).
DIMINUTIVES = {
    "jarek": "jaroslaw", "kasia": "katarzyna", "kaska": "katarzyna", "tomek": "tomasz", "ola": "aleksandra",
    "olek": "aleksander", "alek": "aleksander", "basia": "barbara", "gosia": "malgorzata", "malgosia": "malgorzata",
    "kuba": "jakub", "asia": "joanna", "bartek": "bartlomiej", "wojtek": "wojciech", "krzysiek": "krzysztof",
    "ania": "anna", "magda": "magdalena", "piotrek": "piotr", "grzesiek": "grzegorz", "staszek": "stanislaw",
    "zbyszek": "zbigniew", "rysiek": "ryszard", "darek": "dariusz", "mirek": "miroslaw", "slawek": "slawomir",
    "radek": "radoslaw", "przemek": "przemyslaw", "zosia": "zofia", "julka": "julia", "maciek": "maciej",
    "szymek": "szymon", "seba": "sebastian", "aga": "agnieszka", "agata": "agata", "ewka": "ewa", "iza": "izabela",
    "ela": "elzbieta", "jola": "jolanta", "hania": "hanna", "kamilka": "kamila", "marcinek": "marcin",
    "pawelek": "pawel", "michal": "michal", "lukasz": "lukasz", "mateusz": "mateusz", "kamil": "kamil",
    "dorotka": "dorota", "natka": "natalia", "monia": "monika", "justa": "justyna", "wiki": "wiktoria",
    "rafal": "rafal", "andrzejek": "andrzej", "jedrek": "jedrzej", "franek": "franciszek", "antek": "antoni",
    "stasia": "stanislawa", "wiesiek": "wieslaw", "zenek": "zenon", "heniek": "henryk", "tadek": "tadeusz",
    "romek": "roman", "boguś": "boguslaw", "bogus": "boguslaw", "lesio": "leszek", "kazik": "kazimierz",
}


def canonical_name(name: str) -> str:
    """Klucz osoby: imię w formie pełnej + nazwisko (bez polskich znaków, małe litery)."""
    parts = key(name).split()
    if parts:
        parts[0] = DIMINUTIVES.get(parts[0], parts[0])
    return " ".join(parts)


def name_variants(canon: str) -> set[str]:
    parts = canon.split()
    if len(parts) < 2:
        return {canon}
    first, rest = parts[0], " ".join(parts[1:])
    firsts = {first} | {d for d, full in DIMINUTIVES.items() if full == first}
    return {f"{f} {rest}" for f in firsts} | {f"{rest} {f}" for f in firsts}


class MemoryError_(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def key(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text).translate(_PL)).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9@.\- ]+", " ", t).split())


def emails_in(text: str) -> list[str]:
    seen, out = set(), []
    for e in _EMAIL_RE.findall(text or ""):
        e = e.lower().strip(".")
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


class Memory:
    def __init__(self, db: sqlite3.Connection, own_domains: tuple[str, ...] = ("kancelariabil.pl",)):
        self.db = db
        self.own = tuple(d.lower() for d in own_domains)
        db.executescript(SCHEMA)
        cols = {r[1] for r in db.execute("PRAGMA table_info(facts)")}
        if "source" not in cols:
            db.execute("ALTER TABLE facts ADD COLUMN source TEXT NOT NULL DEFAULT 'marek'")
        for r in db.execute("SELECT id, name, name_key FROM people").fetchall():   # klucze ze zdrobnieniami
            if r["name_key"] != canonical_name(r["name"]):
                db.execute("UPDATE people SET name_key=? WHERE id=?", (canonical_name(r["name"]), r["id"]))
        self._backfill()

    def _backfill(self) -> None:
        """Raz: historia projektów sprzed wprowadzenia pamięci trafia do archiwum."""
        if self.db.execute("SELECT 1 FROM archive LIMIT 1").fetchone():
            return
        has_log = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='project_log'").fetchone()
        if not has_log:
            return
        for r in self.db.execute("SELECT project_id, role, text, created_at FROM project_log ORDER BY id").fetchall():
            kind = {"zespol": "mail od zespołu", "lukasz": "mail od zespołu", "monika": "odpowiedź Moniki", "marek": "odpowiedź Moniki"}.get(r["role"], "system")
            self.db.execute("INSERT INTO archive(text, kind, project_id, created_at) VALUES (?,?,?,?)",
                            (r["text"][:50_000], kind, r["project_id"], r["created_at"]))

    # ---------- archiwum ----------
    def archive(self, text: str, kind: str, project_id: int | None) -> None:
        if text and text.strip():
            self.db.execute("INSERT INTO archive(text, kind, project_id, created_at) VALUES (?,?,?,?)",
                            (text[:50_000], kind, project_id, _now()))

    def search(self, query: str, limit: int = 12) -> list[dict]:
        words = [w for w in re.findall(r"\w+", key(query)) if len(w) > 1][:12]
        if not words:
            return []
        q = " OR ".join(f'"{w}"*' for w in words)
        rows = self.db.execute(
            "SELECT kind, project_id, created_at, snippet(archive, 0, '«', '»', ' … ', 24) AS snip "
            "FROM archive WHERE archive MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---------- osoby i firmy ----------
    def _is_own(self, email: str) -> bool:
        return email.rpartition("@")[2] in self.own

    def person_by_email(self, email: str):
        return self.db.execute("SELECT p.* FROM person_emails e JOIN people p ON p.id=e.person_id WHERE e.email=?",
                               (email.lower(),)).fetchone()

    def people_by_name(self, name: str) -> list:
        return self.db.execute("SELECT * FROM people WHERE name_key=?", (canonical_name(name),)).fetchall()

    def org_by_name(self, name: str):
        return self.db.execute("SELECT * FROM orgs WHERE name_key=?", (key(name),)).fetchone()

    def org_by_domain(self, domain: str):
        return self.db.execute("SELECT o.* FROM org_domains d JOIN orgs o ON o.id=d.org_id WHERE d.domain=?",
                               (domain.lower(),)).fetchone()

    def upsert_org(self, name: str, domain: str | None = None) -> int:
        name = str(name).strip()[:150]
        if not key(name):
            raise MemoryError_("pusta nazwa firmy")
        row = self.org_by_name(name)
        now = _now()
        if row is None:
            oid = self.db.execute("INSERT INTO orgs(name, name_key, created_at, updated_at) VALUES (?,?,?,?)",
                                  (name, key(name), now, now)).lastrowid
        else:
            oid = row["id"]
        if domain:
            d = domain.lower().strip().lstrip("@")
            if d and d not in FREE_MAIL and not d.endswith(self.own):
                self.db.execute("INSERT OR IGNORE INTO org_domains(domain, org_id) VALUES (?,?)", (d, oid))
        return oid

    def remember_person(self, name: str, email: str | None = None, org: str | None = None, role: str = "",
                        fact: str | None = None, project_id: int | None = None, person_id: int | None = None,
                        source: str = "marek", since: str | None = None, until: str | None = None) -> int:
        """Tworzy albo uzupełnia osobę. Nowy adres e-mail dopisuje do tej samej osoby (zmiana pracy itp.).

        Gdy osób o tym nazwisku jest kilka, wymaga person_id (żeby nie skleić dwóch różnych ludzi)."""
        name = str(name).strip()[:120]
        now = _now()
        email = (email or "").strip().lower() or None
        if email and not _EMAIL_RE.fullmatch(email):
            raise MemoryError_(f"niepoprawny e-mail: {email}")
        if email and self._is_own(email):
            raise MemoryError_("to adres kancelarii, nie klienta")
        owner = self.person_by_email(email) if email else None
        if person_id is not None:
            pid = person_id
            if not self.db.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone():
                raise MemoryError_(f"brak osoby #{pid}")
            if owner is not None and owner["id"] != pid:
                raise MemoryError_(f"adres {email} należy do innej osoby (#{owner['id']} {owner['name']})")
        elif owner is not None:
            pid = owner["id"]
        else:
            same = self.people_by_name(name) if canonical_name(name) else []
            if len(same) > 1:
                raise MemoryError_("kilka osób o tym nazwisku: " + ", ".join(f"#{p['id']}" for p in same)
                                   + " – podaj osoba_id albo zapytaj zespół, o kogo chodzi")
            if same:
                pid = same[0]["id"]
            else:
                if not key(name):
                    raise MemoryError_("podaj imię i nazwisko")
                pid = self.db.execute("INSERT INTO people(name, name_key, created_at, updated_at) VALUES (?,?,?,?)",
                                      (name, canonical_name(name), now, now)).lastrowid
        if email:
            self.db.execute("INSERT INTO person_emails(email, person_id, first_seen, last_seen) VALUES (?,?,?,?) "
                            "ON CONFLICT(email) DO UPDATE SET last_seen=excluded.last_seen", (email, pid, now, now))
        if org:
            domain = email.rpartition("@")[2] if email else None
            oid = self.upsert_org(org, domain)
            current = self.db.execute("SELECT org_id FROM person_orgs WHERE person_id=? AND until IS NULL",
                                      (pid,)).fetchall()
            for c in current if until is None else []:   # nowa firma = koniec poprzedniego zatrudnienia
                if c["org_id"] != oid:
                    self.db.execute("UPDATE person_orgs SET until=? WHERE person_id=? AND org_id=?",
                                    (now, pid, c["org_id"]))
            prev = self.db.execute("SELECT role FROM person_orgs WHERE person_id=? AND org_id=?", (pid, oid)).fetchone()
            if prev and role and prev["role"] and prev["role"] != role:
                self.add_fact("osoba", pid, f"zmiana stanowiska w {org}: {prev['role']} → {role}", project_id, source)
            self.db.execute("INSERT INTO person_orgs(person_id, org_id, role, since, until) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(person_id, org_id) DO UPDATE SET until=excluded.until, "
                            "role=CASE WHEN excluded.role!='' THEN excluded.role ELSE role END",
                            (pid, oid, str(role)[:80], now if since is None else since, until or None))
            if project_id:
                self.db.execute("INSERT OR IGNORE INTO project_orgs VALUES (?,?)", (project_id, oid))
        if fact:
            self.add_fact("osoba", pid, fact, project_id, source)
        if project_id:
            self.db.execute("INSERT OR IGNORE INTO project_people VALUES (?,?)", (project_id, pid))
        self.db.execute("UPDATE people SET updated_at=? WHERE id=?", (now, pid))
        return pid

    def add_fact(self, subject: str, subject_id: int | None, text: str, project_id: int | None = None,
                  source: str = "marek") -> None:
        text = " ".join(str(text).split())[:MAX_FACT]
        if not text or source not in SOURCES:
            return
        if self.db.execute("SELECT 1 FROM facts WHERE subject=? AND subject_id IS ? AND text=?",
                           (subject, subject_id, text)).fetchone():
            return
        self.db.execute("INSERT INTO facts(subject, subject_id, text, project_id, created_at, source) "
                        "VALUES (?,?,?,?,?,?)", (subject, subject_id, text, project_id, _now(), source))
        self.archive(text, f"fakt:{subject}" + (" (zasłyszane)" if source == "zaslyszane" else ""), project_id)

    def forget_project(self, project_id: int) -> dict:
        """Usuwa z pamięci wszystko, co pochodzi z projektu (np. testowego). Osoby i firmy znikają tylko,
        gdy nie mają już innych projektów, faktów (w tym zasłyszanych) ani powiązań."""
        db = self.db
        people = [r[0] for r in db.execute("SELECT person_id FROM project_people WHERE project_id=?", (project_id,))]
        orgs = [r[0] for r in db.execute("SELECT org_id FROM project_orgs WHERE project_id=?", (project_id,))]
        n_facts = db.execute("DELETE FROM facts WHERE project_id=?", (project_id,)).rowcount
        db.execute("DELETE FROM archive WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM project_people WHERE project_id=?", (project_id,))
        db.execute("DELETE FROM project_orgs WHERE project_id=?", (project_id,))
        gone_people = 0
        for pid in people:
            if db.execute("SELECT 1 FROM project_people WHERE person_id=?", (pid,)).fetchone() or \
                    db.execute("SELECT 1 FROM facts WHERE subject='osoba' AND subject_id=?", (pid,)).fetchone():
                continue
            db.execute("DELETE FROM person_emails WHERE person_id=?", (pid,))
            db.execute("DELETE FROM person_orgs WHERE person_id=?", (pid,))
            db.execute("DELETE FROM people WHERE id=?", (pid,))
            gone_people += 1
        gone_orgs = 0
        for oid in orgs:
            if db.execute("SELECT 1 FROM project_orgs WHERE org_id=?", (oid,)).fetchone() or \
                    db.execute("SELECT 1 FROM facts WHERE subject='firma' AND subject_id=?", (oid,)).fetchone() or \
                    db.execute("SELECT 1 FROM person_orgs WHERE org_id=?", (oid,)).fetchone():
                continue
            db.execute("DELETE FROM org_domains WHERE org_id=?", (oid,))
            db.execute("DELETE FROM orgs WHERE id=?", (oid,))
            gone_orgs += 1
        return {"fakty": n_facts, "osoby": gone_people, "firmy": gone_orgs}

    def link_project_org(self, project_id: int, org_name: str) -> int:
        oid = self.upsert_org(org_name)
        self.db.execute("INSERT OR IGNORE INTO project_orgs VALUES (?,?)", (project_id, oid))
        return oid

    # ---------- rozpoznawanie ----------
    def recognize(self, text: str, sender: str | None = None) -> dict:
        """Kogo i co znamy z tej wiadomości: osoby (po e-mailu i nazwisku), firmy (po domenie i nazwie)."""
        body = text or ""
        norm = f" {key(body)} "
        people, orgs, unknown = {}, {}, []
        for e in emails_in(f"{sender or ''} {body}"):
            if self._is_own(e):
                continue
            p = self.person_by_email(e)
            if p:
                people[p["id"]] = p
            else:
                unknown.append(e)
            o = self.org_by_domain(e.rpartition("@")[2])
            if o:
                orgs[o["id"]] = o
        by_email = set(people)
        msg_domains = {e.rpartition("@")[2] for e in emails_in(f"{sender or ''} {body}") if not self._is_own(e)}
        warnings = []
        for p in self.db.execute("SELECT * FROM people"):
            if not p["name_key"] or len(p["name_key"]) <= 4:
                continue
            if any(f" {v} " in norm for v in name_variants(p["name_key"])):
                people[p["id"]] = p
                if p["id"] not in by_email:
                    w = self._mismatch(p, msg_domains)
                    if w:
                        warnings.append(w)
        for o in self.db.execute("SELECT * FROM orgs"):
            if o["name_key"] and len(o["name_key"]) > 2 and f" {o['name_key']} " in norm:
                orgs[o["id"]] = o
        return {"people": list(people.values()), "orgs": list(orgs.values()), "unknown_emails": unknown,
                "warnings": warnings}

    def _mismatch(self, p, msg_domains: set[str]) -> str | None:
        """Osoba rozpoznana tylko po nazwisku – czy adres/firma się zgadzają z tym, co pamiętamy?"""
        known = {r["email"].rpartition("@")[2] for r in self.db.execute(
            "SELECT email FROM person_emails WHERE person_id=?", (p["id"],))}
        jobs = self.db.execute("SELECT o.name, po.role, po.since, po.until FROM person_orgs po JOIN orgs o "
                               "ON o.id=po.org_id WHERE po.person_id=? ORDER BY po.since DESC", (p["id"],)).fetchall()
        new = msg_domains - known
        if not new:
            return None
        last = jobs[0] if jobs else None
        where = (f"ostatnio: {last['name']}" + (f" ({last['role']})" if last["role"] else "")
                 + f", od {last['since'][:7] or '?'}" + (f" do {last['until'][:7]}" if last["until"] else " – nadal"))\
            if last else "brak danych o firmie"
        return (f"UWAGA – {p['name']} (#{p['id']}) rozpoznany tylko po imieniu i nazwisku, a pisze z nieznanej domeny "
                f"{', '.join(sorted(new))}; w pamięci {where}. Zanim uznasz, że to ta sama osoba, porównaj fakty "
                f"z nowego maila (firma, stanowisko, „od kiedy”) z tym, co pamiętasz – jeśli coś się nie zgadza "
                f"(np. daty, równoległe role), nazwij tę sprzeczność zespołowi i zapytaj.")

    # ---------- karty ----------
    def person_card(self, pid: int, projects=None) -> str:
        p = self.db.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if p is None:
            return ""
        emails = [r["email"] for r in self.db.execute(
            "SELECT email FROM person_emails WHERE person_id=? ORDER BY last_seen DESC", (pid,))]
        jobs = self.db.execute("SELECT o.name, po.role, po.since, po.until FROM person_orgs po "
                               "JOIN orgs o ON o.id=po.org_id WHERE po.person_id=? ORDER BY po.since", (pid,)).fetchall()
        lines = [f"Osoba #{pid}: {p['name']} (w pamięci od {p['created_at'][:10]})"]
        if emails:
            lines.append("  e-maile: " + ", ".join(emails))
        for j in jobs:
            when = f"{j['since'][:10] or '?'}–{(j['until'] or '')[:10] or 'obecnie'}"
            lines.append(f"  firma: {j['name']}{' (' + j['role'] + ')' if j['role'] else ''}, {when}")
        lines += self._facts_lines("osoba", pid)
        lines += self._projects_lines("SELECT project_id FROM project_people WHERE person_id=?", pid, projects)
        return "\n".join(lines)

    def org_card(self, oid: int, projects=None) -> str:
        o = self.db.execute("SELECT * FROM orgs WHERE id=?", (oid,)).fetchone()
        if o is None:
            return ""
        domains = [r["domain"] for r in self.db.execute("SELECT domain FROM org_domains WHERE org_id=?", (oid,))]
        staff = self.db.execute("SELECT p.id, p.name, po.role, po.until FROM person_orgs po JOIN people p "
                                "ON p.id=po.person_id WHERE po.org_id=? ORDER BY po.until IS NOT NULL, p.name",
                                (oid,)).fetchall()
        lines = [f"Firma #{oid}: {o['name']}" + (f" ({', '.join(domains)})" if domains else "")]
        if staff:
            lines.append("  ludzie: " + "; ".join(
                f"{s['name']} #{s['id']}{' – ' + s['role'] if s['role'] else ''}{' (już tam nie pracuje)' if s['until'] else ''}"
                for s in staff))
        lines += self._facts_lines("firma", oid)
        lines += self._projects_lines("SELECT project_id FROM project_orgs WHERE org_id=?", oid, projects)
        return "\n".join(lines)

    def _facts_lines(self, subject: str, sid: int, limit: int = 25) -> list[str]:
        rows = self.db.execute("SELECT text, created_at, source FROM facts WHERE subject=? AND subject_id=? "
                               "ORDER BY id DESC LIMIT ?", (subject, sid, limit)).fetchall()
        own = [f"  - [{r['created_at'][:10]}] {r['text']}" for r in reversed(rows) if r["source"] == "marek"]
        heard = [f"  - {r['text']}" for r in reversed(rows) if r["source"] == "zaslyszane"]
        out = own
        if heard:
            out = out + ["  zasłyszane w firmie (nie Twoje doświadczenie – mów o tym jak o czymś, co wiesz od innych):"]
            out += ["  " + h for h in heard]
        return out

    def _projects_lines(self, sql: str, sid: int, projects) -> list[str]:
        ids = [r[0] for r in self.db.execute(sql, (sid,))]
        out = []
        for pid in ids[-15:]:
            pr = self.db.execute("SELECT id, client, name, stage, created_at FROM projects WHERE id=?",
                                 (pid,)).fetchone()
            if pr:
                out.append(f"  projekt #{pr['id']} ({pr['created_at'][:10]}): {pr['client']} – {pr['name'] or '?'}"
                           f", etap: {pr['stage']}")
        return out

    def find_card(self, query: str) -> str:
        q = str(query).strip()
        if "@" in q:
            p = self.person_by_email(q)
            return self.person_card(p["id"]) if p else f"nie znam adresu {q}"
        cards = [self.person_card(p["id"]) for p in self.people_by_name(q)]
        o = self.org_by_name(q)
        if o:
            cards.append(self.org_card(o["id"]))
        if not cards:   # częściowe dopasowanie
            k = key(q)
            for p in self.db.execute("SELECT id FROM people WHERE name_key LIKE ? LIMIT 5", (f"%{k}%",)):
                cards.append(self.person_card(p["id"]))
            for o in self.db.execute("SELECT id FROM orgs WHERE name_key LIKE ? LIMIT 5", (f"%{k}%",)):
                cards.append(self.org_card(o["id"]))
        return "\n\n".join(cards) or f"nic nie wiem o: {q}"

    def context(self, text: str, sender: str | None, project_id: int | None) -> str:
        """Blok <pamiec> do promptu: karty rozpoznanych osób i firm oraz osób/firm tego projektu."""
        rec = self.recognize(text, sender)
        pids = {p["id"] for p in rec["people"]}
        oids = {o["id"] for o in rec["orgs"]}
        if project_id:
            pids |= {r[0] for r in self.db.execute("SELECT person_id FROM project_people WHERE project_id=?",
                                                   (project_id,))}
            oids |= {r[0] for r in self.db.execute("SELECT org_id FROM project_orgs WHERE project_id=?",
                                                   (project_id,))}
        parts = [self.person_card(i) for i in sorted(pids)[:12]] + [self.org_card(i) for i in sorted(oids)[:8]]
        if rec["warnings"]:
            parts.insert(0, "\n".join(rec["warnings"]))
        if rec["unknown_emails"]:
            parts.append("Adresy e-mail z tej wiadomości, których nie znasz: " + ", ".join(rec["unknown_emails"][:10]))
        general = self._facts_lines("ogolne", 0, limit=15)
        if general:
            parts.append("Ogólne ustalenia:\n" + "\n".join(general))
        return "\n\n".join(p for p in parts if p) or "Nikogo z tej wiadomości jeszcze nie znasz."


    # ---------- import wiedzy zasłyszanej ----------
    def import_hearsay(self, data: dict) -> dict:
        """Import wiedzy z poczty kancelarii jako „zasłyszane”. Niejednoznaczne osoby pomija (raport)."""
        stats = {"osoby": 0, "firmy": 0, "fakty": 0, "pominiete": []}
        for f in data.get("firmy") or []:
            name = str(f.get("nazwa") or "").strip()
            if not name:
                continue
            doms = [d for d in f.get("domeny") or [] if isinstance(d, str)]
            oid = self.upsert_org(name, doms[0] if doms else None)
            for d in doms[1:]:
                self.upsert_org(name, d)
            for t in f.get("fakty") or []:
                self.add_fact("firma", oid, str(t), None, "zaslyszane")
                stats["fakty"] += 1
            stats["firmy"] += 1
        for o in data.get("osoby") or []:
            name = str(o.get("imie_nazwisko") or "").strip()
            emails = [e for e in o.get("emaile") or [] if isinstance(e, str) and _EMAIL_RE.fullmatch(e.strip())]
            jobs = o.get("firmy") or [{}]
            try:
                pid = None
                for i, e in enumerate(emails or [None]):
                    job = jobs[0] if i == 0 else {}
                    pid = self.remember_person(
                        name, e, job.get("nazwa") or None, job.get("rola") or "", None, None, pid, "zaslyszane",
                        _ym(job.get("od")), _ym(job.get("do")))
                for job in jobs[1:]:
                    if job.get("nazwa"):
                        self.remember_person(name, None, job["nazwa"], job.get("rola") or "", None, None, pid,
                                             "zaslyszane", _ym(job.get("od")), _ym(job.get("do")))
                for t in o.get("fakty") or []:
                    self.add_fact("osoba", pid, str(t), None, "zaslyszane")
                    stats["fakty"] += 1
                stats["osoby"] += 1
            except MemoryError_ as e:
                stats["pominiete"].append(f"{name}: {e}")
        return stats


def _ym(value) -> str | None:
    v = str(value or "").strip()
    return f"{v}-01T00:00:00+00:00" if re.fullmatch(r"\d{4}-\d{2}", v) else ""
