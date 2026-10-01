"""Monthly S&P 500 snapshot. yfinance is optional metadata enrichment only."""
import io
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from sec_client import SecClient


def get_universe(data_dir='data', force=False, enrich=False, now=None):
    now = now or datetime.now(ZoneInfo('Europe/London'))
    path = Path(data_dir) / 'sp500.json'
    shared_path = Path(__file__).with_name('universe_snapshot.json')
    cache_path = path if path.exists() else shared_path
    cached = json.loads(cache_path.read_text()) if cache_path.exists() else None
    if cached and cached.get('companies') and any(not r.get('sector') for r in cached['companies']) and shared_path.exists():
        shared = json.loads(shared_path.read_text())
        if shared['month'] >= cached['month'] and all(r.get('sector') for r in shared['companies']):
            cached = shared
    month = now.strftime('%Y-%m')
    if cached and cached['month'] == month and not force:
        return cached, None
    try:
        response = requests.get('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
                                headers={'User-Agent': 'Mozilla/5.0 CorporateIssuanceMonitor'}, timeout=30)
        response.raise_for_status()
        tables = pd.read_html(io.StringIO(response.text))
        table = next(t for t in tables if {'Symbol', 'Security', 'CIK', 'GICS Sector'} <= set(t.columns))
        if not 490 <= len(table) <= 520:
            raise ValueError(f'Unexpected constituent count: {len(table)}')
        # Wikipedia includes CIKs; SEC mapping verifies issuer IDs.
        mapping = {str(v['ticker']).replace('.', '-').upper(): int(v['cik_str'])
                   for v in SecClient().get('https://www.sec.gov/files/company_tickers.json').json().values()}
        rows = []
        for _, r in table.iterrows():
            ticker = str(r['Symbol']).replace('.', '-').upper()
            cik = int(r['CIK'])
            if ticker in mapping and mapping[ticker] != cik:
                raise ValueError(f'Conflicting CIK for {ticker}; keeping previous universe')
            if cik <= 0:
                raise ValueError(f'Missing CIK for {ticker}')
            name = str(r['Security'])
            if enrich:
                try:
                    import yfinance as yf
                    name = yf.Ticker(ticker).get_info().get('longName') or name
                except Exception:
                    pass  # Names from the constituent source remain usable.
            rows.append({'ticker': ticker, 'company': name, 'cik': cik, 'sector': str(r['GICS Sector']).strip()})
        snapshot = {'month': month, 'updated_at': now.isoformat(),
                    'source': response.url, 'companies': rows}
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(snapshot, indent=2))
        os.replace(temp, path)
        return snapshot, None
    except Exception as exc:
        if cached:
            return cached, f'Universe refresh failed; using {cached["month"]} snapshot: {exc}'
        raise RuntimeError(f'Cannot load S&P 500 universe: {exc}') from exc
