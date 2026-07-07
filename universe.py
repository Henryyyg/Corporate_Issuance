"""
universe.py

Builds the company universe from S&P 500 and Nasdaq 100 constituents,
pulled directly from Wikipedia and mapped to SEC CIK numbers.

No yfinance or market cap lookups required -- the indices themselves are the
filter. Wikipedia tables update within days of index changes, which is precise
enough for this use case. Results are cached to disk for 24h so the app
doesn't re-fetch on every page load.
"""

from __future__ import annotations

import os
import time

import pandas as pd
import requests

CACHE_PATH = "data/universe_cache.csv"
CACHE_MAX_AGE_HOURS = 24

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

HEADERS = {"User-Agent": "sec-debt-monitor contact@example.com"}


def _fetch_sp500_tickers() -> set[str]:
    resp = requests.get(SP500_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    tables = pd.read_html(resp.text)
    df = tables[0]
    col = next(c for c in df.columns if "symbol" in c.lower() or "ticker" in c.lower())
    return set(df[col].str.replace(".", "-", regex=False).str.upper())


def _fetch_ndx_tickers() -> set[str]:
    resp = requests.get(NDX_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    tables = pd.read_html(resp.text)
    for df in tables:
        # Flatten MultiIndex columns (pandas sometimes creates these from
        # Wikipedia tables with merged header rows) to plain strings
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [" ".join(str(c) for c in col).strip() for col in df.columns]
        else:
            df.columns = [str(c) for c in df.columns]
        cols_lower = [c.lower() for c in df.columns]
        if any("ticker" in c or "symbol" in c for c in cols_lower):
            col = next(c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower())
            return set(df[col].astype(str).str.upper().str.strip())
    return set()


def _fetch_cik_map() -> dict[str, dict]:
    """Returns {TICKER: {cik: int, name: str}} from SEC's official file."""
    resp = requests.get(SEC_TICKERS_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    raw = resp.json()
    return {
        row["ticker"].upper(): {"cik": int(row["cik_str"]), "name": row["title"]}
        for _, row in raw.items()
    }


def build_universe() -> pd.DataFrame:
    """Fetches S&P 500 + Nasdaq 100 tickers and maps each to a SEC CIK."""
    sp500 = _fetch_sp500_tickers()
    ndx = _fetch_ndx_tickers()
    all_tickers = sp500 | ndx

    cik_map = _fetch_cik_map()

    rows = []
    for ticker in sorted(all_tickers):
        info = cik_map.get(ticker)
        if not info:
            continue
        rows.append({
            "ticker": ticker,
            "name": info["name"],
            "cik": info["cik"],
            "index": (
                "S&P 500 + Nasdaq 100" if ticker in sp500 and ticker in ndx
                else "S&P 500" if ticker in sp500
                else "Nasdaq 100"
            ),
        })

    return pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)


def get_universe(force_refresh: bool = False) -> pd.DataFrame:
    """Returns the cached universe, refreshing if stale or forced."""
    if not force_refresh and os.path.exists(CACHE_PATH):
        age_hours = (time.time() - os.path.getmtime(CACHE_PATH)) / 3600
        if age_hours < CACHE_MAX_AGE_HOURS:
            return pd.read_csv(CACHE_PATH)

    df = build_universe()
    if not df.empty:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        df.to_csv(CACHE_PATH, index=False)
    return df