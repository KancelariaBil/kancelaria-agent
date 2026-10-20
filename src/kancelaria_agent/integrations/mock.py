"""Implementacje testowe w pamięci. Nie wykonują żadnych połączeń sieciowych."""

from __future__ import annotations

from typing import Any

from .base import IncomingMail, Integrations, InvoiceService, MailGateway, TaskBoard


class MockMail(MailGateway):
    def __init__(self, inbox: list[IncomingMail] | None = None):
        self.inbox = list(inbox or [])
        self.sent: list[dict[str, Any]] = []

    def fetch_unread(self) -> list[IncomingMail]:
        return list(self.inbox)

    def send(self, to, subject, body, cc=None, attachments=None, in_reply_to=None, references=None) -> str:
        self.sent.append({"to": to, "subject": subject, "body": body, "cc": cc or [],
                          "attachments": list(attachments or []), "in_reply_to": in_reply_to,
                          "references": list(references or [])})
        return f"mock-mail-{len(self.sent)}"


class MockBoard(TaskBoard):
    def __init__(self):
        self.tasks: dict[str, dict[str, Any]] = {}

    def create_task(self, title, properties) -> str:
        tid = f"mock-task-{len(self.tasks) + 1}"
        self.tasks[tid] = {"title": title, **properties}
        return tid

    def update_task(self, task_id, properties) -> None:
        self.tasks[task_id].update(properties)


class MockInvoices(InvoiceService):
    def __init__(self):
        self.invoices: list[dict[str, Any]] = []

    def create_invoice(self, client, items, amount_pln) -> str:
        self.invoices.append({"client": client, "items": items, "amount_pln": amount_pln})
        return f"mock-invoice-{len(self.invoices)}"


def build_mock(inbox: list[IncomingMail] | None = None) -> Integrations:
    return Integrations(mail=MockMail(inbox), board=MockBoard(), invoices=MockInvoices(), name="mock")
