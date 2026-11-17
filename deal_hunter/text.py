"""Turkish-aware text helpers: lowercasing, number and price parsing."""
from __future__ import annotations

import re
import unicodedata

_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})
_ASCII = str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ç": "c", "ö": "o", "ü": "u", "â": "a", "î": "i", "û": "u"})


def tr_lower(s: str) -> str:
    """Turkish lowercasing: 'I' -> 'ı', 'İ' -> 'i' (str.lower gets both wrong)."""
    return unicodedata.normalize("NFC", s).translate(_TR_LOWER).lower()


def fold(s: str) -> str:
    """Lowercase and strip Turkish diacritics, for matching only (never for display)."""
    return tr_lower(s).translate(_ASCII)


def fmt_tl(x: float) -> str:
    """21499.9 -> '21.500 TL' (Turkish thousands separator)."""
    return f"{round(x):,}".replace(",", ".") + " TL"


def parse_amount(text: str) -> float | None:
    """'25.000 TL', '25 bin', '25k', '24.999,90 TL' -> number. Returns None if nothing found."""
    t = fold(text)
    m = re.search(r"(\d+(?:[.,]\d+)*)\s*(bin|k\b)", t)
    if m:
        return float(m.group(1).replace(".", "").replace(",", ".")) * 1000
    m = re.search(r"(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)\s*(tl|₺|lira)", t)
    if m:
        return float(m.group(1).replace(".", "").replace(",", "."))
    return None


def parse_inches(text: str) -> float | None:
    """'55 inç', '55"', '55 inch', '139 cm ekran' -> inches."""
    t = fold(text)
    m = re.search(r"(\d{2,3}(?:[.,]\d)?)\s*(?:\"|''|inc\b|inch|in\b|”)", t)
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(r"(\d{2,3})\s*cm", t)
    if m:
        return round(int(m.group(1)) / 2.54)
    return None


def parse_capacity_gb(text: str) -> list[int]:
    """All storage/RAM capacities in a title, in GB ('1 TB' -> 1024)."""
    out = []
    for num, unit in re.findall(r"(\d+)\s*(gb|tb)\b", fold(text)):
        out.append(int(num) * (1024 if unit == "tb" else 1))
    return out
