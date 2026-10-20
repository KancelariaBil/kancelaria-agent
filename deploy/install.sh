#!/usr/bin/env bash
# Instaluje usługę Moniki i automatyczne aktualizacje (wymaga sudo). Uruchamia człowiek.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data logs
chmod +x deploy/update.sh deploy/apply-update.sh
# Narzędzia dokumentów: podgląd skanów PDF (poppler) i konwersja .doc/.odt/.rtf (LibreOffice bez GUI).
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q poppler-utils libreoffice-writer-nogui libreoffice-calc-nogui
sudo install -m 0644 deploy/monika.service /etc/systemd/system/monika.service
sudo install -m 0644 deploy/monika-update.service /etc/systemd/system/monika-update.service
sudo install -m 0644 deploy/monika-update.timer /etc/systemd/system/monika-update.timer
sudo systemctl daemon-reload
sudo systemctl reset-failed monika 2>/dev/null || true
sudo systemctl enable monika.service monika-update.timer
sudo systemctl restart monika.service
sudo systemctl start monika-update.timer
systemctl --no-pager status monika.service || true
