"""Panel kancelarii (panel.kancelariabil.pl) – działa na tym samym serwerze, wywołanie tylko lokalne.

Wywołania: oferta z maila w DW (POST /api/monika/oferta), kopia maila do Skrzynki DW (POST /api/monika/dw)
i powiadomienia do wysłania przez Monikę (GET/POST /api/monika/powiadomienia, np. „klient przesłał pliki”). Klucz w .env (PANEL_API_KEY),
adres stały – lokalny port panelu (PANEL_URL może go zmienić, ale wyłącznie na 127.0.0.1/localhost).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_URL = "http://127.0.0.1:3000"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


class PanelError(Exception):
    pass


class PanelGateway(ABC):
    @abstractmethod
    def add_offer(self, offer: dict[str, Any], file_path: Path | None) -> str:
        """Wpisuje ofertę; zwraca id oferty w panelu."""

    @abstractmethod
    def add_dw(self, mail: dict[str, Any], files: list[Path]) -> str:
        """Kopia maila z Moniką w DW do Skrzynki DW; zwraca id wpisu w panelu."""

    def pending_notifications(self) -> list[dict[str, Any]]:
        """Powiadomienia wewnętrzne z panelu do wysłania przez Monikę: [{id, to: [..], cc: [..], subject, body}]."""
        return []

    def ack_notification(self, notification_id: str, ok: bool, error: str = "") -> None:
        """Wynik wysyłki – błąd oddaje powiadomienie panelowi (wyśle je sam)."""


def panel_base_url(value: str | None) -> str:
    url = (value or DEFAULT_URL).rstrip("/")
    u = urlparse(url)
    if u.scheme != "http" or u.hostname not in LOCAL_HOSTS:
        raise PanelError("PANEL_URL musi wskazywać lokalny panel (http://127.0.0.1:<port>)")
    return url


class HttpPanel(PanelGateway):
    def __init__(self, key: str, base_url: str = DEFAULT_URL, timeout: float = 30):
        self._key = key
        self.base_url = panel_base_url(base_url)
        self.timeout = timeout

    def __repr__(self) -> str:  # klucz nigdy w logach
        return f"HttpPanel({self.base_url})"

    def add_offer(self, offer: dict[str, Any], file_path: Path | None) -> str:
        if len(self._key) < 32:
            raise PanelError("brak PANEL_API_KEY w .env")
        body = {
            "messageId": offer["message_id"], "from": offer.get("from"), "to": offer.get("to") or [],
            "subject": offer.get("subject"), "date": offer.get("date"), "body": offer.get("body"),
            "clientName": offer.get("client_name"), "clientEmail": offer.get("client_email"),
            "title": offer.get("title"), "amount": offer.get("amount"), "form": offer.get("form"),
            "validUntil": offer.get("valid_until"),
        }
        if file_path is not None:
            body["attachment"] = {"name": file_path.name, "base64": base64.b64encode(file_path.read_bytes()).decode()}
        return self._post("/api/monika/oferta", body, "ofertę")

    def add_dw(self, mail: dict[str, Any], files: list[Path]) -> str:
        if len(self._key) < 32:
            raise PanelError("brak PANEL_API_KEY w .env")
        body = {
            "messageId": mail["message_id"], "from": mail.get("from"), "to": mail.get("to") or [], "cc": mail.get("cc") or [],
            "subject": mail.get("subject"), "date": mail.get("date"), "body": mail.get("body"),
            "attachments": [{"name": f.name, "base64": base64.b64encode(f.read_bytes()).decode()} for f in files],
        }
        return self._post("/api/monika/dw", body, "mail z DW")

    def pending_notifications(self) -> list[dict[str, Any]]:
        if len(self._key) < 32:
            return []
        data = self._request("GET", "/api/monika/powiadomienia", None, "powiadomienia")
        items = data.get("items") or []
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)]

    def ack_notification(self, notification_id: str, ok: bool, error: str = "") -> None:
        self._request("POST", "/api/monika/powiadomienia", {"id": notification_id, "ok": ok, "error": error[:300]}, "wynik powiadomienia")

    def _post(self, path: str, body: dict[str, Any], what: str) -> str:
        return str(self._request("POST", path, body, what).get("id", ""))

    def _request(self, method: str, path: str, body: dict[str, Any] | None, what: str) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=None if body is None else json.dumps(body).encode(), method=method,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:   # noqa: S310 – adres lokalny, sprawdzony
                data = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read().decode() or "{}")
            except ValueError:
                data = {}
            raise PanelError(f"panel odpowiedział {e.code}: {data.get('error', '')}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise PanelError(f"panel niedostępny: {e}") from None
        if not data.get("ok"):
            raise PanelError(f"panel odrzucił {what}: {data.get('error', '')}")
        return data


def build_panel(env: dict[str, str] | None = None) -> HttpPanel:
    env = env if env is not None else dict(os.environ)
    return HttpPanel(env.get("PANEL_API_KEY", "").strip(), env.get("PANEL_URL") or DEFAULT_URL)
