"""Jedyna droga do skutków zewnętrznych. Każda akcja przechodzi: bezpieczniki → (propozycja | wykonanie)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import guards
from .audit import AuditLog
from .config import Config
from .guards import Verdict
from .integrations import Integrations
from .store import Store


@dataclass(frozen=True)
class Outcome:
    status: str  # denied | proposed | executed | failed
    reason: str
    proposal_id: int | None = None
    result: str | None = None


class Executor:
    def __init__(self, cfg: Config, store: Store, audit: AuditLog, integrations: Integrations):
        self.cfg = cfg
        self.store = store
        self.audit = audit
        self.integrations = integrations

    def submit(self, kind: str, payload: dict[str, Any], task_id: int | None = None) -> Outcome:
        """Wywoływane przez agenta. W trybie próbnym niczego nie wykonuje."""
        decision = guards.evaluate(self.cfg, kind, payload)
        self.audit.record(
            "decision", task_id=task_id, action=kind, payload=payload,
            verdict=decision.verdict.value, reason=decision.reason, dry_run=self.cfg.dry_run,
        )
        if decision.verdict is Verdict.DENY:
            return Outcome("denied", decision.reason)

        if self.cfg.dry_run or decision.verdict is Verdict.REQUIRE_APPROVAL:
            why = "tryb próbny" if self.cfg.dry_run else decision.reason
            pid = self.store.add_proposal(task_id, kind, payload, why)
            self.audit.record("proposal_created", proposal_id=pid, task_id=task_id, action=kind, reason=why)
            return Outcome("proposed", why, proposal_id=pid)

        return self._execute(kind, payload, task_id=task_id, proposal_id=None)

    def approve(self, proposal_id: int) -> Outcome:
        """Zatwierdzenie przez człowieka (CLI, TTY). Bezpieczniki sprawdzane ponownie."""
        row = self.store.get_proposal(proposal_id)
        if row is None:
            return Outcome("failed", f"brak propozycji {proposal_id}")
        kind, payload = row["action_kind"], json.loads(row["payload"])

        decision = guards.evaluate(self.cfg, kind, payload)
        # Człowiek akceptuje przekroczenie limitu kwoty; reszta bezpieczników pozostaje twarda.
        if decision.verdict is Verdict.DENY:
            self.store.set_proposal_status(proposal_id, "failed", f"zablokowane: {decision.reason}")
            self.audit.record("approval_blocked", proposal_id=proposal_id, action=kind, reason=decision.reason)
            return Outcome("denied", decision.reason, proposal_id=proposal_id)

        if not self.store.decide_proposal(proposal_id, "executing"):
            return Outcome("failed", f"propozycja {proposal_id} nie jest w stanie 'pending'")
        self.audit.record("proposal_approved", proposal_id=proposal_id, action=kind)
        return self._execute(kind, payload, task_id=row["task_id"], proposal_id=proposal_id)

    def reject(self, proposal_id: int) -> bool:
        ok = self.store.decide_proposal(proposal_id, "rejected")
        if ok:
            self.audit.record("proposal_rejected", proposal_id=proposal_id)
        return ok

    def _execute(self, kind: str, payload: dict[str, Any], task_id: int | None, proposal_id: int | None) -> Outcome:
        try:
            result = self._dispatch(kind, payload)
        except Exception as e:  # skutek zewnętrzny nie powiódł się
            msg = f"{type(e).__name__}: {e}"
            if proposal_id:
                self.store.set_proposal_status(proposal_id, "failed", msg)
            self.audit.record("action_failed", task_id=task_id, proposal_id=proposal_id, action=kind, error=msg)
            return Outcome("failed", msg, proposal_id=proposal_id)
        if proposal_id:
            self.store.set_proposal_status(proposal_id, "executed", result)
        self.audit.record("action_executed", task_id=task_id, proposal_id=proposal_id, action=kind, result=result)
        return Outcome("executed", "wykonano", proposal_id=proposal_id, result=result)

    def _dispatch(self, kind: str, p: dict[str, Any]) -> str:
        i = self.integrations
        if kind == guards.SEND_MAIL:
            to = [p["to"]] if isinstance(p["to"], str) else list(p["to"])
            files = [str(f) for f in guards.resolve_attachments(self.cfg, p.get("attachments"))]
            return i.mail.send(to, p.get("subject", ""), p.get("body", ""), cc=p.get("cc"), attachments=files,
                               in_reply_to=p.get("in_reply_to"), references=p.get("references"))
        if kind == guards.CREATE_NOTION_TASK:
            return i.board.create_task(p["title"], p.get("properties", {}))
        if kind == guards.UPDATE_NOTION_TASK:
            i.board.update_task(p["task_id"], p.get("properties", {}))
            return p["task_id"]
        if kind == guards.CREATE_INVOICE:
            return i.invoices.create_invoice(p["client"], p.get("items", []), str(p["amount_pln"]))
        raise ValueError(f"nieznana akcja: {kind}")
