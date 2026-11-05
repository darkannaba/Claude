"""Montant en toutes lettres (anglais) pour la mention « This invoice is closed at the sum of… » des factures en anglais."""
from __future__ import annotations

_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
          "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
_SCALES = ((10**9, "billion"), (10**6, "million"), (1000, "thousand"))


def _below_100(n: int) -> str:
    if n < 20:
        return _UNITS[n]
    tens, unit = divmod(n, 10)
    return _TENS[tens] if unit == 0 else f"{_TENS[tens]}-{_UNITS[unit]}"


def _below_1000(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    if hundreds == 0:
        return _below_100(rest)
    head = f"{_UNITS[hundreds]} hundred"
    return head if rest == 0 else f"{head} and {_below_100(rest)}"


def number_to_words_en(n: int) -> str:
    """Nombre entier en lettres anglaises (usage britannique : « one hundred and five »)."""
    if n == 0:
        return "zero"
    if n < 0:
        return "minus " + number_to_words_en(-n)
    parts = []
    for value, name in _SCALES:
        count, n = divmod(n, value)
        if count:
            parts.append(f"{_below_1000(count)} {name}")
    if n:
        if parts and n < 100:
            parts.append(f"and {_below_100(n)}")
        else:
            parts.append(_below_1000(n))
    return " ".join(parts)


def amount_in_words_en(amount: int, currency_word: str = "Algerian dinars") -> str:
    words = number_to_words_en(abs(int(amount)))
    return f"{words} {currency_word}"[0].upper() + f"{words} {currency_word}"[1:]
