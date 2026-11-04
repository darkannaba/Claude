#!/usr/bin/env sh
# Installation automatique sous Linux / macOS (Python 3.10+).
set -e
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
"$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || {
  echo "Python 3.10 ou plus récent est requis." >&2; exit 1; }
[ -d .venv ] || "$PYTHON" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
python scripts/seed_prod.py
echo "Installation terminée. Notez les mots de passe affichés, puis lancez : scripts/run.sh"
