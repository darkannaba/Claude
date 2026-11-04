import os
import tempfile

# Base SQLite jetable et clé de session fixe, définies AVANT l'import de l'application.
_tmp_dir = tempfile.mkdtemp(prefix="hotel-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test.db"
os.environ["SECRET_KEY"] = "test-secret-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.admin import User  # noqa: E402
from app.services.auth import hash_password  # noqa: E402
from app.services.seed import run as seed_database  # noqa: E402

KNOWN_PASSWORDS = {
    "admin": "Admin12345!",
    "reception": "Reception123!",
    "gouvernante": "Gouvernante1!",
}


@pytest.fixture(scope="session", autouse=True)
def bootstrap():
    init_db()
    seed_database([])
    with SessionLocal() as db:
        for username, password in KNOWN_PASSWORDS.items():
            user = db.query(User).filter(User.username == username).one()
            user.password_hash = hash_password(password)
        db.commit()
    yield


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, username: str) -> None:
    response = client.post("/login", data={"username": username, "password": KNOWN_PASSWORDS[username]},
                           follow_redirects=False)
    assert response.status_code == 303, response.text
