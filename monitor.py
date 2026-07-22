"""
monitor.py

Two scanning modes:

1. check_realtime() -- FAST, for live monitoring
   Polls EDGAR's current-filings Atom feed per form type (~8 requests total),
   filters hits against the universe CIK set, and only fetches filing text
   for actual matches. Runs in seconds. Use this with auto-refresh.

2. check_filings() -- THOROUGH, for historical catch-up
   Polls each company's submission history directly (~600 requests).
   Complete but slow (~2-3 min). Use this for a specific date range.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

SEC_USER_AGENT = "sec-debt-monitor henry.gilbert@newsquawk.com"
HEADERS = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_URL   = "https://www.sec.gov/Archives/edgar/data"
FEED_URL       = "https://www.sec.gov/cgi-bin/browse-edgar"

DEBT_FORMS = {"S-1", "S-3", "S-3ASR", "424B1", "424B2", "424B3", "424B4", "424B5", "FWP", "8-K"}

_TAG_RE    = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]+|#\d+);")
_WS_RE     = re.compile(r"\s+")
_CIK_RE    = re.compile(r"/data/(\d+)/")
_ACC_RE    = re.compile(r"/(\d{10}-\d{2}-\d{6})-index\.htm")

_NDU_RE    = re.compile(r"notes?\s+due", re.IGNORECASE)
_FRN_RE    = re.compile(r"floating\s+rate", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?", re.IGNORECASE)
_YEAR_RE   = re.compile(r"\b(20\d{2})\b")
_PRELIM_RE = re.compile(r"subject\s+to\s+completion", re.IGNORECASE)


def _parse_cover_summary(text: str) -> dict:
    """
    Parses the bolded summary at the very top of a prospectus cover page --
    e.g. "$300,000,000 Floating Rate Senior Notes due 2029  $1,000,000,000
    4.750% Senior Notes due 2029 ..." -- which lists every tranche with its
    amount in one consistent prose format regardless of how the issuer lays
    out their tables. This is the primary extraction method; tables are the
    fallback.

    Each tranche = a "$<amount>" followed (within the same segment, before
    the next "$") by "notes due <year>". Tranches are deduped on
    (amount, coupon/FRN, year) so the same deal listed twice (cover + summary
    paragraph) isn't double counted.
    """
    head = text[:20000]
    seen_keys = set()
    tranches  = []   # list of (amount, is_frn, year)

    for m in re.finditer(r"\$\s*([\d,]{7,})", head):
        try:
            amt = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if amt < 1_000_000:
            continue
        seg = head[m.end(): m.end() + 160]
        nd  = re.search(r"notes\s+due\s+(?:[A-Za-z]+\.?\s+\d{1,2},?\s+)?(20\d{2})", seg, re.IGNORECASE)
        if not nd:
            continue
        # Don't cross into the next tranche's segment
        next_dollar = seg.find("$")
        if next_dollar != -1 and nd.start() > next_dollar:
            continue
        before   = seg[:nd.start()]
        is_frn   = bool(re.search(r"floating\s+rate", before, re.IGNORECASE))
        coupon_m = re.search(r"([\d.]+)\s*%", before)
        coupon   = coupon_m.group(1) if coupon_m else ("FRN" if is_frn else "")
        year     = int(nd.group(1))
        if not (2020 <= year <= 2200):
            continue
        key = (amt, coupon, year)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        tranches.append((amt, is_frn, year))

    if not tranches:
        return {"found": False}

    return {
        "found":          True,
        "is_preliminary": bool(_PRELIM_RE.search(head)),
        "tranche_count":  len(tranches),
        "has_frn":        any(t[1] for t in tranches),
        "total_amount":   sum(t[0] for t in tranches),
        "maturities":     sorted({t[2] for t in tranches}),
    }


def _parse_cover_table(raw_html: str) -> dict:
    """
    Extracts deal structure from the cover page HTML table.

    Format C -- FWP vertical definition table (checked first):
      | Title: | 3.400% Notes due 2029 / Floating Rate Notes due 2028 / ... |
      | Size:  | 2029 Notes: $1,250,000,000 / FRN: $2,000,000,000 / ...    |
      Detected by cells whose text is just "Title" or "Size".

    Format B -- column-per-tranche (some FWP term sheets):
      | FRN | 3.40% Notes | 3.70% Notes | ... |

    Format A -- row-per-tranche (424B5 cover page):
      | $1,000,000,000 | Floating Rate Senior Notes due 2028 |
    """
    result = {
        "found":          False,
        "is_preliminary": bool(_PRELIM_RE.search(raw_html)),
        "tranche_count":  0,
        "has_frn":        False,
        "total_amount":   None,
        "maturities":     [],
    }

    def _amounts_from_text(text: str) -> list[float]:
        found = []
        for m in _AMOUNT_RE.finditer(text):
            try:
                val = float(m.group(1).replace(",", ""))
                mult = (m.group(2) or "").lower()
                if mult == "million":
                    val *= 1e6
                elif mult == "billion":
                    val *= 1e9
                if val >= 1_000_000:
                    found.append(val)
            except ValueError:
                pass
        return found

    def _years_from_text(text: str) -> list[int]:
        return [int(y) for y in _YEAR_RE.findall(text) if 2020 <= int(y) <= 2200]

    _TITLE_CELL = re.compile(r"^\s*title\s*:?\s*$", re.IGNORECASE)
    _SIZE_CELL  = re.compile(r"^\s*size\s*:?\s*$",  re.IGNORECASE)

    try:
        soup = BeautifulSoup(raw_html, "lxml")

        for table in soup.find_all("table"):
            all_rows = table.find_all("tr")

            # ── Format C: FWP vertical definition table (Title / Size label rows)
            # Accumulate from this table -- FWPs often use SEPARATE tables for
            # FRN and fixed tranches (e.g. Amazon: one table per series group).
            # We collect from all tables and merge at the end.
            title_text, size_texts = "", []
            for row in all_rows:
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True)
                    value = cells[1].get_text(separator=" ", strip=True)
                    if _TITLE_CELL.match(label):
                        title_text += " " + value
                    elif _SIZE_CELL.match(label):
                        size_texts.append(value)

            if title_text.strip() and _NDU_RE.search(title_text):
                result["found"]          = True
                result["tranche_count"] += len(_NDU_RE.findall(title_text))
                result["has_frn"]        = result["has_frn"] or bool(_FRN_RE.search(title_text))
                result["maturities"]     = sorted(set(result["maturities"] + _years_from_text(title_text)))
                for st in size_texts:
                    result.setdefault("_amounts", []).extend(_amounts_from_text(st))
                continue   # keep scanning remaining tables

            # ── Format B: row where 2+ cells each contain "notes due"
            for row in all_rows:
                cells = [c for c in row.find_all(["td", "th"])
                         if _NDU_RE.search(c.get_text())]
                if len(cells) >= 2:
                    has_frn    = any(_FRN_RE.search(c.get_text()) for c in cells)
                    maturities = []
                    for c in cells:
                        maturities.extend(_years_from_text(c.get_text()))
                    amounts = _amounts_from_text(table.get_text())
                    result.update({
                        "found":         True,
                        "tranche_count": len(cells),
                        "has_frn":       has_frn,
                        "total_amount":  sum(amounts) if amounts else None,
                        "maturities":    sorted(set(maturities)),
                    })
                    return result

            # ── Format A: each row with "notes due" = one tranche (424B5)
            # Some printer HTML puts all tranches in ONE <tr> with <br> between
            # them. In that case, count "notes due" occurrences within the row.
            rows = [r for r in all_rows if _NDU_RE.search(r.get_text())]
            if not rows:
                continue
            amounts, years, has_frn, tranche_count = [], [], False, 0
            for row in rows:
                text = row.get_text(separator=" ", strip=True)
                if _FRN_RE.search(text):
                    has_frn = True
                amounts.extend(_amounts_from_text(text))
                years.extend(_years_from_text(text))
                # Count tranches: if multiple "notes due" in one row, each is a tranche
                ndu_in_row = len(_NDU_RE.findall(text))
                tranche_count += max(ndu_in_row, 1)
            result.update({
                "found":         True,
                "tranche_count": tranche_count,
                "has_frn":       has_frn,
                "total_amount":  sum(amounts) if amounts else None,
                "maturities":    sorted(set(years)),
            })
            return result

    except Exception:
        pass

    # Total amounts collected across all Format C tables
    if result.get("_amounts"):
        result["total_amount"] = sum(result.pop("_amounts"))
    elif "_amounts" in result:
        result.pop("_amounts")

    return result


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict | None = None, retries: int = 3, backoff: float = 1.0):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(backoff * (attempt + 1) * 2)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (attempt + 1))


def _fetch_filing(cik: int, accession_with_dashes: str, max_bytes: int = 600_000) -> tuple[str, dict]:
    """
    Fetches the primary filing document and returns:
      (stripped_text, cover_table_data)

    stripped_text  -- for debt/equity keyword classification
    cover_table_data -- extracted directly from the first 'notes due' HTML
                        table: tranche count, FRN flag, amounts, maturities.
                        Far more accurate than regex on stripped text because
                        body text / existing notes cannot pollute the result.

    Step 1: fetch the filing index to find the primary .htm document.
    Step 2: fetch the first N bytes (the cover page) of that document.
    """
    acc_no_dashes = accession_with_dashes.replace("-", "")
    index_url = f"{ARCHIVES_URL}/{cik}/{acc_no_dashes}/{accession_with_dashes}-index.htm"

    doc_url = None
    try:
        resp = _get(index_url)
        soup  = BeautifulSoup(resp.content, "lxml")
        for a in soup.select("table.tableFile a"):
            href = a.get("href", "")
            if href.lower().endswith((".htm", ".html")) and "index" not in href.lower():
                doc_url = f"https://www.sec.gov{href}" if href.startswith("/") else href
                break
    except Exception:
        pass

    fetch_url = doc_url or f"{ARCHIVES_URL}/{cik}/{acc_no_dashes}/{accession_with_dashes}.txt"
    empty = ("", {"found": False, "is_preliminary": False, "tranche_count": 0,
                  "has_frn": False, "total_amount": None, "maturities": []})
    try:
        with requests.get(fetch_url, headers=HEADERS, timeout=20, stream=True) as r:
            r.raise_for_status()
            chunks, total = [], 0
            for chunk in r.iter_content(chunk_size=32768):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                # Early exit: stop as soon as we have ~10K chars of *visible*
                # text -- that comfortably covers any cover page. Printer HTML
                # wraps each visible line in huge styling markup, so raw bytes
                # are a poor proxy for content; visible-text length is the
                # right stop condition. Check every ~128KB to keep it cheap.
                if total % 131072 < 32768:
                    visible = _WS_RE.sub(" ", _TAG_RE.sub(" ", b"".join(chunks).decode("utf-8", errors="ignore")))
                    if len(visible) > 10_000:
                        break
                if total >= max_bytes:
                    break
        raw   = b"".join(chunks).decode("utf-8", errors="ignore")
        table = _parse_cover_table(raw)
        text  = _TAG_RE.sub(" ", raw)
        text  = _ENTITY_RE.sub(" ", text)
        return _WS_RE.sub(" ", text).strip(), table
    except requests.RequestException:
        return empty


def _fmt_structure(count: int, has_frn: bool, preliminary: bool = False) -> str:
    if count == 0:
        return ""
    label = f"{count}-part" if count > 1 else "single tranche"
    if has_frn:
        label += " (incl. FRN)"
    if preliminary:
        label += " (Preliminary)"
    return label


def _build_result_row(hit: dict, cik_to_row: dict) -> dict | None:
    """Classifies a filing hit and returns a result row, or None if not relevant."""
    from classify import classify, is_debt_filing
    cik  = hit["cik"]
    text, tbl = _fetch_filing(cik, hit["accession"])
    cls  = classify(hit["form"], hit["items"], text)
    if not is_debt_filing(cls):
        return None
    row = cik_to_row.get(cik, None)
    acc = hit["accession"]

    # Extraction priority:
    # 1. Cover page summary (the bolded tranche list at the very top -- most
    #    consistent format across issuers regardless of table layout)
    # 2. HTML cover table (varies by issuer)
    # 3. Classifier regex results (8-Ks and other non-prospectus forms)
    summary = _parse_cover_summary(text)

    if summary.get("found"):
        prelim     = summary["is_preliminary"]
        structure  = _fmt_structure(summary["tranche_count"], summary["has_frn"], prelim)
        debt_size  = None if prelim else summary["total_amount"]
        maturities = summary["maturities"]   # always show -- years are stated even in preliminary
        currency   = cls.currency
    elif tbl["found"]:
        structure  = _fmt_structure(tbl["tranche_count"], tbl["has_frn"], tbl["is_preliminary"])
        debt_size  = None if tbl["is_preliminary"] else tbl["total_amount"]
        maturities = tbl["maturities"]       # always show
        currency   = cls.currency
    else:
        structure  = cls.structure
        debt_size  = cls.debt_amount
        maturities = cls.maturity_years
        currency   = cls.currency

    return {
        "filed_at":       hit["filed_at"],
        "ticker":         row.ticker if row else "",
        "company":        row.name   if row else hit.get("company", ""),
        "form":           hit["form"],
        "classification": "Debt + Equity" if cls.is_debt and cls.is_equity else "Debt",
        "currency":       currency,
        "debt_size":      debt_size,
        "equity_size":    cls.equity_amount if cls.is_equity else None,
        "maturities":     ", ".join(str(y) for y in maturities),
        "structure":      structure,
        "link": (
            f"{ARCHIVES_URL}/{cik}/{acc.replace('-','')}/"
            f"{acc}-index.htm"
        ),
    }


# ---------------------------------------------------------------------------
# Mode 1: Real-time feed (FAST)
# ---------------------------------------------------------------------------

def _poll_feed(form_type: str, count: int = 100) -> list[dict]:
    """
    Polls EDGAR's Atom feed for a single form type.
    Returns a list of {cik, accession, filed_at, form, items} dicts.
    This is the system-wide list of the most recently accepted filings --
    for S&P 500 + Nasdaq 100 companies checking every few minutes, the
    100-entry window is more than wide enough.
    """
    params = {
        "action": "getcurrent",
        "type":   form_type,
        "dateb":  "",
        "owner":  "include",
        "count":  count,
        "output": "atom",
    }
    try:
        resp = _get(FEED_URL, params=params)
    except Exception:
        return []

    soup = BeautifulSoup(resp.content, "xml")
    hits = []
    for entry in soup.find_all("entry"):
        link_tag = entry.find("link")
        href = link_tag["href"] if link_tag and link_tag.has_attr("href") else ""
        cik_m = _CIK_RE.search(href)
        acc_m = _ACC_RE.search(href)
        if not cik_m or not acc_m:
            continue
        updated = entry.updated.text[:10] if entry.updated else ""
        title   = entry.title.text if entry.title else ""
        # Form type is the first token of the title (e.g. "424B5 - COMPANY ...")
        detected_form = title.split(" - ")[0].strip() if " - " in title else form_type
        hits.append({
            "cik":       int(cik_m.group(1)),
            "accession": acc_m.group(1),
            "filed_at":  updated,
            "form":      detected_form,
            "items":     "",
            "link":      href,
        })
    return hits


def check_realtime(
    universe,
    seen_accessions: set,
    exclude_structured: bool = True,
    min_debt_amount: float | None = None,
    max_workers: int = 10,
) -> tuple[list[dict], set]:
    """
    Fast real-time check: polls the EDGAR feed for each debt-relevant form
    type, filters to universe companies, classifies matches only.

    Returns (new_results, updated_seen_accessions).
    Typically completes in 5-20 seconds.
    """
    from classify import classify, is_debt_filing

    cik_to_row = {int(r.cik): r for r in universe.itertuples()}
    cik_set    = set(cik_to_row.keys())

    # Collect all candidate hits from the feed (fast -- one request per form)
    candidates = []
    for form_type in DEBT_FORMS:
        for hit in _poll_feed(form_type):
            if hit["cik"] not in cik_set:
                continue
            if hit["accession"] in seen_accessions:
                continue
            candidates.append(hit)
            seen_accessions.add(hit["accession"])

    if not candidates:
        return [], seen_accessions

    # Classify only the matches (fetch text concurrently)
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_build_result_row, hit, cik_to_row): hit
            for hit in candidates
        }
        for future in as_completed(futures):
            row = future.result()
            if row:
                results.append(row)

    results.sort(key=lambda r: r["filed_at"], reverse=True)
    return results, seen_accessions


# ---------------------------------------------------------------------------
# Mode 2: Historical per-company scan (THOROUGH)
# ---------------------------------------------------------------------------

def _get_recent_filings(cik: int, since: date) -> list[dict]:
    try:
        data = _get(SUBMISSIONS_URL.format(cik=cik)).json()
    except Exception:
        return []

    recent     = data.get("filings", {}).get("recent", {})
    forms      = recent.get("form", [])
    dates      = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    items_list = recent.get("items", [""] * len(forms))
    since_str  = since.isoformat()

    hits = []
    for i in range(len(forms)):
        if dates[i] < since_str:
            break
        form = forms[i]
        if form not in DEBT_FORMS and not any(form.startswith(f) for f in DEBT_FORMS):
            continue
        hits.append({
            "cik":       cik,
            "form":      form,
            "filed_at":  dates[i],
            "accession": accessions[i],
            "items":     items_list[i] if i < len(items_list) else "",
        })
    return hits


def check_filings(
    universe,
    since: date,
    exclude_structured: bool = True,
    min_debt_amount: float | None = None,
    max_workers: int = 20,
    progress_callback=None,
) -> list[dict]:
    """
    Thorough historical scan: queries each company's submission history.
    ~600 requests, takes 2-3 minutes. Use for date-range catch-up.
    """
    cik_to_row = {int(r.cik): r for r in universe.itertuples()}
    results    = []
    completed  = 0
    total      = len(universe)

    def process_company(row):
        hits = _get_recent_filings(int(row.cik), since)
        out  = []
        for hit in hits:
            result = _build_result_row(hit, cik_to_row)
            if result:
                out.append(result)
        return out

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_company, row): row for row in universe.itertuples()}
        for future in as_completed(futures):
            results.extend(future.result())
            completed += 1
            if progress_callback:
                progress_callback(completed, total)

    results.sort(key=lambda r: r["filed_at"], reverse=True)
    return results
