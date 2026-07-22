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
    """Identifies the primary currency of a filing by finding the first
    currency symbol that precedes a number ≥ $1M (i.e. an actual deal
    amount). This avoids false positives from boilerplate references to
    small amounts in other currencies (e.g. 'C$5 Canadian withholding tax'
    in a USD JPMorgan prospectus)."""
    for m in _CCY_CAPTURE_RE.finditer(text[:30000]):
        following = text[m.end(): m.end() + 40]
        digits    = re.match(r"[\s\d,]+", following)
        if not digits:
            continue
        try:
            val = float(digits.group(0).replace(",", "").strip())
        except ValueError:
            continue
        if val >= 1_000_000:
            symbol = m.group(1).replace(" ", "").lower()
            return _CCY_TO_CODE.get(symbol, "USD")
    return "USD"

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
# page starts with a dollar amount (blank or filled).
# Negative lookahead excludes "Securities Offered" section ("$ of our X% Notes due").
# %[^$]{0,25}notes? allows adjectives between % and notes (e.g. "% Senior Notes due",
# "% Subordinated Notes due") which our earlier %\s*notes? was missing.
_TRANCHE_RE = re.compile(
    r"\$(?!(?:[\s\d,]*)of\s+our)[^$]{0,200}(?:floating\s+rate\s+[^$]{0,20}notes?|%[^$]{0,25}notes?)\s+due",
    re.IGNORECASE,
)
# Just "floating rate" is distinctive enough — avoids missing "Floating Rate Senior Notes"
_FRN_RE = re.compile(r"floating\s+rate", re.IGNORECASE)


def _detect_structure(text: str) -> tuple[int, bool]:
    """
    Returns (tranche_count, has_frn) by counting the first cluster of
    dollar-anchored tranche matches. Stops at the first large gap so that
    a repeated listing later in the document doesn't inflate the count.
    """
    matches = list(_TRANCHE_RE.finditer(text))
    if not matches:
        return 0, False

    # Walk through matches and stop when there's a gap > 500 chars,
    # which indicates we've moved past the first listing block.
    count = 1
    for i in range(1, len(matches)):
        if matches[i].start() - matches[i - 1].end() > 100:
            break
        count += 1

    # FRN: only flag if "floating rate notes" appears in an actual tranche
    # match string -- not just anywhere in the document (boilerplate like
    # "Unlike floating rate notes, these Notes bear a fixed rate" would
    # otherwise cause false positives on fixed-rate-only offerings).
    has_frn = any(_FRN_RE.search(m.group()) for m in matches[:count])
    return count, has_frn
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

    # Preliminary prospectus supplements carry "subject to completion" by SEC rule.
    # All amounts and maturities on the cover page are blank -- any figures found
    # in the text come from the existing notes or base prospectus incorporated by
    # reference, not the new deal. Zero them out so the table isn't misleading.
    is_preliminary = bool(doc_text and "subject to completion" in doc_text.lower())

    debt_amount   = None if is_preliminary else (_extract_debt_amount(doc_text, form) if doc_text else None)
    equity_amount = None if is_preliminary else (_extract_equity_amount(doc_text) if doc_text else None)
    maturities    = _extract_maturities(doc_text) if doc_text else []  # always extract -- years are stated even in preliminary
    currency      = _detect_currency(doc_text) if doc_text else "USD"
    tranche_count, has_frn = _detect_structure(doc_text) if doc_text else (0, False)
    structure     = _structure_label(tranche_count, has_frn)
    if is_preliminary and structure:
        structure += " (Preliminary)"

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
