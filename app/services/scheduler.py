"""Tâches planifiées en arrière-plan : audit de nuit automatique et sauvegarde quotidienne."""
from __future__ import annotations

import asyncio
import logging

from app.config import config
from app.database import SessionLocal
from app.services import backup, night_audit
from app.services.audit_log import log_action
from app.services.settings import load_hotel_settings, now_of

logger = logging.getLogger(__name__)


async def tick() -> dict:
    """Une itération : exécute ce qui est dû. Retourne un résumé (utile aux tests et aux journaux)."""
    summary = {"audits": 0, "backup": None}
    async with SessionLocal() as db:
        hs = await load_hotel_settings(db)
        now = now_of(hs)
        if now.hour >= hs.night_audit_hour:
            runs = await night_audit.run_pending_audits(db, hs, None)
            summary["audits"] = len(runs)
            await db.commit()
        if now.hour >= hs.backup_hour and not await asyncio.to_thread(backup.has_backup_on, now.date()):
            info = await asyncio.to_thread(backup.create_backup, "automatique", now)
            await asyncio.to_thread(backup.prune_backups, config.backup_retention)
            await log_action(db, None, "backup", "backup", info.name, "Sauvegarde automatique", now)
            await db.commit()
            summary["backup"] = info.name
    return summary


async def run_forever() -> None:
    logger.info("Planificateur démarré (intervalle %ss).", config.scheduler_interval_seconds)
    while True:
        try:
            summary = await tick()
            if summary["audits"] or summary["backup"]:
                logger.info("Tâches planifiées exécutées : %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:  # le planificateur ne doit jamais s'arrêter sur une erreur ponctuelle
            logger.exception("Erreur dans le planificateur")
        await asyncio.sleep(config.scheduler_interval_seconds)
