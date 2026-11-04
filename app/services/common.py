"""Erreurs métier et helpers de saisie partagés."""
from datetime import date


class BookingError(Exception):
    """Règle métier non respectée : le message est présenté tel quel à l'utilisateur."""


def to_int(value, label: str = "Montant", default: int | None = None, minimum: int | None = None) -> int:
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise BookingError(f"{label} requis.")
    try:
        number = int(float(str(value).replace(" ", "").replace(",", ".")))
    except ValueError as exc:
        raise BookingError(f"{label} invalide.") from exc
    if minimum is not None and number < minimum:
        raise BookingError(f"{label} doit être supérieur ou égal à {minimum}.")
    return number


def to_date(value, label: str = "Date", required: bool = True) -> date | None:
    if value is None or str(value).strip() == "":
        if required:
            raise BookingError(f"{label} requise.")
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise BookingError(f"{label} invalide (format AAAA-MM-JJ).") from exc
