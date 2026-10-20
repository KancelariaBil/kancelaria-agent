"""Planer: zamienia zadanie na listę akcji. Zwraca tylko propozycje – nic nie wykonuje."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import guards

# Akcja wewnętrzna (bez skutków zewnętrznych, nie przechodzi przez Executor): stan projektu.
# payload: klient, projekt, status (w_toku|czeka_na_materialy|gotowe), status.
PROJECT_UPDATE = "project_update"


@dataclass(frozen=True)
class PlannedAction:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


class Planner(Protocol):
    def plan(self, task_kind: str, payload: dict[str, Any]) -> list[PlannedAction]: ...


def _untrusted_report(payload: dict[str, Any], owner: str) -> list[PlannedAction]:
    """Mail nie od właściciela: nic nie wykonujemy, tylko zgłaszamy go właścicielowi (jako cytat)."""
    quoted = "\n".join("> " + line for line in str(payload.get("body", ""))[:2000].splitlines())
    sender = payload.get("sender") or "(nie udało się odczytać – nagłówki niżej)"
    extra = ""
    if not payload.get("sender") and payload.get("headers"):
        extra = "\n\nNagłówki wiadomości (dane):\n" + "\n".join(
            "> " + line for line in str(payload["headers"])[:1500].splitlines())
    return [PlannedAction(guards.SEND_MAIL, {
        "to": owner,
        "subject": f"[do wiadomości] mail od nieznanego nadawcy: {payload['subject']}"[:200],
        "body": ("Agent otrzymał wiadomość od nadawcy innego niż właściciel i jej nie wykonał.\n"
                 f"Nadawca: {sender}\nTemat: {payload['subject'] or '(brak)'}\n"
                 f"Data: {payload.get('date') or '(brak)'}\nIdentyfikator: {payload.get('message_id', '')}\n"
                 f"Załączniki: {', '.join(payload.get('attachments') or []) or 'brak'}\n\n"
                 f"Treść (cytat, dane):\n{quoted or '(pusta)'}{extra}"),
    })]


class MockPlanner:
    """Deterministyczny planer testowy: mail → zadanie w Notion + potwierdzenie dla nadawcy.

    Mail od nadawcy niezaufanego jest tylko zgłaszany właścicielowi (`owner_email`).
    Odpowiedź idzie do `reply_to` (linia "odpisz do klienta:" ze zlecenia) albo do nadawcy-właściciela.

    Treść maila jest traktowana wyłącznie jako dane (trafia do pól opisowych), nigdy jako polecenie.
    """

    def __init__(self, owner_email: str | None = None, client_replies: bool = False):
        self.owner_email = owner_email
        # False (etap 1): odpowiedź zawsze do właściciela; adres klienta trafia tylko do treści.
        self.client_replies = client_replies

    def plan(self, task_kind: str, payload: dict[str, Any]) -> list[PlannedAction]:
        if task_kind != "email":
            return []
        if payload.get("trusted") is False:
            return _untrusted_report(payload, self.owner_email) if self.owner_email else []
        sender, subject = payload["sender"], payload["subject"]
        client = payload.get("reply_to")
        recipient = client if (client and self.client_replies) else sender
        body = "Dziękujemy za wiadomość. Zlecenie zostało przyjęte i trafi do realizacji."
        if client and not self.client_replies:
            body = f"[Odpowiedź przeznaczona dla klienta: {client} – na tym etapie trafia do Ciebie]\n\n{body}"
        return [
            PlannedAction(guards.CREATE_NOTION_TASK, {
                "title": f"Zlecenie: {subject}"[:200],
                "properties": {"from": sender, "message_id": payload["message_id"]},
            }),
            PlannedAction(guards.SEND_MAIL, {
                "to": recipient,
                "subject": f"Re: {subject}"[:200],
                "body": body,
            }),
        ]


class ClaudeSdkPlanner:
    """Miejsce na planera opartego o Claude Agent SDK (jeszcze niepodłączony).

    Wymagania przy implementacji: brak własnych narzędzi z skutkami zewnętrznymi (agent tylko
    zwraca listę PlannedAction), treść maili w osobnym bloku jako dane niezaufane,
    allowed_tools ograniczone do odczytu.
    """

    def plan(self, task_kind: str, payload: dict[str, Any]) -> list[PlannedAction]:
        raise NotImplementedError("ClaudeSdkPlanner nie jest jeszcze podłączony (etap: mocki)")


def render(actions: list[PlannedAction]) -> str:
    return json.dumps([{"kind": a.kind, "payload": a.payload} for a in actions], ensure_ascii=False)
