"""Enregistrement de tous les modèles ORM auprès de ``Base.metadata``."""
from app.models.audit import AuditLog, NightAuditRun
from app.models.backup import BACKUP_DESTINATIONS, BACKUP_STATUSES, BackupJob
from app.models.booking import ACTIVE_STATUSES, CHANNELS, RES_STATUSES, Companion, Reservation, StaySegment
from app.models.customer import GENDERS, GUEST_STATUSES, ID_TYPES, ID_TYPES_AR, Company, Guest, normalize_id_number
from app.models.id_scan import ID_SCAN_SOURCES, ID_SCAN_STATUSES, IdScan
from app.models.licensing import LicenseInfo
from app.models.ota import OTA_CHANNELS, OTA_PERMISSIONS, OtaApiKey, OtaBooking, OtaLog
from app.models.invoice import ITEM_KINDS, PAYMENT_KINDS, PAYMENT_METHODS, FolioItem, Invoice, Payment
from app.models.pos import OUTLETS, ORDER_STATUSES, STOCK_REASONS, PosItem, PosOrder, PosOrderLine, StockMovement
from app.models.room import (
    PRICE_FIELDS, PRICE_TARGETS, ROOM_STATUSES, SEASON_LEVELS, LongStayDiscount, PriceChange, RatePeriod, Room,
    RoomType, Season,
)
from app.models.system import FeatureFlag, Setting
from app.models.theme import BRANDING_KINDS, THEME_DENSITIES, THEME_FONTS, ThemeSettings
from app.models.user import ROLES, User

__all__ = [
    "AuditLog", "NightAuditRun", "BACKUP_DESTINATIONS", "BACKUP_STATUSES", "BackupJob",
    "ACTIVE_STATUSES", "CHANNELS", "RES_STATUSES", "Companion", "Reservation", "StaySegment",
    "GENDERS", "GUEST_STATUSES", "ID_TYPES", "ID_TYPES_AR", "Company", "Guest", "normalize_id_number",
    "ITEM_KINDS", "PAYMENT_KINDS", "PAYMENT_METHODS", "FolioItem", "Invoice", "Payment",
    "OUTLETS", "ORDER_STATUSES", "STOCK_REASONS", "PosItem", "PosOrder", "PosOrderLine", "StockMovement",
    "ROOM_STATUSES", "RatePeriod", "Room", "RoomType",
    "PRICE_FIELDS", "PRICE_TARGETS", "SEASON_LEVELS", "LongStayDiscount", "PriceChange", "Season",
    "FeatureFlag", "Setting",
    "BRANDING_KINDS", "THEME_DENSITIES", "THEME_FONTS", "ThemeSettings",
    "ROLES", "User",
    "ID_SCAN_SOURCES", "ID_SCAN_STATUSES", "IdScan",
    "LicenseInfo", "OTA_CHANNELS", "OTA_PERMISSIONS", "OtaApiKey", "OtaBooking", "OtaLog",
]
