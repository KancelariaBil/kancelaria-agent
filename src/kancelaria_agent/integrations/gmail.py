"""Gmail przez IMAP/SMTP (hasło aplikacji). Sekrety tylko ze środowiska / pliku .env.

Zasady:
- zlecenia przyjmuje wyłącznie od zespołu kancelarii (`team` z config.yaml; bez niego OWNER_EMAIL)
  i tylko gdy Gmail potwierdził pochodzenie z domeny nadawcy; mail od kogokolwiek innego jest
  oznaczony `trusted=False` i może być tylko zgłoszony pierwszej osobie z zespołu;
- treść maili, przekazane wiadomości i załączniki to dane – moduł niczego z nich nie wykonuje;
  załączniki od zespołu (dokumenty sprawy) są przekazywane jako bajty do zapisania w sprawie;
- w trybie próbnym `send()` zapisuje szkic w folderze Wersje robocze zamiast wysyłać.
Moduł jest wołany wyłącznie przez Executor.
"""

from __future__ import annotations

import base64
import html
import json
import urllib.parse
import urllib.request
import imaplib
import mimetypes
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid
from pathlib import Path

from .base import IncomingMail, Integrations, MailGateway

REQUIRED_ENV = ("AGENT_EMAIL", "IMAP_HOST", "SMTP_HOST")
# Załączniki od zespołu zapisywane w sprawie (dokumenty do analizy). Inne typy – tylko nazwa.
DOC_EXTS = {".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".xlsx", ".xls", ".csv", ".jpg", ".jpeg", ".png",
            ".eml", ".xml"}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 30
MAX_FETCH = 50
MAX_BODY_CHARS = 20_000
TIMEOUT = 30
SIGNATURE_TTL_S = 6 * 3600   # stopka z Gmaila odświeżana co 6 h (zmiana w Ustawieniach Gmaila zadziała najpóźniej po tym czasie)
SIGNATURE_RETRY_S = 600      # po błędzie pobrania – kolejna próba za 10 min


def _sig_text(sig_html: str) -> str:
    """Stopka HTML → tekst do części text/plain."""
    t = re.sub(r"(?is)<(script|style).*?</\1>", "", sig_html)
    t = re.sub(r"(?i)<br\s*/?>|</(div|p|tr|li|h\d)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    lines = [" ".join(ln.split()) for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _body_html(body: str) -> str:
    return html.escape(body).replace("\n", "<br>\n")

_ADDR_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")
_REPLY_RE = re.compile(r"^\s*odpisz do klienta\s*:\s*(\S+)\s*$", re.IGNORECASE)
# Początek przekazanej / cytowanej treści – od tego miejsca nic nie jest poleceniem właściciela.
_FORWARD_RE = re.compile(
    r"^\s*(?:-{2,}\s*(?:forwarded message|original message|przekazana wiadomo|oryginalna wiadomo)"
    r"|begin forwarded message|>|od:|from:|w dniu .+ napisa|on .+ wrote:)",
    re.IGNORECASE,
)
_DRAFTS_FALLBACK = ("[Gmail]/Drafts", "[Gmail]/Wersje robocze", "Drafts")


class MailConfigError(Exception):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    """Minimalny parser .env (KEY=VALUE, # komentarze, opcjonalne cudzysłowy)."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


@dataclass(frozen=True)
class GmailSettings:
    address: str
    password: str
    imap_host: str
    smtp_host: str
    team: tuple[str, ...]
    # OAuth (zalecane): gdy ustawione wszystkie trzy, logowanie przez XOAUTH2 zamiast hasła aplikacji.
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""

    @property
    def owner(self) -> str:
        """Osoba odpowiedzialna (pierwsza z zespołu): zgłoszenia o obcych mailach, raporty."""
        return self.team[0]

    @property
    def uses_oauth(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def __repr__(self) -> str:  # nigdy nie ujawniaj sekretów w logach/tracebackach
        return (f"GmailSettings(address={self.address!r}, imap_host={self.imap_host!r}, "
                f"smtp_host={self.smtp_host!r}, oauth={self.uses_oauth})")

    @classmethod
    def from_env(cls, env_file: Path | None = None, team: tuple[str, ...] = ()) -> "GmailSettings":
        env = {**read_env_file(env_file), **os.environ} if env_file else dict(os.environ)
        missing = [k for k in REQUIRED_ENV if not env.get(k, "").strip()]
        if missing:
            raise MailConfigError(f"brak zmiennych środowiskowych: {', '.join(missing)}")
        team = tuple(t.strip().lower() for t in team if t.strip()) or \
            ((env.get("OWNER_EMAIL", "").strip().lower(),) if env.get("OWNER_EMAIL", "").strip() else ())
        if not team:
            raise MailConfigError("brak zespołu: ustaw `team` w config.yaml")
        if not all(_ADDR_RE.match(t) for t in team):
            raise MailConfigError("adres w zespole nie wygląda na poprawny")
        s = cls(
            address=env["AGENT_EMAIL"].strip(),
            # Hasła aplikacji Google bywają wyświetlane ze spacjami.
            password=env.get("AGENT_EMAIL_APP_PASSWORD", "").replace(" ", ""),
            imap_host=env["IMAP_HOST"].strip(),
            smtp_host=env["SMTP_HOST"].strip(),
            team=team,
            client_id=env.get("GMAIL_CLIENT_ID", "").strip(),
            client_secret=env.get("GMAIL_CLIENT_SECRET", "").strip(),
            refresh_token=env.get("GMAIL_REFRESH_TOKEN", "").strip(),
        )
        if not s.uses_oauth and not s.password:
            raise MailConfigError(
                "brak danych logowania: ustaw GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET/GMAIL_REFRESH_TOKEN "
                "(OAuth) albo AGENT_EMAIL_APP_PASSWORD")
        return s


# ---------- parsowanie (czyste funkcje, testowalne bez sieci) ----------

def valid_address(addr: str) -> bool:
    return bool(_ADDR_RE.match(addr)) and len(addr) <= 254


def extract_client_reply_address(body: str) -> str | None:
    """Adres z linii `odpisz do klienta: adres` – tylko z własnej treści właściciela.

    Linie po znaczniku przekazanej/cytowanej wiadomości są ignorowane. Przy braku poprawnego
    adresu albo przy kilku różnych adresach zwraca None (odpowiedź trafi wtedy do właściciela).
    """
    found: set[str] = set()
    for line in body.splitlines():
        if _FORWARD_RE.match(line):
            break
        m = _REPLY_RE.match(line)
        if m:
            addr = m.group(1).strip("<>").lower()
            if not valid_address(addr):
                return None
            found.add(addr)
    return found.pop() if len(found) == 1 else None


def sender_address(msg) -> str:
    addrs = getaddresses([str(msg.get("From", ""))])
    addr = addrs[0][1].strip().lower() if addrs else ""
    return addr if valid_address(addr) else ""


SENDER_NAME = os.environ.get("AGENT_NAME") or "Monika – asystentka kancelarii"

TRUSTED_AUTHSERV_ID = "mx.google.com"
_COMMENT_RE = re.compile(r"\([^()]*\)")
_HEADER_FROM_RE = re.compile(r"(?:^|\s)header\.from\s*=\s*([^\s;]+)", re.IGNORECASE)
_DMARC_PASS_RE = re.compile(r"^\s*dmarc\s*=\s*pass(?:\s|$)", re.IGNORECASE)


_SPF_PASS_RE = re.compile(r"^\s*spf\s*=\s*pass(?:\s|$)", re.IGNORECASE)
_DKIM_PASS_RE = re.compile(r"^\s*dkim\s*=\s*pass(?:\s|$)", re.IGNORECASE)
_DMARC_ANY_RE = re.compile(r"^\s*dmarc\s*=\s*(\w+)", re.IGNORECASE)
_MAILFROM_RE = re.compile(r"(?:^|\s)smtp\.mailfrom\s*=\s*([^\s;]+)", re.IGNORECASE)
_DKIM_ID_RE = re.compile(r"(?:^|\s)header\.(?:i|d)\s*=\s*@?([^\s;]+)", re.IGNORECASE)


def _domain(value: str) -> str:
    return value.strip().strip("<>").rpartition("@")[2].lower()


def authenticated(msg, owner: str) -> bool:
    """Czy serwer Google potwierdził, że mail naprawdę pochodzi z domeny właściciela (fail closed).

    Nadawca może dopisać własny Authentication-Results, ale Gmail zawsze dokłada swój nagłówek
    NA GÓRZE. Liczy się wyłącznie pierwszy nagłówek, i tylko z authserv-id mx.google.com.
    Wystarczy jeden z wyników wyrównanych z domeną nadawcy:
    - dmarc=pass z header.from = domena nadawcy,
    - dkim=pass podpisany domeną nadawcy (Workspace: Admin → Gmail → Uwierzytelnianie e-maili).
    Samo spf=pass NIE wystarcza (inaczej niż u agenta Brandwise): SPF dotyczy koperty, nie nagłówka From,
    a współdzielone serwery pocztowe pozwalałyby podszyć się pod adres zespołu.
    Jawne dmarc=fail/quarantine/reject zawsze odrzuca.
    """
    results = msg.get_all("Authentication-Results") or []
    if not results:
        return False
    owner_domain = owner.rpartition("@")[2].strip().lower()
    if not owner_domain:
        return False
    # Komentarze w nawiasach nie mogą udawać wyników (np. "dmarc=fail (dmarc=pass)").
    parts = _COMMENT_RE.sub(" ", str(results[0])).split(";")
    authserv = parts[0].split()
    if not authserv or authserv[0].lower() != TRUSTED_AUTHSERV_ID:
        return False
    aligned = False
    for resinfo in parts[1:]:
        dm = _DMARC_ANY_RE.match(resinfo)
        if dm:
            if dm.group(1).lower() != "pass":
                return False
            m = _HEADER_FROM_RE.search(resinfo)
            if m and m.group(1).strip().lower() == owner_domain:
                aligned = True
        elif _DKIM_PASS_RE.match(resinfo):
            m = _DKIM_ID_RE.search(resinfo)
            if m and _domain("@" + m.group(1)) == owner_domain:
                aligned = True
    return aligned


def _plain_body(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, UnicodeDecodeError):
        return ""
    return text[:MAX_BODY_CHARS]


_MSGID_RE = re.compile(r"<[^<>\s]{1,250}>")


def _msgids(value) -> list[str]:
    return _MSGID_RE.findall(str(value or ""))


def _doc_files(msg) -> tuple[tuple[str, bytes], ...]:
    """Załączniki-dokumenty (typy z DOC_EXTS, do 25 MB, maks. 30) – tylko dla maili od zespołu."""
    out = []
    for part in msg.iter_attachments():
        name = (part.get_filename() or "").strip()
        if not name or Path(name).suffix.lower() not in DOC_EXTS:
            continue
        data = part.get_payload(decode=True) or b""
        if 0 < len(data) <= MAX_FILE_BYTES:
            out.append((name[:120], data))
        if len(out) >= MAX_FILES:
            break
    return tuple(out)


def _recipients(msg, header: str) -> tuple[str, ...]:
    """Adresaci z nagłówka (maks. 30) jako „Imię <adres>” albo sam adres."""
    out = []
    for name, addr in getaddresses([str(v) for v in (msg.get_all(header) or [])]):
        addr = addr.strip().lower()
        if valid_address(addr):
            out.append(formataddr((" ".join(name.split())[:120], addr)) if name.strip() else addr)
    return tuple(out[:30])


def parse_message(raw: bytes, team, fallback_id: str) -> IncomingMail:
    msg = message_from_bytes(raw, policy=policy.default)
    sender = sender_address(msg)
    team = (team,) if isinstance(team, str) else tuple(team)
    is_owner = sender in {t.lower() for t in team} and authenticated(msg, sender)
    body = _plain_body(msg)
    attachments = tuple(
        (part.get_filename() or "bez-nazwy")[:120] for part in msg.iter_attachments()
    )
    data = None
    subject_text = " ".join(str(msg.get("Subject", "")).split())
    if is_owner:
        for part in msg.iter_attachments():
            fname = (part.get_filename() or "").lower()
            # Import pamięci: załącznik pamiec*.json albo dowolny .json w mailu „import pamięci”.
            if fname.endswith(".json") and (fname.startswith("pamiec") or subject_text.lower() == "import pamięci"):
                raw_json = part.get_payload(decode=True) or b""
                if len(raw_json) <= 5 * 1024 * 1024:
                    data = raw_json.decode("utf-8", "replace")
                break
    headers = ""
    if not sender:   # nadawcy nie da się odczytać – surowe nagłówki pomogą ustalić, co to było
        head = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
        headers = head.decode("utf-8", "replace")[:1500]
    return IncomingMail(
        headers=headers,
        date=str(msg.get("Date") or "")[:100] or None,
        data=data,
        in_reply_to=next(iter(_msgids(msg.get("In-Reply-To"))), None),
        references=tuple(_msgids(msg.get("References"))[-20:]),
        message_id=str(msg.get("Message-ID") or fallback_id).strip(),
        sender=sender,
        subject=" ".join(str(msg.get("Subject", "")).split())[:300],
        body=body,
        trusted=is_owner,
        reply_to=extract_client_reply_address(body) if is_owner else None,
        attachments=attachments,
        files=_doc_files(msg) if is_owner else (),
        to=_recipients(msg, "To"),
        cc=_recipients(msg, "Cc"),
    )


# ---------- bramka ----------

class GmailMail(MailGateway):
    def __init__(self, settings: GmailSettings, dry_run: bool = True):
        self.s = settings
        self.dry_run = dry_run  # fail-safe: domyślnie szkic zamiast wysyłki
        self._tokens = None
        self._sig: tuple[str, float] = ("", 0.0)  # (stopka HTML, kiedy pobrana)
        if settings.uses_oauth:
            from .oauth import TokenSource
            self._tokens = TokenSource(settings.client_id, settings.client_secret, settings.refresh_token)

    # -- logowanie (XOAUTH2 albo hasło aplikacji) --
    def _xoauth2(self) -> bytes:
        return f"user={self.s.address}\x01auth=Bearer {self._tokens.access_token()}\x01\x01".encode()

    def _smtp_login(self, smtp: smtplib.SMTP) -> None:
        if self._tokens is None:
            smtp.login(self.s.address, self.s.password)
            return
        smtp.ehlo()
        code, resp = smtp.docmd("AUTH", "XOAUTH2 " + base64.b64encode(self._xoauth2()).decode())
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, resp)

    # -- IMAP --
    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.s.imap_host, timeout=TIMEOUT, ssl_context=ssl.create_default_context())
        if self._tokens is None:
            conn.login(self.s.address, self.s.password)
        else:
            payload = self._xoauth2()
            conn.authenticate("XOAUTH2", lambda _: payload)
        return conn

    def fetch_unread(self, skip=None) -> list[IncomingMail]:
        """Nieprzeczytane z INBOX (BODY.PEEK – nie zmienia flag).

        Skrzynka zostaje nieprzeczytana, więc te same maile wracają przy każdym sprawdzeniu poczty. Żeby nie ściągać
        co 30 s całych wiadomości z załącznikami (Gmail blokuje wtedy IMAP: „OVERQUOTA … bandwidth limits”), najpierw
        pobieramy sam nagłówek Message-ID; treść tylko wtedy, gdy skip(message_id) mówi, że to nowa wiadomość.
        """
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            # UID zamiast numerów kolejnych: numery przesuwają się, gdy ktoś usuwa/przenosi maile w skrzynce
            # między wyszukaniem a pobraniem (wtedy serwer potrafi zwrócić pustą wiadomość).
            typ, data = conn.uid("SEARCH", None, "UNSEEN")
            if typ != "OK" or not data or not data[0]:
                return []
            ids = data[0].split()[-MAX_FETCH:]
            mails, self.unreadable = [], []
            for uid in ids:
                if skip is not None and skip(self._message_id(conn, uid) or f"imap-uid-{uid.decode()}"):
                    continue
                typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = parts[0][1] if typ == "OK" and parts and isinstance(parts[0], tuple) else b""
                mail = parse_message(raw, self.s.team, fallback_id=f"imap-uid-{uid.decode()}") if raw else None
                if mail is None or (not mail.sender and not mail.subject and not mail.body.strip()):
                    self.unreadable.append(f"uid {uid.decode()}, {len(raw or b'')} B")   # nie zgłaszamy pustki
                    continue
                mails.append(mail)
            return mails
        finally:
            _quiet_logout(conn)

    @staticmethod
    def _message_id(conn: imaplib.IMAP4_SSL, uid: bytes) -> str:
        """Sam nagłówek Message-ID (kilkadziesiąt bajtów zamiast całej wiadomości)."""
        typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if typ != "OK" or not parts or not isinstance(parts[0], tuple):
            return ""
        head = message_from_bytes(parts[0][1] or b"", policy=policy.default)
        return str(head.get("Message-ID") or "").strip()

    def drafts_folder(self, conn: imaplib.IMAP4_SSL) -> str:
        typ, folders = conn.list()
        names = []
        for raw in folders or []:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            m = re.match(r'\((?P<attrs>[^)]*)\)\s+"(?P<sep>[^"]*)"\s+(?P<name>.+)$', line)
            if not m:
                continue
            name = m.group("name").strip().strip('"')
            names.append(name)
            if "\\drafts" in m.group("attrs").lower():
                return name
        for candidate in _DRAFTS_FALLBACK:
            if candidate in names:
                return candidate
        raise MailConfigError("nie znaleziono folderu Wersje robocze")

    # -- stopka --
    def signature_html(self) -> str:
        """Stopka z Gmaila (Ustawienia → Podpis). Gmail dokleja ją tylko w przeglądarce, a Monika wysyła przez SMTP –
        więc pobieramy ją z Gmail API (zakres https://mail.google.com/) i doklejamy sami. Błąd = poprzednia stopka albo bez."""
        if self._tokens is None:
            return ""
        sig, at = self._sig
        if time.time() - at < SIGNATURE_TTL_S:
            return sig
        try:
            url = f"https://gmail.googleapis.com/gmail/v1/users/me/settings/sendAs/{urllib.parse.quote(self.s.address)}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._tokens.access_token()}"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                sig = str(json.loads(r.read().decode()).get("signature") or "")
            self._sig = (sig, time.time())
        except Exception:
            self._sig = (sig, time.time() - SIGNATURE_TTL_S + SIGNATURE_RETRY_S)
        return sig

    # -- budowa wiadomości --
    def _build(self, to: list[str], subject: str, body: str, cc: list[str] | None,
               attachments: list[str] | None = None, in_reply_to: str | None = None,
               references: list[str] | None = None) -> EmailMessage:
        for addr in [*to, *(cc or [])]:
            if not valid_address(addr.strip()):
                raise ValueError(f"niepoprawny adres odbiorcy: {addr!r}")
        msg = EmailMessage()
        # Nazwa wyświetlana nadawcy: „Monika – asystentka kancelarii <adres>” (AGENT_NAME w .env zmienia).
        msg["From"] = formataddr((SENDER_NAME, self.s.address))
        msg["To"] = ", ".join(a.strip() for a in to)
        if cc:
            msg["Cc"] = ", ".join(a.strip() for a in cc)
        msg["Subject"] = " ".join(subject.split())
        msg["Message-ID"] = make_msgid(domain=self.s.address.split("@")[-1])
        thread = [r for r in (references or []) if _MSGID_RE.fullmatch(r)]
        if in_reply_to and _MSGID_RE.fullmatch(in_reply_to):
            msg["In-Reply-To"] = in_reply_to
            if in_reply_to not in thread:
                thread.append(in_reply_to)
        if thread:
            msg["References"] = " ".join(thread[-20:])
        sig = self.signature_html()
        if sig:   # tekst + HTML ze stopką z Gmaila (logo, imię, funkcja)
            msg.set_content(body.rstrip() + "\n\n-- \n" + _sig_text(sig) + "\n")
            msg.add_alternative(f'<div dir="ltr" style="font-family:Arial,Helvetica,sans-serif;font-size:14px">{_body_html(body.rstrip())}'
                                f'<br><br><div class="gmail_signature">{sig}</div></div>', subtype="html")
        else:
            msg.set_content(body)
        for path in attachments or []:
            p = Path(path)
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
        return msg

    def send(self, to, subject, body, cc=None, attachments=None, in_reply_to=None, references=None) -> str:
        msg = self._build(to, subject, body, cc, attachments, in_reply_to, references)
        if self.dry_run:
            return self._save_draft(msg)
        recipients = [*to, *(cc or [])]
        with smtplib.SMTP_SSL(self.s.smtp_host, 465, timeout=TIMEOUT, context=ssl.create_default_context()) as smtp:
            self._smtp_login(smtp)
            smtp.send_message(msg, to_addrs=recipients)
        return msg["Message-ID"]

    def _save_draft(self, msg: EmailMessage) -> str:
        conn = self._imap()
        try:
            folder = self.drafts_folder(conn)
            typ, _ = conn.append(f'"{folder}"', "\\Draft", imaplib.Time2Internaldate(time.time()), bytes(msg))
            if typ != "OK":
                raise RuntimeError("zapis szkicu nie powiódł się")
        finally:
            _quiet_logout(conn)
        return f"draft:{msg['Message-ID']}"

    # -- diagnostyka (nic nie wysyła ani nie zmienia) --
    def check_connection(self) -> list[str]:
        report = []
        conn = self._imap()
        try:
            typ, data = conn.select("INBOX", readonly=True)
            report.append(f"IMAP {self.s.imap_host}: logowanie OK, INBOX: {data[0].decode()} wiadomości")
            report.append(f"folder szkiców: {self.drafts_folder(conn)}")
        finally:
            _quiet_logout(conn)
        with smtplib.SMTP_SSL(self.s.smtp_host, 465, timeout=TIMEOUT, context=ssl.create_default_context()) as smtp:
            self._smtp_login(smtp)
            report.append(f"SMTP {self.s.smtp_host}: logowanie OK (nic nie wysłano)")
        report.append("metoda logowania: " + ("OAuth (XOAUTH2)" if self._tokens else "hasło aplikacji"))
        return report


def _quiet_logout(conn) -> None:
    try:
        conn.logout()
    except Exception:
        pass


def build_gmail(dry_run: bool, env_file: Path | None = None, team: tuple[str, ...] = ()) -> Integrations:
    """Gmail prawdziwy; tablica zadań i faktury – mocki (kancelaria ich nie używa)."""
    from .mock import MockBoard, MockInvoices

    from .panel import build_panel

    mail = GmailMail(GmailSettings.from_env(env_file, team), dry_run=dry_run)
    env = {**read_env_file(env_file), **os.environ} if env_file else dict(os.environ)
    return Integrations(mail=mail, board=MockBoard(), invoices=MockInvoices(), name="gmail", panel=build_panel(env))
