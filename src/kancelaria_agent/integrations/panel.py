"""Panel kancelarii (panel.kancelariabil.pl) – działa na tym samym serwerze, wywołanie tylko lokalne.

Jedyne wywołanie: wpisanie oferty z maila w DW (POST /api/monika/oferta). Klucz w .env (PANEL_API_KEY),
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
        req = urllib.request.Request(
            f"{self.base_url}/api/monika/oferta", data=json.dumps(body).encode(), method="POST",
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
            raise PanelError(f"panel odrzucił ofertę: {data.get('error', '')}")
        return str(data.get("id", ""))


def build_panel(env: dict[str, str] | None = None) -> HttpPanel:
    env = env if env is not None else dict(os.environ)
    return HttpPanel(env.get("PANEL_API_KEY", "").strip(), env.get("PANEL_URL") or DEFAULT_URL)
