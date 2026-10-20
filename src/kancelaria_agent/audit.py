"""Log audytowy: jeden obiekt JSON na linię (JSONL), z rotacją."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any



# Tokeny w linkach jednorazowych (logowanie do panelu, dane logowania WP) nie trafiają do logu.
_SECRET_LINK_RE = re.compile(r"(/(?:auth|k)/)[A-Za-z0-9_-]{20,}")

class AuditLog:
    def __init__(self, path: Path, max_bytes: int = 5_000_000, backup_count: int = 10):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Osobny logger na instancję/ścieżkę, bez propagacji do root loggera.
        self._logger = logging.getLogger(f"kancelaria_agent.audit.{path.resolve()}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def record(self, event: str, **details: Any) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **details}
        line = json.dumps(entry, ensure_ascii=False, default=str, sort_keys=True)
        self._logger.info(_SECRET_LINK_RE.sub(r"\1[ukryte]", line))

    def close(self) -> None:
        for h in list(self._logger.handlers):
            h.close()
            self._logger.removeHandler(h)
