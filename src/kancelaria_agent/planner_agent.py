"""Planer-agent: Monika wykonuje zlecenia zespołu kancelarii przez Claude Agent SDK.

Granice (hook PreToolUse – deny ma pierwszeństwo):
- brak wbudowanych narzędzi Claude Code poza WebSearch/WebFetch: żadnej powłoki ani plików;
- WebSearch tylko z allowed_domains z OFFICIAL_DOMAINS, WebFetch tylko na adresy z tych domen
  (oficjalne źródła prawa) – i tylko gdy web_research w config.yaml nie jest wyłączone;
- własne narzędzia (serwer „kancelaria”): pliki wyłącznie w katalogu sprawy (zapis tylko do pisma/),
  baza wzorów tylko do odczytu, kalkulator odsetek, pamięć;
- maile wyłącznie przez Executor (planer zwraca tylko PlannedAction), adresata ustala kod: osoba
  z zespołu, która przysłała zlecenie.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import guards
from .planner import PROJECT_UPDATE, PlannedAction, _untrusted_report

MAX_TURNS = 80
TASK_TIMEOUT_S = 25 * 60
PROMPTS = Path(__file__).parent / "prompts"
STATUSES = {"pytania", "w_realizacji", "gotowe", "odmowa"}
SERVER = "kancelaria"

# Oficjalne źródła prawa (WebSearch: allowed_domains ⊆ tej listy; WebFetch: host w tej liście lub poddomena).
OFFICIAL_DOMAINS = (
    "isap.sejm.gov.pl", "dziennikustaw.gov.pl", "monitorpolski.gov.pl", "sejm.gov.pl", "legislacja.gov.pl",
    "www.gov.pl", "podatki.gov.pl", "biznes.gov.pl", "eureka.mf.gov.pl", "orzeczenia.nsa.gov.pl", "sn.pl", "orzeczenia.ms.gov.pl",
    "trybunal.gov.pl", "nbp.pl", "ekrs.ms.gov.pl", "prs.ms.gov.pl", "uodo.gov.pl", "eur-lex.europa.eu",
    "curia.europa.eu",
)


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def system_prompt(with_project: bool, web: bool) -> str:
    parts = [_prompt("monika.md")]
    if with_project:
        parts.append(_prompt("pamiec.md"))
    parts.append("# Źródła w internecie\n" + (
        "szukaj_w_sieci = WebSearch z allowed_domains wyłącznie z listy: " + ", ".join(OFFICIAL_DOMAINS)
        + ". czytaj_strone = WebFetch tylko na adresy z tych domen. Inne domeny są zablokowane."
        if web else "Wyszukiwanie w internecie jest wyłączone – pracuj na dokumentach i wiedzy, zaznacz, "
                    "co wymaga sprawdzenia w źródłach."))
    lessons = PROMPTS / "doswiadczenia.md"
    if lessons.exists():
        parts.append(lessons.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def official_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS)


def decide(tool_name: str, tool_input: dict, web: bool) -> tuple[bool, str]:
    """Bramka narzędzi (czysta funkcja – testowana bez SDK)."""
    if tool_name.startswith(f"mcp__{SERVER}__"):
        return True, "narzędzie kancelarii"
    if tool_name == "WebSearch":
        if not web:
            return False, "wyszukiwanie wyłączone w config.yaml"
        domains = tool_input.get("allowed_domains") or []
        if not domains or not all(isinstance(d, str) and official_host(d) for d in domains):
            return False, "WebSearch tylko z allowed_domains z listy oficjalnych źródeł"
        return True, "wyszukiwanie w oficjalnych źródłach"
    if tool_name == "WebFetch":
        if not web:
            return False, "pobieranie stron wyłączone w config.yaml"
        try:
            parts = urlsplit(str(tool_input.get("url") or ""))
        except ValueError:
            return False, "niepoprawny adres"
        if parts.scheme not in ("https", "http") or parts.username or parts.password or not official_host(
                parts.hostname or ""):
            return False, "WebFetch tylko na strony oficjalnych źródeł prawa"
        return True, "strona oficjalnego źródła"
    return False, f"narzędzie niedozwolone: {tool_name}"


def parse_reply(text: str) -> dict[str, Any]:
    """JSON od modelu (ostatni obiekt z polem wiadomosc); przy błędzie formatu – cała odpowiedź jako treść."""
    for m in reversed(list(re.finditer(r"\{", text))):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and str(obj.get("wiadomosc", "")).strip():
            return obj
    return {"tytul_zadania": "", "wiadomosc": text.strip(), "wymaga_czlowieka": True}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _allow(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": reason}}


class AgentPlanner:
    def __init__(self, owner_email: str | None, api_key: str | None = None, model: str | None = None,
                 audit=None, projects=None, templates_dir: Path | None = None, web: bool = True,
                 client_replies: bool = False):
        self._projects = projects
        self._local = threading.local()
        self.owner_email = owner_email      # pierwsza osoba z zespołu: zgłoszenia o obcych mailach
        self.templates_dir = Path(templates_dir) if templates_dir else None
        self.web = web
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5"
        self.audit = audit
        if not self.api_key:
            raise RuntimeError("brak ANTHROPIC_API_KEY")

    @property
    def projects(self):
        return getattr(self._local, "projects", None) or self._projects

    @contextmanager
    def use_projects(self, projects):
        """Rejestr spraw z połączeniem do bazy tego wątku (zadania równoległe)."""
        self._local.projects = projects
        try:
            yield
        finally:
            self._local.projects = None

    def _log(self, event: str, **kw) -> None:
        if self.audit:
            self.audit.record(event, **kw)

    def __repr__(self) -> str:
        return f"AgentPlanner(model={self.model!r}, web={self.web})"

    # ---- narzędzia ----
    def _tools(self, project_id: int | None) -> list:
        from claude_agent_sdk import tool

        from . import odsetki
        from . import project_tools as pt

        def ok(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}]}

        def err(e: Exception) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": f"błąd: {e}"}], "is_error": True}

        def guarded(fn):
            # W wątku pętli (nie to_thread): połączenie SQLite jest jednowątkowe.
            async def run(args: dict[str, Any]) -> dict[str, Any]:
                try:
                    return fn(args)
                except (pt.ToolError, odsetki.OdsetkiError, OSError, ValueError, KeyError, TypeError,
                        ArithmeticError) as e:
                    return err(e)
            return run

        @tool("oblicz_odsetki", "Oblicz odsetki (jedyny dozwolony sposób liczenia odsetek). rodzaj: 'opoznienie' "
              "(art. 481 § 2 k.c., domyślnie), 'kapitalowe' (art. 359 § 2 k.c.) albo 'handlowe' (transakcje "
              "handlowe). od = pierwszy dzień opóźnienia (dzień po terminie płatności), do = dzień zapłaty "
              "albo dzień, na który liczysz (włącznie). Daty dd.mm.rrrr. Kilka należności = osobne wywołania.",
              {"type": "object", "properties": {
                  "kwota": {"type": "string"}, "od": {"type": "string"}, "do": {"type": "string"},
                  "rodzaj": {"type": "string", "enum": list(odsetki.RODZAJE)},
                  "dluznik_publiczny_podmiot_leczniczy": {"type": "boolean"},
                  "transakcja_zawarta_przed_2020": {"type": "boolean"}},
               "required": ["kwota", "od", "do"]})
        @guarded
        def interest(args):
            w = odsetki.oblicz(args["kwota"], odsetki.parse_date(args["od"]), odsetki.parse_date(args["do"]),
                               args.get("rodzaj") or "opoznienie",
                               bool(args.get("dluznik_publiczny_podmiot_leczniczy")),
                               bool(args.get("transakcja_zawarta_przed_2020")))
            self._log("odsetki", project_id=project_id, rodzaj=w.rodzaj, suma=str(w.suma))
            return ok(w.opis())

        @tool("wzory_lista", "Lista wzorów dokumentów kancelarii (opcjonalnie filtr słów, np. 'umowa spolki').",
              {"type": "object", "properties": {"szukaj": {"type": "string"}}})
        @guarded
        def templates(args):
            if self.templates_dir is None:
                return ok("baza wzorów nie jest skonfigurowana")
            rows = pt.list_templates(self.templates_dir, args.get("szukaj") or "")
            return ok("\n".join(f"{n} ({s // 1024} KB)" for n, s in rows) or "brak pasujących wzorów")

        @tool("wzor_czytaj", "Odczytaj treść wzoru z bazy (ścieżka z wzory_lista).",
              {"type": "object", "properties": {"sciezka": {"type": "string"}, "od_znaku": {"type": "integer"}},
               "required": ["sciezka"]})
        @guarded
        def template_read(args):
            if self.templates_dir is None:
                return err(ValueError("baza wzorów nie jest skonfigurowana"))
            return {"content": pt.read_template(self.templates_dir, args["sciezka"], args.get("od_znaku") or 0)}

        tools = [interest, templates, template_read]
        if project_id is None or self.projects is None:
            return tools

        def pdir() -> Path:
            return self.projects.dir(self.projects.store.get_project(project_id))

        def mem():
            return self.projects.memory

        from .memory import MemoryError_

        @tool("czytaj_dokument", "Odczytaj dokument sprawy (PDF, Word, Excel, tekst, zdjęcie). Długie: od_znaku; "
              "skany PDF: strona = od której strony obrazy.",
              {"type": "object", "properties": {"sciezka": {"type": "string"}, "od_znaku": {"type": "integer"},
                                                "strona": {"type": "integer"}}, "required": ["sciezka"]})
        @guarded
        def read(args):
            return {"content": pt.read_file(pdir(), args["sciezka"], args.get("od_znaku") or 0,
                                            args.get("strona"))}

        def same_client(other) -> bool:
            """Inne sprawy tylko tego samego klienta (konflikt interesów, wstrzyknięcia z dokumentów)."""
            cur = self.projects.store.get_project(project_id)
            a, b = str(cur["client"] or "").strip().lower(), str(other["client"] or "").strip().lower()
            return bool(a) and a == b

        @tool("pliki_sprawy", "Lista plików sprawy: bieżącej albo innej sprawy TEGO SAMEGO klienta (po numerze).",
              {"type": "object", "properties": {"sprawa_id": {"type": "integer"}}})
        @guarded
        def listing(args):
            pid = int(args.get("sprawa_id") or project_id)
            p = self.projects.store.get_project(pid)
            if p is None or (pid != project_id and not same_client(p)):
                return err(ValueError("brak takiej sprawy tego klienta"))
            return ok("\n".join(f"{n} ({s // 1024} KB)" for n, s in self.projects.list_files(p)) or "brak plików")

        @tool("skopiuj_ze_sprawy", "Skopiuj dokument z innej sprawy TEGO SAMEGO klienta (np. umowę spółki) do "
              "dokumenty/ bieżącej.", {"sprawa_id": int, "sciezka": str})
        @guarded
        def copy_other(args):
            import shutil
            other = self.projects.store.get_project(int(args["sprawa_id"]))
            if other is None or other["id"] == project_id or not same_client(other):
                return err(ValueError("brak takiej sprawy tego klienta"))
            src = pt._in(self.projects.dir(other), args["sciezka"], pt.READABLE)
            dst = pt._in(pdir(), f"dokumenty/ze-sprawy-{other['id']}/{src.name}", ("dokumenty",))
            if not src.is_file():
                return err(ValueError("brak pliku"))
            n = 2
            while dst.exists():
                dst = dst.with_name(f"{src.stem}-{n}{src.suffix}")
                n += 1
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return ok(str(dst.relative_to(pdir())))

        @tool("utworz_pismo", "Utwórz plik .docx w pisma/ (do załącznika). tresc: '# Tytuł', '## Podtytuł', "
              "akapity, '- punkt', '§ 1' / '1.' (numeracja zostaje jak napisana), '**pogrubienie**', tabele "
              "'| a | b |', '>> ' akapit do prawej (miejscowość, data), '---' nowa strona. wzor = ścieżka .docx "
              "z wzory_lista (styl, nagłówek i stopka kancelarii). Nowa wersja nie nadpisuje poprzedniej.",
              {"type": "object", "properties": {"nazwa": {"type": "string"}, "tresc": {"type": "string"},
                                                "wzor": {"type": "string"}}, "required": ["nazwa", "tresc"]})
        @guarded
        def docx_tool(args):
            rel = pt.create_docx(pdir(), args["nazwa"], args["tresc"], self.templates_dir, args.get("wzor"))
            self._log("pismo", project_id=project_id, plik=rel)
            return ok(rel)

        @tool("zapisz_notatke", "Zapisz notatkę roboczą .md w pisma/ (np. analiza do dalszej pracy).",
              {"nazwa": str, "tresc": str})
        @guarded
        def note(args):
            return ok(pt.write_note(pdir(), args["nazwa"], args["tresc"]))

        @tool("pamiec_szukaj", "Przeszukaj całą swoją pamięć (maile, odpowiedzi, fakty – bez limitu czasu).",
              {"zapytanie": str})
        @guarded
        def mem_search(args):
            hits = mem().search(args["zapytanie"])
            return ok("\n".join(f"[{h['created_at'][:10]}, sprawa #{h['project_id']}, {h['kind']}] {h['snip']}"
                                 for h in hits) or "nic nie znalazłam")

        @tool("pamiec_karta", "Karta osoby albo firmy z pamięci (imię i nazwisko, e-mail albo nazwa).", {"kto": str})
        @guarded
        def mem_card(args):
            return ok(mem().find_card(args["kto"]))

        @tool("pamiec_zapisz_osobe", "Zapamiętaj osobę: klient, wspólnik, członek zarządu, pełnomocnik, fakt o niej.",
              {"type": "object", "properties": {
                  "imie_nazwisko": {"type": "string"}, "email": {"type": "string"}, "firma": {"type": "string"},
                  "rola": {"type": "string"}, "fakt": {"type": "string"}, "osoba_id": {"type": "integer"}},
               "required": ["imie_nazwisko"]})
        @guarded
        def mem_person(args):
            try:
                pid = mem().remember_person(args["imie_nazwisko"], args.get("email"), args.get("firma"),
                                            args.get("rola") or "", args.get("fakt"), project_id,
                                            args.get("osoba_id"))
            except MemoryError_ as e:
                return err(e)
            return ok(mem().person_card(pid))

        @tool("pamiec_zapisz_firme", "Zapamiętaj firmę (klienta) albo fakt o niej (ustalenia, stan, terminy).",
              {"type": "object", "properties": {"nazwa": {"type": "string"}, "domena": {"type": "string"},
                                                "fakt": {"type": "string"}}, "required": ["nazwa"]})
        @guarded
        def mem_org(args):
            try:
                oid = mem().upsert_org(args["nazwa"], args.get("domena"))
            except MemoryError_ as e:
                return err(e)
            mem().db.execute("INSERT OR IGNORE INTO project_orgs VALUES (?,?)", (project_id, oid))
            if args.get("fakt"):
                mem().add_fact("firma", oid, args["fakt"], project_id)
            return ok(mem().org_card(oid))

        # Bez zapisu „reguł ogólnych”: reguły dla wszystkich spraw dopisuje zespół przez PR (doswiadczenia.md) –
        # inaczej dokument klienta z ukrytym poleceniem mógłby ustawić regułę dla całej kancelarii.
        return tools + [read, listing, copy_other, docx_tool, note, mem_search, mem_card, mem_person, mem_org]

    def _options(self, project_id: int | None = None):
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, create_sdk_mcp_server

        async def pre(input_data, tool_use_id, context):
            name = input_data.get("tool_name", "")
            allowed, reason = decide(name, input_data.get("tool_input") or {}, self.web)
            self._log("tool_gate", tool=name, allow=allowed, reason=reason)
            return _allow(reason) if allowed else _deny(reason)

        with_project = self.projects is not None and project_id is not None
        return ClaudeAgentOptions(
            system_prompt=system_prompt(with_project, self.web),
            model=self.model,
            tools=["WebSearch", "WebFetch"] if self.web else [],
            setting_sources=[],
            mcp_servers={SERVER: create_sdk_mcp_server(name=SERVER, tools=self._tools(project_id))},
            hooks={"PreToolUse": [HookMatcher(hooks=[pre])]},
            max_turns=MAX_TURNS,
            max_buffer_size=32 * 1024 * 1024,    # skany stron PDF jako obrazy
            env={"ANTHROPIC_API_KEY": self.api_key, "DISABLE_AUTOUPDATER": "1"},
        )

    async def _run(self, prompt: str, project_id: int | None = None) -> str:
        from claude_agent_sdk import ClaudeSDKClient, ResultMessage

        result = ""
        async with ClaudeSDKClient(options=self._options(project_id)) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    self._local.cost = float(getattr(msg, "total_cost_usd", 0) or 0)
                    usage = getattr(msg, "usage", None) or {}
                    self._log("ai_cost", project_id=project_id, cost_usd=round(self._local.cost, 4),
                              turns=getattr(msg, "num_turns", None), model=self.model,
                              seconds=round((getattr(msg, "duration_ms", 0) or 0) / 1000),
                              input=usage.get("input_tokens"), output=usage.get("output_tokens"),
                              cache_read=usage.get("cache_read_input_tokens"),
                              cache_write=usage.get("cache_creation_input_tokens"))
                    if msg.is_error or msg.result is None:
                        raise RuntimeError(f"agent zakończył się błędem: {msg.subtype}")
                    result = msg.result
        return result

    def run_agent(self, prompt: str, project_id: int | None = None) -> str:
        return asyncio.run(asyncio.wait_for(self._run(prompt, project_id), TASK_TIMEOUT_S))

    # ---- Planner ----
    def plan(self, task_kind: str, payload: dict[str, Any]) -> list[PlannedAction]:
        if task_kind != "email":
            return []
        if payload.get("trusted") is not True:
            return _untrusted_report(payload, self.owner_email) if self.owner_email else []

        sender, subject, body = payload["sender"], payload["subject"], str(payload.get("body", ""))
        project_id = payload.get("project_id") if self.projects is not None else None
        project = self.projects.store.get_project(project_id) if project_id is not None else None

        saved = payload.get("saved_files") or []
        names = ", ".join(payload.get("attachments") or []) or "brak"
        prompt = (f"<zlecenie>\nOd: {sender}\nTemat: {subject}\nZałączniki maila: {names}\n"
                  + (f"Zapisane w sprawie (czytaj_dokument): {', '.join(saved)}\n" if saved else "")
                  + f"\n{body}\n</zlecenie>")
        if project is not None:
            mem = self.projects.memory.context(body, sender, project_id)
            prompt = (f"<pamiec>\n{mem}\n</pamiec>\n\n"
                      f"<sprawa>\n{self.projects.context(project)}\n</sprawa>\n\n" + prompt)

        self._local.cost = 0.0
        try:
            reply = parse_reply(self.run_agent(prompt, project_id))
        except Exception as e:  # noqa: BLE001 – każdy błąd zgłaszamy osobie, która zleciła
            self._log("agent_error", error=f"{type(e).__name__}: {e}"[:500])
            return [PlannedAction(guards.SEND_MAIL, {
                "to": sender, "subject": f"Re: {subject}"[:200],
                "body": ("Cześć,\n\nprzerwało mi pracę w trakcie i nie skończyłam. Odpisz „ponów”, "
                         "to spróbuję jeszcze raz.\n\n"
                         f"(szczegóły techniczne: {type(e).__name__}: {e})"[:1500] + "\n\nMonika"),
            })]

        text = str(reply["wiadomosc"]).strip()
        status = reply.get("status") if reply.get("status") in STATUSES else "w_realizacji"
        actions = []
        if project is not None:
            actions.append(PlannedAction(PROJECT_UPDATE, {
                "klient": str(reply.get("klient") or "")[:120], "projekt": str(reply.get("projekt") or "")[:120],
                "status": status, "koszt_usd": getattr(self._local, "cost", 0.0)}))
        actions.append(PlannedAction(guards.CREATE_NOTION_TASK, {
            "title": str(reply.get("tytul_zadania") or f"Zlecenie: {subject}")[:200],
            "properties": {"from": sender, "message_id": payload["message_id"],
                           "wymaga_czlowieka": bool(reply.get("wymaga_czlowieka", False))},
        }))
        mail = {"to": sender, "subject": f"Re: {subject}"[:200], "body": text}
        files = [str(f) for f in reply.get("zalaczniki") or [] if isinstance(f, str)][:10]
        if files and project is not None:
            pdir = self.projects.dir(self.projects.store.get_project(project_id))
            ok = [f for f in files if f.split("/")[0] in ("pisma", "dokumenty") and (pdir / f).is_file()]
            if ok:
                mail["project_attachments"] = ok
            if len(ok) < len(files):
                self._log("zalaczniki_pominiete", pominiete=sorted(set(files) - set(ok)))
        actions.append(PlannedAction(guards.SEND_MAIL, mail))
        return actions
