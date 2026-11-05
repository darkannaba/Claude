"""Tâches planifiées en arrière-plan : audit de nuit automatique et double sauvegarde quotidienne."""
from __future__ import annotations

import asyncio
import logging

from app.config import config
from app.database import SessionLocal
from app.services import backup_manager, night_audit
from app.services.audit_log import log_action
from app.services.settings import load_hotel_settings, now_of

logger = logging.getLogger(__name__)


async def tick() -> dict:
    """Une itération : exécute ce qui est dû. Retourne un résumé (utile aux tests et aux journaux)."""
    summary: dict = {"audits": 0, "backup": None}
    async with SessionLocal() as db:
        hs = await load_hotel_settings(db)
        now = now_of(hs)
        if now.hour >= hs.night_audit_hour:
            runs = await night_audit.run_pending_audits(db, hs, None)
            summary["audits"] = len(runs)
            await db.commit()
        if now.hour >= hs.backup_hour:
            # À partir de l'heure de sauvegarde : chaque destination activée sans sauvegarde automatique
            # réussie aujourd'hui est exécutée (une archive commune, une ligne d'historique par destination).
            due = await backup_manager.due_destinations(db, hs, now)
            if due:
                jobs = await backup_manager.run_backup(db, hs, trigger="automatique", destinations=due, when=now)
                states = {job.destination: job.status for job in jobs}
                archive = jobs[0].archive_name
                await log_action(db, None, "backup", "backup", archive, "Sauvegarde automatique : "
                                 + ", ".join(f"{dest}={state}" for dest, state in states.items()), now)
                await db.commit()
                summary["backup"] = {"archive": archive, "destinations": states}
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
