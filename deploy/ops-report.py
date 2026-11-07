"""Wysyła raport serwisowy (stdin) mailem ze skrzynki Moniki do autora projektu.

Uruchamiany przez apply-update.sh (root) po każdej aktualizacji z main. Kanał serwisowy bez SSH:
adresat musi być w `team` w config.yaml (Monika nie pisze poza zespół). Domyślnie OPS_REPORT_TO
z .env albo lukasz@brandwise.pl. Błąd wysyłki nie przerywa aktualizacji.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kancelaria_agent.config import load_config  # noqa: E402
from kancelaria_agent.integrations.gmail import GmailMail, GmailSettings, read_env_file  # noqa: E402

SECRET_RE = re.compile(r"(sk-ant-[A-Za-z0-9]{3})[A-Za-z0-9_\-]{8,}|(1//[0-9A-Za-z])[0-9A-Za-z_\-]{20,}|(GOCSPX-)[0-9A-Za-z_\-]+")


def redact(text: str) -> str:
    return SECRET_RE.sub(lambda m: (m.group(1) or m.group(2) or m.group(3)) + "…[ukryte]", text)


def recipient(env: dict, team: tuple[str, ...]) -> str | None:
    to = (env.get("OPS_REPORT_TO") or "lukasz@brandwise.pl").strip().lower()
    return to if to in team else None


def main() -> int:
    body = redact(sys.stdin.read())[-60000:]
    cfg = load_config(ROOT / "config.yaml")
    env_file = ROOT / ".env"
    env = read_env_file(env_file) if env_file.exists() else {}
    to = recipient(env, tuple(t.lower() for t in cfg.team))
    if not to:
        print("ops-report: adresat spoza zespołu – nie wysyłam", file=sys.stderr)
        return 0
    mail = GmailMail(GmailSettings.from_env(env_file, team=tuple(cfg.team)), dry_run=False)
    mail.send([to], "[Monika serwis] raport po aktualizacji", body)
    print(f"ops-report: wysłano do {to}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 – raport nie może wywrócić aktualizacji
        print(f"ops-report: błąd {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)
