#!/usr/bin/env bash
# Raport stanu serwera (uruchamiany jako root przez apply-update.sh po każdej aktualizacji).
# Wynik trafia mailem do autora projektu (deploy/ops-report.py) – kanał serwisowy bez SSH.
# Nie wypisuje sekretów: z .env tylko nazwy zmiennych i prefiks klucza API.
DIR=/home/ubuntu/kancelaria-agent
cd "$DIR" || exit 0
sec() { echo; echo "===== $* ====="; }
sec "czas, wersja kodu"
date -u; uptime; git -c safe.directory="$DIR" log -1 --format='%h %ci %s' 2>&1
sec "usługi"
systemctl is-enabled monika monika-update.timer 2>&1
systemctl --no-pager status monika 2>&1 | head -n 15
systemctl --no-pager list-timers monika-update.timer 2>&1 | head -n 4
sec "journal monika (ostatnie 80)"
journalctl -u monika -n 80 --no-pager 2>&1 | cut -c1-400
sec "journal monika-update (ostatnie 15)"
journalctl -u monika-update -n 15 --no-pager 2>&1 | cut -c1-300
sec ".env – tylko nazwy i prefiks klucza"
if [ -f .env ]; then
  sed -n 's/^\([A-Z_]*\)=.*/\1/p' .env | tr '\n' ' '; echo
  k=$(sed -n 's/^ANTHROPIC_API_KEY=//p' .env | tr -d '"'"'"' \r'); echo "ANTHROPIC_API_KEY: prefiks ${k:0:12}… długość ${#k}"
else echo "BRAK .env"; fi
sec "audit.jsonl – ważne zdarzenia (ostatnie 30)"
grep -E '"event": ?"(start|stop|halt|task_failed|agent_error|action_failed|mail_blocked|mail_received|mail_unreadable|ai_cost|task_done)"' logs/audit.jsonl 2>/dev/null | tail -n 30 | cut -c1-400
sec "diagnostyka Moniki"
sudo -u ubuntu .venv/bin/python - <<'PY' 2>&1 | tail -n 40
import sys; sys.path.insert(0, "src")
from pathlib import Path
from kancelaria_agent.config import load_config
from kancelaria_agent.store import Store
from kancelaria_agent.diagnostics import report
cfg = load_config(Path("config.yaml").resolve()); print(report(cfg, Store(cfg.database)))
PY
sec "SSH: klucze w authorized_keys (tylko komentarze)"
awk '{print $1, $NF}' /home/ubuntu/.ssh/authorized_keys 2>&1
sec "pliki STOP / dane"
ls -la data 2>&1 | head -n 20; for f in STOP data/STOP; do [ -f "$f" ] && echo "UWAGA: plik $f istnieje"; done
exit 0
