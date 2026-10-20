#!/usr/bin/env bash
# Pobiera zatwierdzone zmiany z main (jako ubuntu). Gdy coś się zmieniło, zostawia znacznik
# data/.updated – krok uprzywilejowany w monika-update.service przeinstalowuje usługę i ją restartuje.
set -euo pipefail
cd "$(dirname "$0")/.."
git fetch -q origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  git checkout -q main
  git merge -q --ff-only origin/main
  .venv/bin/pip install -q -r requirements.txt
  mkdir -p data logs
  touch data/.updated
fi
