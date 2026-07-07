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
_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]+|#\d+);")  # handles &nbsp; AND &#160; etc.
_WS_RE     = re.compile(r"\s+")
_CIK_RE    = re.compile(r"/data/(\d+)/")
_ACC_RE    = re.compile(r"/(\d{10}-\d{2}-\d{6})-index\.htm")


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


def _fetch_filing_text(cik: int, accession_with_dashes: str, max_bytes: int = 150_000) -> str:
    """Streams the first 150KB of a filing's submission text and strips HTML tags."""
    acc_no_dashes = accession_with_dashes.replace("-", "")
    url = f"{ARCHIVES_URL}/{cik}/{acc_no_dashes}/{accession_with_dashes}.txt"
    try:
        with requests.get(url, headers=HEADERS, timeout=20, stream=True) as r:
            r.raise_for_status()
            chunks, total = [], 0
            for chunk in r.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
        raw  = b"".join(chunks).decode("utf-8", errors="ignore")
        text = _TAG_RE.sub(" ", raw)
        text = _ENTITY_RE.sub(" ", text)
        return _WS_RE.sub(" ", text).strip()
    except requests.RequestException:
        return ""


def _build_result_row(hit: dict, cik_to_row: dict) -> dict | None:
    """Classifies a filing hit and returns a result row, or None if not relevant."""
    from classify import classify, is_debt_filing
    cik = hit["cik"]
    text = _fetch_filing_text(cik, hit["accession"])
    cls  = classify(hit["form"], hit["items"], text)
    if not is_debt_filing(cls):
        return None
    row = cik_to_row.get(cik, None)
    acc = hit["accession"]
    return {
        "filed_at":       hit["filed_at"],
        "ticker":         row.ticker if row else "",
        "company":        row.name   if row else hit.get("company", ""),
        "form":           hit["form"],
        "classification": "Debt + Equity" if cls.is_debt and cls.is_equity else "Debt",
        "currency":       cls.currency,
        "debt_size":      cls.debt_amount,
        "equity_size":    cls.equity_amount if cls.is_equity else None,
        "maturities":     ", ".join(str(y) for y in cls.maturity_years),
        "structure":      cls.structure,
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
