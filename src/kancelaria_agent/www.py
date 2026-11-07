"""Panel WWW Moniki: baza wiedzy (lista, wyszukiwanie, dodawanie, pobieranie, usuwanie plików).

Osobna usługa (`monika-www`), biblioteka standardowa, bez logowania – etap testów, w bazie wyłącznie
dane fikcyjne (decyzja Łukasza 3.11.2026). Przed wgraniem prawdziwych dokumentów kancelarii panel MUSI
dostać logowanie (docs/stan.md). Proces może pisać tylko do katalogu bazy wiedzy (systemd).
"""

from __future__ import annotations

import html
import os
import time
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .wiedza import EXTS, MAX_FILE, KnowledgeBase, KnowledgeError

MAX_UPLOAD = 120 * 1024 * 1024

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a65;--line:#e4e2dc;--acc:#1f4e5f;--acc2:#e8f0f2;--bad:#9b2c2c;--ok:#2d6a4f}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe7;--mute:#a09e97;--line:#33322f;--acc:#8cc2d3;--acc2:#22323a;--bad:#e08a8a;--ok:#8ccfa9}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:960px;margin:0 auto;padding:28px 16px 60px}
header{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:20px}
h1{font-size:22px;margin:0;letter-spacing:-.01em}h1 small{font-weight:400;color:var(--mute);font-size:15px;margin-left:6px}
.note{font-size:13px;color:var(--mute)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
h2{font-size:15px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
form.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=search]{flex:1;min-width:200px;padding:9px 11px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--ink);font:inherit}
button,.btn{padding:9px 14px;border:0;border-radius:7px;background:var(--acc);color:var(--card);font:inherit;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
button.link{background:none;color:var(--bad);padding:4px 6px;font-weight:500}
.drop{border:2px dashed var(--line);border-radius:9px;padding:18px;text-align:center;color:var(--mute)}
.drop.on{border-color:var(--acc);background:var(--acc2)}
table{width:100%;border-collapse:collapse}td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
td.num{white-space:nowrap;color:var(--mute);font-variant-numeric:tabular-nums}
a{color:var(--acc)}.hit{padding:10px 0;border-bottom:1px solid var(--line)}.hit:last-child{border:0}
.snip{color:var(--mute);font-size:14px;margin:4px 0 0}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:16px;background:var(--acc2)}.msg.bad{color:var(--bad)}
.empty{color:var(--mute);padding:8px 0}
@media (max-width:600px){td.hide,th.hide{display:none}}
"""

JS = """
const d=document.getElementById('drop'),f=document.getElementById('pliki'),l=document.getElementById('wybrane');
if(d){
const show=()=>{l.textContent=f.files.length?[...f.files].map(x=>x.name).join(', '):'';};
f.addEventListener('change',show);
['dragenter','dragover'].forEach(e=>d.addEventListener(e,ev=>{ev.preventDefault();d.classList.add('on')}));
['dragleave','drop'].forEach(e=>d.addEventListener(e,ev=>{ev.preventDefault();d.classList.remove('on')}));
d.addEventListener('drop',ev=>{f.files=ev.dataTransfer.files;show();});
d.addEventListener('click',()=>f.click());
}
"""


def _size(n: int) -> str:
    return f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{max(1, n // 1024)} KB"


def _page(body: str, msg: str = "", bad: bool = False) -> bytes:
    m = f'<div class="msg{" bad" if bad else ""}">{html.escape(msg)}</div>' if msg else ""
    return f"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Monika – baza wiedzy</title>
<style>{CSS}</style></head><body><main>
<header><h1>Monika <small>baza wiedzy kancelarii</small></h1>
<span class="note">wersja testowa · bez logowania · tylko dane fikcyjne</span></header>
{m}{body}</main><script>{JS}</script></body></html>""".encode()


def render_index(kb: KnowledgeBase, query: str = "", msg: str = "", bad: bool = False) -> bytes:
    entries = kb.entries()
    accept = ",".join(sorted(EXTS))
    parts = [f"""<section class="card"><h2>Szukaj</h2>
<form class="row" method="get" action="/"><input type="search" name="q" value="{html.escape(query)}"
placeholder="np. zgromadzenie wspólników, fundacja rodzinna, odsetki" autofocus><button>Szukaj</button></form>"""]
    if query:
        hits = kb.search(query, limit=20)
        if hits:
            parts.append("".join(
                f'<div class="hit"><a href="/plik/{quote(h.name)}">{html.escape(h.name)}</a>'
                + "".join(f'<p class="snip">{html.escape(s)}</p>' for s in h.snippets) + "</div>" for h in hits))
        else:
            parts.append(f'<p class="empty">Brak trafień dla „{html.escape(query)}”.</p>')
    parts.append("</section>")
    parts.append(f"""<section class="card"><h2>Dodaj pliki</h2>
<form method="post" action="/dodaj" enctype="multipart/form-data">
<div class="drop" id="drop">Przeciągnij pliki tutaj albo kliknij, żeby wybrać<br>
<span class="note">PDF, Word, Excel, tekst · do {MAX_FILE // 2**20} MB na plik · można też wysłać mailem
o temacie „wiedza” na adres Moniki</span><div id="wybrane" class="note"></div></div>
<input type="file" id="pliki" name="pliki" multiple accept="{accept}" hidden>
<p><button>Dodaj do bazy</button></p></form></section>""")
    rows = "".join(
        f'<tr><td><a href="/plik/{quote(e.name)}">{html.escape(e.name)}</a></td>'
        f'<td class="num">{_size(e.size)}</td>'
        f'<td class="num hide">{time.strftime("%d.%m.%Y %H:%M", time.localtime(e.mtime))}</td>'
        f'<td><form method="post" action="/usun" onsubmit="return confirm(\'Usunąć {html.escape(e.name)}?\')">'
        f'<input type="hidden" name="plik" value="{html.escape(e.name)}"><button class="link">usuń</button></form></td></tr>'
        for e in entries)
    parts.append(f"""<section class="card"><h2>Pliki w bazie ({len(entries)})</h2>
{'<table><tr><th>Plik</th><th>Rozmiar</th><th class="hide">Dodano</th><th></th></tr>' + rows + '</table>'
 if entries else '<p class="empty">Baza jest pusta.</p>'}</section>""")
    return _page("".join(parts), msg, bad)


def parse_multipart(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    msg = BytesParser(policy=policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
    out = []
    for part in msg.iter_parts() if msg.is_multipart() else []:
        name = part.get_filename()
        if name:
            out.append((name, part.get_payload(decode=True) or b""))
    return out


def make_handler(kb: KnowledgeBase, log=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Monika"

        def log_message(self, fmt, *args):  # bez zaśmiecania journala; zdarzenia w audycie
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _redirect(self, msg: str = "", bad: bool = False):
            loc = "/" + (f"?msg={quote(msg)}" + ("&bad=1" if bad else "") if msg else "")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            if url.path == "/zdrowie":
                return self._send(200, b"ok", "text/plain; charset=utf-8")
            if url.path == "/":
                return self._send(200, render_index(kb, (q.get("q") or [""])[0].strip(),
                                                    (q.get("msg") or [""])[0], bool(q.get("bad"))))
            if url.path.startswith("/plik/"):
                name = unquote(url.path[len("/plik/"):])
                try:
                    p = kb.path(name)
                except KnowledgeError:
                    return self._send(404, _page("<p>Nie ma takiego pliku.</p>"))
                return self._send(200, p.read_bytes(), "application/octet-stream",
                                  {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(p.name)}"})
            return self._send(404, _page("<p>Nie ma takiej strony. <a href='/'>Wróć</a></p>"))

        def do_POST(self):
            url = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD:
                return self._redirect(f"Za duże przesłanie (limit {MAX_UPLOAD // 2**20} MB naraz).", True)
            body = self.rfile.read(length)
            if url.path == "/dodaj":
                files = parse_multipart(self.headers.get("Content-Type", ""), body)
                saved, errors = [], []
                for name, data in files:
                    try:
                        saved.append(kb.save(name, data))
                    except KnowledgeError as e:
                        errors.append(str(e))
                if log:
                    log("wiedza_panel_dodaj", dodane=saved, bledy=len(errors))
                if not files:
                    return self._redirect("Nie wybrano plików.", True)
                msg = (f"Dodano: {', '.join(saved)}." if saved else "") + \
                      (f" Pominięto: {'; '.join(errors)}" if errors else "")
                return self._redirect(msg.strip(), bool(errors) and not saved)
            if url.path == "/usun":
                name = (parse_qs(body.decode("utf-8", "replace")).get("plik") or [""])[0]
                try:
                    kb.delete(name)
                except KnowledgeError as e:
                    return self._redirect(str(e), True)
                if log:
                    log("wiedza_panel_usun", plik=name)
                return self._redirect(f"Usunięto: {name}.")
            return self._send(404, _page("<p>Nie ma takiej strony.</p>"))

    return Handler


def serve(knowledge_dir: Path, host: str = "0.0.0.0", port: int = 80, log=None) -> None:
    kb = KnowledgeBase(knowledge_dir)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((host, port), make_handler(kb, log))
    srv.daemon_threads = True
    if log:
        log("www_start", port=port)
    srv.serve_forever()


def port_from_env() -> int:
    return int(os.environ.get("WWW_PORT") or 80)
