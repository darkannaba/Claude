"""Journal d'activité (traçabilité des opérations sensibles)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User


async def log_action(db: AsyncSession, user: User | None, action: str, entity: str = "",
                     entity_id: object = "", details: str = "", when: datetime | None = None) -> None:
    db.add(AuditLog(
        created_at=when or datetime.now(),
        user_id=user.id if user else None,
        username=user.username if user else "système",
        action=action,
        entity=entity,
        entity_id=str(entity_id or ""),
        details=details[:2000],
    ))
