#!/usr/bin/env sh
# Lancement du serveur sous Linux / macOS : http://localhost:8000
set -e
cd "$(dirname "$0")/.."
[ -f .venv/bin/activate ] || { echo "Lancez d'abord scripts/install.sh" >&2; exit 1; }
. .venv/bin/activate
exec python main.py
