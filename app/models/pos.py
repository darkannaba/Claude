from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

OUTLETS = {
    "restaurant": "Restaurant / Café / Bar",
    "parking": "Parking",
    "salle_reunion": "Salle de réunion",
}
ORDER_STATUSES = {"open": "En cours", "paid": "Payée comptant", "room_charge": "Imputée chambre", "void": "Annulée"}
STOCK_REASONS = {"vente": "Vente", "reappro": "Réapprovisionnement", "ajustement": "Ajustement d'inventaire",
                 "annulation": "Annulation de vente"}


class PosItem(Base):
    __tablename__ = "pos_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    outlet: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60), default="")
    price: Mapped[int] = mapped_column(Integer, default=0)  # TTC
    vat_rate: Mapped[int] = mapped_column(Integer, default=19)
    track_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    stock_qty: Mapped[int] = mapped_column(Integer, default=0)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    @property
    def low_stock(self) -> bool:
        return self.track_stock and self.stock_qty <= self.low_stock_threshold


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("pos_items.id", ondelete="CASCADE"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)  # + entrée / − sortie
    reason: Mapped[str] = mapped_column(String(20))
    reference: Mapped[str] = mapped_column(String(80), default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    item = relationship("PosItem", lazy="joined")
    user = relationship("User", lazy="joined")


class PosOrder(Base):
    __tablename__ = "pos_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, default="")
    outlet: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    label: Mapped[str] = mapped_column(String(80), default="")  # table, plaque, salle...
    reservation_id: Mapped[int | None] = mapped_column(ForeignKey("reservations.id", ondelete="SET NULL"), nullable=True)
    payment_method: Mapped[str] = mapped_column(String(20), default="")
    total: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    lines = relationship("PosOrderLine", cascade="all, delete-orphan", order_by="PosOrderLine.id", lazy="selectin")
    reservation = relationship("Reservation", lazy="joined")
    user = relationship("User", lazy="joined")

    @property
    def outlet_label(self) -> str:
        return OUTLETS.get(self.outlet, self.outlet)

    @property
    def status_label(self) -> str:
        return ORDER_STATUSES.get(self.status, self.status)


class PosOrderLine(Base):
    __tablename__ = "pos_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("pos_orders.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("pos_items.id", ondelete="SET NULL"), nullable=True)
    description: Mapped[str] = mapped_column(String(160))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    vat_rate: Mapped[int] = mapped_column(Integer, default=19)
