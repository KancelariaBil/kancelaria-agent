"""Google OAuth dla skrzynki agenta: odświeżanie tokenu i jednorazowa autoryzacja.

Autoryzacja (`python -m kancelaria_agent oauth-setup`) działa na serwerze, a przeglądarka na
komputerze właściciela łączy się z nią przez tunel SSH (`ssh -L 8765:127.0.0.1:8765 ...`).
Właściciel otwiera krótki adres http://localhost:8765 – bez przeklejania długich linków.

Bezpieczeństwo:
- serwer słucha wyłącznie na 127.0.0.1 (z zewnątrz niedostępny), kończy się po jednej autoryzacji
  albo po 10 minutach;
- PKCE + losowy `state`;
- token jest zapisywany TYLKO, gdy Google potwierdzi, że zalogowano się na AGENT_EMAIL
  (chroni przed przypadkowym oddaniem agentowi dostępu do skrzynki właściciela);
- sekrety nigdy nie są wypisywane.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://mail.google.com/ openid email"
SETUP_PORT = 8765
SETUP_TIMEOUT_S = 600
HTTP_TIMEOUT_S = 30


class OAuthError(Exception):
    pass


def _post(url: str, data: dict[str, str]) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", e.code)
        except Exception:
            detail = e.code
        raise OAuthError(f"Google odrzucił żądanie tokenu: {detail}") from None


class TokenSource:
    """Krótkotrwały access token z refresh tokenu, odświeżany z zapasem 60 s."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._cid, self._secret, self._refresh = client_id, client_secret, refresh_token
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "TokenSource(<ukryte>)"

    def access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            data = _post(TOKEN_URL, {
                "client_id": self._cid, "client_secret": self._secret,
                "refresh_token": self._refresh, "grant_type": "refresh_token",
            })
            if "access_token" not in data:
                raise OAuthError("brak access_token w odpowiedzi Google")
            self._token = data["access_token"]
            self._expires = time.time() + float(data.get("expires_in", 3600))
            return self._token


def id_token_email(id_token: str) -> str:
    """E-mail z id_tokenu. Token pochodzi wprost z endpointu Google przez TLS, więc nie weryfikujemy podpisu."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception as e:
        raise OAuthError("nieczytelny id_token") from e
    if not claims.get("email_verified", False):
        raise OAuthError("Google nie potwierdził adresu e-mail konta")
    return str(claims.get("email", "")).lower()


def set_env_value(env_file: Path, key: str, value: str) -> None:
    """Ustawia KEY=value w .env (zastępuje istniejący wpis), uprawnienia 600."""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_file, 0o600)


def run_setup(client_id: str, client_secret: str, agent_email: str, env_file: Path,
              port: int = SETUP_PORT, timeout_s: int = SETUP_TIMEOUT_S) -> str:
    """Jednorazowa autoryzacja. Zwraca adres konta, dla którego zapisano token."""
    redirect_uri = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth_link = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent select_account",
        "login_hint": agent_email, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # bez logowania adresów z kodami
            pass

        def _reply(self, code: int, text: str) -> None:
            body = f"<!doctype html><meta charset=utf-8><body style='font:16px sans-serif;padding:2em'>{text}"
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                self.send_response(302)
                self.send_header("Location", auth_link)
                self.end_headers()
                return
            if url.path != "/callback":
                self._reply(404, "Nie ma takiej strony.")
                return
            q = urllib.parse.parse_qs(url.query)
            if q.get("state", [""])[0] != state:
                self._reply(400, "Niepoprawny parametr state. Otwórz ponownie http://localhost:%d" % port)
                return
            if "error" in q:
                result["error"] = q["error"][0]
                self._reply(400, f"Autoryzacja przerwana: {q['error'][0]}")
                done.set()
                return
            try:
                data = _post(TOKEN_URL, {
                    "client_id": client_id, "client_secret": client_secret,
                    "code": q.get("code", [""])[0], "code_verifier": verifier,
                    "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                })
                email = id_token_email(data.get("id_token", ""))
                if email != agent_email.lower():
                    raise OAuthError(f"zalogowano jako {email}, a wymagane jest {agent_email}. "
                                     "Token NIE został zapisany")
                if not data.get("refresh_token"):
                    raise OAuthError("Google nie zwrócił refresh tokenu")
                set_env_value(env_file, "GMAIL_REFRESH_TOKEN", data["refresh_token"])
                result["email"] = email
                self._reply(200, f"Gotowe. Agent ma dostęp do skrzynki <b>{email}</b>. Możesz zamknąć to okno.")
            except OAuthError as e:
                result["error"] = str(e)
                self._reply(400, f"Błąd: {e}")
            done.set()

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if not done.wait(timeout_s):
            raise OAuthError("minął czas na autoryzację (10 minut)")
    finally:
        server.shutdown()
        server.server_close()
    if "error" in result:
        raise OAuthError(result["error"])
    return result["email"]
