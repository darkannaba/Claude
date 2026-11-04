"""Enregistrement de tous les modèles ORM auprès de ``Base.metadata``."""
from app.models.audit import AuditLog, NightAuditRun
from app.models.booking import ACTIVE_STATUSES, CHANNELS, RES_STATUSES, Companion, Reservation, StaySegment
from app.models.customer import GENDERS, GUEST_STATUSES, ID_TYPES, ID_TYPES_AR, Company, Guest, normalize_id_number
from app.models.invoice import ITEM_KINDS, PAYMENT_KINDS, PAYMENT_METHODS, FolioItem, Invoice, Payment
from app.models.pos import OUTLETS, ORDER_STATUSES, STOCK_REASONS, PosItem, PosOrder, PosOrderLine, StockMovement
from app.models.room import ROOM_STATUSES, RatePeriod, Room, RoomType
from app.models.system import FeatureFlag, Setting
from app.models.user import ROLES, User

__all__ = [
    "AuditLog", "NightAuditRun",
    "ACTIVE_STATUSES", "CHANNELS", "RES_STATUSES", "Companion", "Reservation", "StaySegment",
    "GENDERS", "GUEST_STATUSES", "ID_TYPES", "ID_TYPES_AR", "Company", "Guest", "normalize_id_number",
    "ITEM_KINDS", "PAYMENT_KINDS", "PAYMENT_METHODS", "FolioItem", "Invoice", "Payment",
    "OUTLETS", "ORDER_STATUSES", "STOCK_REASONS", "PosItem", "PosOrder", "PosOrderLine", "StockMovement",
    "ROOM_STATUSES", "RatePeriod", "Room", "RoomType",
    "FeatureFlag", "Setting",
    "ROLES", "User",
]
