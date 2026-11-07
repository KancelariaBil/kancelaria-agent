#!/bin/sh
# Krok uprzywilejowany (root, przez prefiks "+" w monika-update.service). Działa tylko po aktualizacji.
# Restartuje agenta zawsze – także gdy stał po bezpieczniku "config zmieniony" (zmiana przyszła
# zatwierdzonym PR, więc jest oczekiwana).
set -eu
DIR=/home/ubuntu/kancelaria-agent
[ -f "$DIR/data/.updated" ] || exit 0
rm -f "$DIR/data/.updated"
install -m 0644 "$DIR/deploy/monika.service" /etc/systemd/system/monika.service
install -m 0644 "$DIR/deploy/monika-update.service" /etc/systemd/system/monika-update.service
install -m 0644 "$DIR/deploy/monika-update.timer" /etc/systemd/system/monika-update.timer
install -m 0644 "$DIR/deploy/monika-www.service" /etc/systemd/system/monika-www.service
install -d -o ubuntu -g ubuntu "$DIR/data/wiedza"
systemctl daemon-reload
systemctl reset-failed monika 2>/dev/null || true
systemctl restart monika
systemctl enable monika-www.service >/dev/null 2>&1 || true
systemctl restart monika-www.service || true
# Zapora: gdy ufw jest włączony, otwórz port panelu.
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 80/tcp >/dev/null || true; fi
# Raport serwisowy mailem do autora projektu (diagnoza bez SSH). Nie przerywa aktualizacji.
sleep 20
"$DIR/deploy/ops.sh" 2>&1 | "$DIR/.venv/bin/python" "$DIR/deploy/ops-report.py" || true
