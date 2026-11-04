"""Montant en toutes lettres (français) pour la mention « Arrêtée la présente facture à la somme de… »."""
from __future__ import annotations

_UNITS = ["zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix", "onze",
          "douze", "treize", "quatorze", "quinze", "seize"]
_TENS = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}


def _below_100(n: int) -> str:
    if n < 17:
        return _UNITS[n]
    if n < 20:
        return "dix-" + _UNITS[n - 10]
    if n < 70:
        tens, unit = divmod(n, 10)
        base = _TENS[tens]
        if unit == 0:
            return base
        if unit == 1:
            return f"{base} et un"
        return f"{base}-{_UNITS[unit]}"
    if n < 80:
        rest = n - 60
        return "soixante et onze" if rest == 11 else "soixante-" + _below_100(rest)
    rest = n - 80
    return "quatre-vingts" if rest == 0 else "quatre-vingt-" + _below_100(rest)


def _below_1000(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    if hundreds == 0:
        return _below_100(rest)
    head = "cent" if hundreds == 1 else f"{_UNITS[hundreds]} cent"
    if rest == 0:
        return head + ("s" if hundreds > 1 else "")
    return f"{head} {_below_100(rest)}"


def number_to_words_fr(n: int) -> str:
    if n == 0:
        return "zéro"
    if n < 0:
        return "moins " + number_to_words_fr(-n)
    billions, n = divmod(n, 10**9)
    millions, n = divmod(n, 10**6)
    thousands, units = divmod(n, 1000)
    parts = []
    if billions:
        parts.append(f"{_below_1000(billions)} milliard{'s' if billions > 1 else ''}")
    if millions:
        parts.append(f"{_below_1000(millions)} million{'s' if millions > 1 else ''}")
    if thousands:
        if thousands == 1:
            parts.append("mille")
        else:
            words = _below_1000(thousands)
            if words.endswith("cents") or words.endswith("vingts"):
                words = words[:-1]  # « deux cent mille », « quatre-vingt mille »
            parts.append(f"{words} mille")
    if units:
        parts.append(_below_1000(units))
    return " ".join(parts)


def amount_in_words(amount: int, currency_word: str = "dinars algériens") -> str:
    words = number_to_words_fr(abs(int(amount)))
    return f"{words} {currency_word}".capitalize()
