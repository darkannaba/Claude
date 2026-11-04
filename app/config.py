"""Configuration technique (chemins, base de données, clé de session).

Les paramètres *métier* (nom de l'hôtel, devise, taxe de séjour, TVA...) ne sont
PAS ici : ils sont stockés en base et éditables depuis la page Administration.
"""
import os
import secrets
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def _load_dotenv() -> None:
    """Charge un fichier .env minimal (sans dépendance externe)."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DB = DATA_DIR / "hotel.db"
TEMPLATE_DB = DATA_DIR / "hotel_template.db"  # structure + 30 chambres pré-remplies (sans comptes)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DB}")

# Premier lancement : on part de la base modèle fournie avec le projet.
if "DATABASE_URL" not in os.environ and not DEFAULT_DB.exists() and TEMPLATE_DB.exists():
    shutil.copyfile(TEMPLATE_DB, DEFAULT_DB)


def get_secret_key() -> str:
    """Clé de signature des cookies de session.

    Ordre : variable SECRET_KEY, sinon fichier data/.secret_key (créé une fois).
    """
    if os.getenv("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    key_file = DATA_DIR / ".secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return key_file.read_text(encoding="utf-8").strip()
