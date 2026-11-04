"""Import de tous les modèles pour l'enregistrement auprès de SQLAlchemy."""
from app.models.admin import Company, FeatureFlag, ServiceItem, Setting, User
from app.models.guests import ID_TYPES, Guest
from app.models.property import ROOM_STATUSES, RatePeriod, Room, RoomType
from app.models.reservations import (
    ACTIVE_STATUSES,
    CHARGE_CATEGORIES,
    PAYMENT_KINDS,
    PAYMENT_METHODS,
    STATUS_LABELS,
    Charge,
    Companion,
    Invoice,
    Payment,
    Reservation,
    StaySegment,
)

__all__ = [
    "User", "Setting", "FeatureFlag", "Company", "ServiceItem",
    "RoomType", "Room", "RatePeriod", "ROOM_STATUSES",
    "Guest", "ID_TYPES",
    "Reservation", "StaySegment", "Companion", "Payment", "Charge", "Invoice",
    "ACTIVE_STATUSES", "STATUS_LABELS", "PAYMENT_KINDS", "PAYMENT_METHODS", "CHARGE_CATEGORIES",
]
