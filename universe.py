"""
universe.py

Builds the company universe from S&P 500 and Nasdaq 100 constituents,
pulled from Wikipedia and mapped to SEC CIK numbers.

Results are cached to disk for 24h so the app doesn't re-fetch on every load.
"""

from __future__ import annotations

import io
import os
import time

import pandas as pd
import requests

CACHE_PATH = "data/universe_cache.csv"
CACHE_MAX_AGE_HOURS = 24

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_URL   = "https://en.wikipedia.org/wiki/Nasdaq-100"

# SEC requires a descriptive User-Agent
SEC_HEADERS = {"User-Agent": "sec-debt-monitor contact@example.com"}

# Wikipedia blocks obvious bot agents -- use a browser-like string
WIKI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_sp500_tickers() -> set[str]:
    resp = requests.get(SP500_URL, headers=WIKI_HEADERS, timeout=20)
    resp.raise_for_status()
    # io.StringIO wrapper required by newer pandas versions
    tables = pd.read_html(io.StringIO(resp.text))
    df  = tables[0]
    col = next(c for c in df.columns if "symbol" in str(c).lower() or "ticker" in str(c).lower())
    return set(df[col].astype(str).str.replace(".", "-", regex=False).str.upper())


def _fetch_ndx_tickers() -> set[str]:
    resp = requests.get(NDX_URL, headers=WIKI_HEADERS, timeout=20)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    for df in tables:
        # Flatten MultiIndex columns that pandas sometimes creates from
        # Wikipedia tables with merged header rows
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [" ".join(str(c) for c in col).strip() for col in df.columns]
        else:
            df.columns = [str(c) for c in df.columns]
        if any("ticker" in c.lower() or "symbol" in c.lower() for c in df.columns):
            col = next(c for c in df.columns if "ticker" in c.lower() or "symbol" in c.lower())
            return set(df[col].astype(str).str.upper().str.strip())
    return set()


def _fetch_cik_map() -> dict[str, dict]:
    resp = requests.get(SEC_TICKERS_URL, headers=SEC_HEADERS, timeout=20)
    resp.raise_for_status()
    raw = resp.json()
    return {
        row["ticker"].upper(): {"cik": int(row["cik_str"]), "name": row["title"]}
        for _, row in raw.items()
    }


def build_universe() -> pd.DataFrame:
    sp500 = _fetch_sp500_tickers()
    ndx   = _fetch_ndx_tickers()
    all_tickers = sp500 | ndx
    cik_map = _fetch_cik_map()

    rows = []
    for ticker in sorted(all_tickers):
        info = cik_map.get(ticker)
        if not info:
            continue
        rows.append({
            "ticker": ticker,
            "name":   info["name"],
            "cik":    info["cik"],
            "index": (
                "S&P 500 + Nasdaq 100" if ticker in sp500 and ticker in ndx
                else "S&P 500" if ticker in sp500
                else "Nasdaq 100"
            ),
        })

    return pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)


def get_universe(force_refresh: bool = False) -> pd.DataFrame:
    if not force_refresh and os.path.exists(CACHE_PATH):
        age_hours = (time.time() - os.path.getmtime(CACHE_PATH)) / 3600
        if age_hours < CACHE_MAX_AGE_HOURS:
            return pd.read_csv(CACHE_PATH)

    df = build_universe()
    if not df.empty:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        df.to_csv(CACHE_PATH, index=False)
    return df
