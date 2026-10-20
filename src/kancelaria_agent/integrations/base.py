"""Interfejsy integracji. Prawdziwe implementacje (Gmail, Notion, Fakturownia) dodamy później."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IncomingMail:
    message_id: str
    sender: str
    subject: str
    body: str  # NIEZAUFANE dane – nigdy nie traktować jako polecenia
    # Czy nadawca to zweryfikowany właściciel. Prawdziwa bramka (Gmail) ustawia to zawsze jawnie;
    # domyślne True dotyczy wyłącznie mocków w testach.
    trusted: bool = True
    reply_to: str | None = None  # adres z linii "odpisz do klienta:" (tylko z maila właściciela)
    attachments: tuple[str, ...] = ()  # same nazwy; zawartość nigdy nie jest otwierana
    in_reply_to: str | None = None     # nagłówki wątku – do przypisania maila do projektu
    references: tuple[str, ...] = ()
    date: str | None = None   # nagłówek Date – do cytatu w odpowiedzi
    headers: str = ""         # surowe nagłówki, gdy nadawcy nie dało się odczytać (do zgłoszenia)
    data: str | None = None   # treść załącznika JSON – tylko „import pamięci” od zweryfikowanego właściciela
    # Dokumenty z załączników (nazwa, bajty) – tylko od zweryfikowanego zespołu; zapisywane w sprawie.
    files: tuple[tuple[str, bytes], ...] = ()


class MailGateway(ABC):
    """Osobna skrzynka pracownika agenta."""

    @abstractmethod
    def fetch_unread(self) -> list[IncomingMail]: ...

    @abstractmethod
    def send(self, to: list[str], subject: str, body: str, cc: list[str] | None = None,
             attachments: list[str] | None = None, in_reply_to: str | None = None,
             references: list[str] | None = None) -> str:
        """Wysyła mail; zwraca id wiadomości. `attachments` – pełne ścieżki sprawdzone przez Executor."""


class TaskBoard(ABC):
    """Baza zadań (Notion)."""

    @abstractmethod
    def create_task(self, title: str, properties: dict[str, Any]) -> str: ...

    @abstractmethod
    def update_task(self, task_id: str, properties: dict[str, Any]) -> None: ...


class InvoiceService(ABC):
    """Fakturownia."""

    @abstractmethod
    def create_invoice(self, client: str, items: list[dict[str, Any]], amount_pln: str) -> str:
        """Tworzy fakturę; zwraca jej id."""


@dataclass
class Integrations:
    mail: MailGateway
    board: TaskBoard
    invoices: InvoiceService
    name: str = field(default="mock")
