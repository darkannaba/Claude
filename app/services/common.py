"""Erreurs métier et validations de saisie partagées."""
from __future__ import annotations

from datetime import date


class BusinessError(Exception):
    """Règle métier non respectée : le message est affiché tel quel à l'utilisateur."""


def to_int(value, label: str = "Montant", default: int | None = None,
           minimum: int | None = None, maximum: int | None = None) -> int:
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise BusinessError(f"{label} requis.")
    try:
        number = int(float(str(value).replace(" ", "").replace(" ", "").replace(",", ".")))
    except ValueError as exc:
        raise BusinessError(f"{label} invalide.") from exc
    if minimum is not None and number < minimum:
        raise BusinessError(f"{label} doit être supérieur ou égal à {minimum}.")
    if maximum is not None and number > maximum:
        raise BusinessError(f"{label} doit être inférieur ou égal à {maximum}.")
    return number


def to_date(value, label: str = "Date", required: bool = True) -> date | None:
    if value is None or str(value).strip() == "":
        if required:
            raise BusinessError(f"{label} requise.")
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise BusinessError(f"{label} invalide (format AAAA-MM-JJ).") from exc


def parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def like_pattern(term: str) -> str:
    """Motif LIKE sûr : les caractères joker saisis par l'utilisateur sont échappés."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def split_vat(amount_ttc: int, rate: int) -> tuple[int, int]:
    """Décompose un montant TTC en (HT, TVA) arrondis au dinar."""
    if not rate:
        return amount_ttc, 0
    ht = round(amount_ttc / (1 + rate / 100))
    return ht, amount_ttc - ht


def format_money(amount, symbol: str = "DA") -> str:
    try:
        number = int(round(float(amount or 0)))
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}".replace(",", " ") + f" {symbol}"
