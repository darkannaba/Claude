"""Lancement du serveur : `python main.py` puis http://localhost:8000.

Un seul processus (SQLite + planificateur intégré) : ne pas lancer plusieurs workers.
"""
import uvicorn

from app.config import config

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        workers=1,
        proxy_headers=True,
        log_level=config.log_level.lower(),
    )
