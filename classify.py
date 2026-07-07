"""
classify.py

Classifies a filing as debt, equity, or both, and flags structured/retail
notes (continuous bank note shelves) that aren't genuine corporate issuance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Keyword lists
# ---------------------------------------------------------------------------

DEBT_KEYWORDS = [
    "senior notes", "subordinated notes", "convertible notes", "debentures",
    "term loan", "credit facility", "indenture", "notes due", "senior secured notes",
    "private placement of notes", "bonds", "revolving credit", "promissory note",
    "debt securities", "aggregate principal amount",
]

EQUITY_KEYWORDS = [
    "shares of common stock",       # offering-specific phrasing
    "shares of our common stock",   # offering-specific phrasing
    "ordinary shares",
    "at-the-market offering",
    "atm program",
    "equity offering",
    "warrants to purchase",
    "rights offering",
    "depositary shares",
    "concurrent equity offering",
]

STRUCTURED_NOTE_KEYWORDS = [
    "estimated value of the notes", "estimated value on the pricing date",
    "estimated value of the securities", "buffer", "autocallable", "auto-callable",
    "participation rate", "barrier event", "knock-in", "underlying index",
    "basket of underliers", "market-linked", "index-linked notes", "trigger value",
    "digital notes", "accelerated return notes", "leveraged notes", "capped notes",
    "principal at risk", "callable notes linked to", "contingent coupon",
    "reference asset", "underlying stock",
]

# 8-K items that indicate debt or equity issuance
ITEM_DEBT = {"2.03"}
ITEM_EQUITY = {"3.02"}

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    is_debt: bool
    is_equity: bool
    is_structured_note: bool = False
    debt_amount: float | None = None
    equity_amount: float | None = None
    maturity_years: list[int] = field(default_factory=list)
    currency: str = "USD"
    structure: str = ""  # e.g. "8-part (incl. FRN)", "3-part", "single tranche"


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

_CCY = r"(?:C|CA|A|AU|NZ|HK|S|US)?\s*\$|€|£|¥|CHF\s*"

# Captures the currency prefix so we can label amounts correctly
_CCY_CAPTURE_RE = re.compile(
    r"(C(?:A)?\s*\$|A(?:U)?\s*\$|NZ\s*\$|HK\s*\$|S\s*\$|US\s*\$|€|£|¥|CHF|\$)",
    re.IGNORECASE,
)

_CCY_TO_CODE = {
    "$": "USD", "us$": "USD",
    "c$": "CAD", "ca$": "CAD",
    "a$": "AUD", "au$": "AUD",
    "nz$": "NZD",
    "hk$": "HKD",
    "s$": "SGD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "chf": "CHF",
}


def _detect_currency(text: str) -> str:
    """Identifies the primary currency of a filing from the first currency
    symbol found near a large number. Defaults to USD."""
    m = _CCY_CAPTURE_RE.search(text)
    if not m:
        return "USD"
    symbol = m.group(1).replace(" ", "").lower()
    return _CCY_TO_CODE.get(symbol, "USD")

_DEBT_AMT_RE = re.compile(
    rf"(?:{_CCY})\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?\s*aggregate principal amount",
    re.IGNORECASE,
)
_DEBT_AMT_ALT_RE = re.compile(
    rf"aggregate principal amount of\s*(?:{_CCY})\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?",
    re.IGNORECASE,
)
_FWP_TRANCHE_RE = re.compile(
    rf"(?:{_CCY})\s*([\d,]+)\s*(million|billion)?(?!\s*%)",
    re.IGNORECASE,
)
_FWP_SIZE_SECTION_RE = re.compile(r"size\s*[:\|](.*?)(?:\n\n|\Z)", re.IGNORECASE | re.DOTALL)

_EQUITY_AMT_RE = re.compile(
    rf"(?:{_CCY})\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?\s*(?:maximum\s+)?aggregate\s+offering\s+price",
    re.IGNORECASE,
)
_EQUITY_AMT_ALT_RE = re.compile(
    rf"(?:{_CCY})\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?\s*(?:of\s+)?(?:common\s+stock|ordinary\s+shares)",
    re.IGNORECASE,
)

_MATURITY_RE = re.compile(
    r"(?:notes?|bonds?|debentures?)\s+due\s+(?:[A-Za-z]+\.?\s+\d{1,2},?\s+)?(\d{4})",
    re.IGNORECASE,
)
_ITEM_RE = re.compile(r"item\s+(\d{1,2}\.\d{2})", re.IGNORECASE)


def _parse_amount(raw: str, unit: str) -> float:
    v = float(raw.replace(",", ""))
    u = unit.lower()
    if u == "million":
        v *= 1e6
    elif u == "billion":
        v *= 1e9
    return v


def _extract_debt_amount(text: str, form: str) -> float | None:
    m = _DEBT_AMT_RE.search(text) or _DEBT_AMT_ALT_RE.search(text)
    if m:
        try:
            return _parse_amount(m.group(1), m.group(2) or "")
        except ValueError:
            pass
    # FWP fallback: sum tranche sizes from the Size: section
    if form.upper() == "FWP":
        section = _FWP_SIZE_SECTION_RE.search(text)
        blob = section.group(1) if section else text[:2000]
        total = sum(
            _parse_amount(m.group(1), m.group(2) or "")
            for m in _FWP_TRANCHE_RE.finditer(blob)
            if (v := _parse_amount(m.group(1), m.group(2) or "")) >= 1_000_000
        )
        return total if total > 0 else None
    return None


def _extract_equity_amount(text: str) -> float | None:
    for pattern in (_EQUITY_AMT_RE, _EQUITY_AMT_ALT_RE):
        m = pattern.search(text)
        if m:
            try:
                return _parse_amount(m.group(1), m.group(2) or "")
            except ValueError:
                pass
    return None


def _extract_maturities(text: str) -> list[int]:
    years = {int(y) for y in _MATURITY_RE.findall(text) if 2020 <= int(y) <= 2200}
    return sorted(years)


# Tranche detection -- anchored on "$" since every tranche line on the cover
# page starts with a dollar amount (blank or filled). This avoids double-counting
# from the table of contents which lists each series again without "$" prefixes.
_TRANCHE_RE = re.compile(
    r"\$\s*[\d,]*\s*(?:floating\s+rate\s+notes?|(?:[\d\.]+\s*)?%?\s*notes?)\s+due",
    re.IGNORECASE,
)
_FRN_RE = re.compile(r"floating\s+rate\s+notes?", re.IGNORECASE)


def _detect_structure(text: str) -> tuple[int, bool]:
    """
    Returns (tranche_count, has_frn) by counting dollar-anchored tranche
    lines on the cover page. Scanning up to 15,000 chars covers the full
    cover page without reaching body text.
    """
    cover = text[:15000]
    tranche_count = len(_TRANCHE_RE.findall(cover))
    has_frn       = bool(_FRN_RE.search(cover))
    return tranche_count, has_frn


def _structure_label(tranche_count: int, has_frn: bool) -> str:
    """Human-readable structure string, e.g. '8-part (incl. FRN)' or '3-part'."""
    if tranche_count == 0:
        return ""
    label = f"{tranche_count}-part" if tranche_count > 1 else "single tranche"
    if has_frn:
        label += " (incl. FRN)"
    return label


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

def classify(form: str, items: str, doc_text: str | None) -> Classification:
    form = form.upper().strip()

    # Recover 8-K item codes from text if not supplied
    item_codes = {i.strip() for i in items.split(",") if i.strip()} if items else set()
    if not item_codes and doc_text and form.startswith("8-K"):
        item_codes = set(_ITEM_RE.findall(doc_text))

    is_structured = (
        any(kw in doc_text.lower() for kw in STRUCTURED_NOTE_KEYWORDS)
        if doc_text else False
    )
    debt_amount   = _extract_debt_amount(doc_text, form) if doc_text else None
    equity_amount = _extract_equity_amount(doc_text) if doc_text else None
    maturities    = _extract_maturities(doc_text) if doc_text else []
    currency      = _detect_currency(doc_text) if doc_text else "USD"
    tranche_count, has_frn = _detect_structure(doc_text) if doc_text else (0, False)
    structure     = _structure_label(tranche_count, has_frn)

    # 8-K: use item codes as primary signal
    if form.startswith("8-K"):
        return Classification(
            is_debt=bool(item_codes & ITEM_DEBT),
            is_equity=bool(item_codes & ITEM_EQUITY),
            is_structured_note=is_structured,
            debt_amount=debt_amount,
            equity_amount=equity_amount,
            maturity_years=maturities,
            currency=currency,
            structure=structure,
        )

    # All other forms: keyword scan
    if doc_text:
        lower = doc_text.lower()
        debt_hits  = [kw for kw in DEBT_KEYWORDS  if kw in lower]
        equity_hits = [kw for kw in EQUITY_KEYWORDS if kw in lower]

        if form == "FWP":
            is_debt   = len(debt_hits) >= 1 or len(maturities) >= 2
            is_equity = len(equity_hits) >= 1
        else:
            is_debt   = len(debt_hits) >= 2
            is_equity = len(equity_hits) >= 3

        return Classification(
            is_debt=is_debt,
            is_equity=is_equity,
            is_structured_note=is_structured,
            debt_amount=debt_amount,
            equity_amount=equity_amount,
            maturity_years=maturities,
            currency=currency,
            structure=structure,
        )

    return Classification(False, False)


def is_debt_filing(
    cls: Classification,
    exclude_structured: bool = True,
    min_debt_amount: float | None = None,
) -> bool:
    if not cls.is_debt:
        return False
    if exclude_structured and cls.is_structured_note:
        return False
    if min_debt_amount and cls.debt_amount is not None and cls.debt_amount < min_debt_amount:
        return False
    return True
