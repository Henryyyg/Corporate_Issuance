"""EDGAR-first scanner: submissions discovery, complete documents and 8-K exhibits."""
from datetime import date
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from filing_parser import parse_document, STAGES
from sec_client import SecClient

FORMS = {'8-K', '8-K/A', '424B2', '424B3', '424B5', 'FWP'}
ARCHIVES = 'https://www.sec.gov/Archives/edgar/data'


def candidates(client, cik, since):
    data = client.get(f'https://data.sec.gov/submissions/CIK{cik:010d}.json').json()
    filings = data['filings']
    blocks = [filings['recent']]
    for archive in filings.get('files', []):
        if archive['filingTo'] >= since.isoformat():
            blocks.append(client.get('https://data.sec.gov/submissions/' + archive['name']).json())
    hits = {}
    for block in blocks:
        for i, form in enumerate(block.get('form', [])):
            filed = block['filingDate'][i]
            if form not in FORMS or filed < since.isoformat():
                continue
            def value(key):
                values = block.get(key, [])
                return values[i] if i < len(values) else ''
            acc = value('accessionNumber')
            hits[acc] = {'cik': cik, 'accession': acc, 'form': form,
                         'filed_date': filed, 'accepted_at': value('acceptanceDateTime'),
                         'primary': value('primaryDocument')}
    return sorted(hits.values(), key=lambda h: (h['filed_date'], h['accepted_at'], h['accession']))


def documents(client, hit):
    base = f'{ARCHIVES}/{hit["cik"]}/{hit["accession"].replace("-", "")}/'
    index = base + hit['accession'] + '-index.htm'
    soup = BeautifulSoup(client.get(index).text, 'html.parser')
    links = []
    for row in soup.select('table.tableFile tr'):
        cells = row.find_all('td')
        if len(cells) < 4:
            continue
        kind = cells[3].get_text(strip=True).upper()
        link = cells[2].find('a', href=True)
        if not link or not (kind == hit['form'] or (hit['form'].startswith('8-K') and kind.startswith(('EX-99', 'EX-1.1')))):
            continue
        url = urljoin(index, link['href'])
        # SEC viewer links sometimes wrap the actual document.
        if '/ix?doc=' in url or '/ixviewer/doc/action?doc=' in url:
            url = 'https://www.sec.gov' + url.split('doc=', 1)[1]
        parsed = urlparse(url)
        if parsed.hostname != 'www.sec.gov' or not parsed.path.startswith('/Archives/'):
            raise ValueError('Unexpected filing document URL')
        if not parsed.path.lower().endswith(('.htm', '.html', '.txt')):
            raise ValueError(f'Unsupported required exhibit format: {url}; filing will be retried')
        if url not in links:
            links.append(url)
    if not links and hit['primary']:
        links = [base + hit['primary']]
    if not links:
        raise ValueError('No primary filing document found')
    # No truncation. Failure in any required exhibit leaves accession retryable.
    return [(url, client.get(url).text) for url in links]


def scan(universe, store, since, progress=None, client=None):
    client = client or SecClient()
    errors, new, checked = [], 0, 0
    issuers = {}
    for company in universe:
        issuers.setdefault(int(company['cik']), company)  # e.g. GOOG/GOOGL share a CIK
    for i, (cik, company) in enumerate(issuers.items(), 1):
        try:
            hits = candidates(client, cik, since)
        except Exception as exc:
            errors.append(f'{company["ticker"]}: submissions failed: {exc}')
            if progress:
                progress(i, len(issuers))
            continue
        for hit in hits:
            if store.seen(hit['accession']):
                continue
            try:
                parsed = []
                for url, html in documents(client, hit):
                    event = parse_document(html, hit['form'])
                    if event:
                        event.update(hit)
                        event.update(company)
                        event['source_url'] = url
                        parsed.append(event)
                # Primary/exhibit duplication within an accession: keep strongest
                # evidence for identical securities, but retain distinct deals.
                unique = {}
                for event in parsed:
                    key = (event['currency'], event['amount'], str(event['tranches']))
                    if key not in unique or STAGES[event['stage']] > STAGES[unique[key]['stage']]:
                        unique[key] = event
                new += store.save(hit['accession'], list(unique.values()))
                checked += 1
            except Exception as exc:
                errors.append(f'{company["ticker"]} {hit["accession"]}: {exc}')
        if progress:
            progress(i, len(issuers))
    return {'new': new, 'checked': checked, 'errors': errors}
