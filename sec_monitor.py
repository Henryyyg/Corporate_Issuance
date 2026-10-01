"""EDGAR-first scanner: submissions discovery, complete documents and 8-K exhibits."""
from datetime import date, datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed
from queue import Queue, Empty
import time
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from filing_parser import parse_document, STAGES
from sec_client import SecClient

FORMS = {'8-K', '8-K/A', '424B2', '424B3', '424B5', 'FWP'}
ARCHIVES = 'https://www.sec.gov/Archives/edgar/data'


def latest_candidates(client, issuers, since):
    """A bounded current-filings feed check, not a historical completeness scan."""
    def fetch_form(form):
        hits, errors, warnings = {}, [], []
        try:
            response = client.get('https://www.sec.gov/cgi-bin/browse-edgar', params={
                'action': 'getcurrent', 'type': form, 'owner': 'include',
                'count': 100, 'output': 'atom'})
            soup = BeautifulSoup(response.text, 'xml')
            if soup.find('feed') is None:
                raise ValueError('SEC did not return an Atom feed')
            entries = soup.find_all('entry')
            if len(entries) >= 100:
                warnings.append(f'{form}: latest 100 filings only; use Full catch-up for earlier filings')
            for entry in entries:
                link = entry.find('link', href=True)
                url = link['href'] if link else ''
                cik = re.search(r'/data/(\d+)/', url)
                accession = re.search(r'(\d{10}-\d{2}-\d{6})-index', url)
                if not cik or not accession or int(cik[1]) not in issuers:
                    continue
                detected = entry.find('title').get_text().split(' - ')[0].strip()
                if detected not in FORMS:
                    continue
                updated = entry.find('updated')
                filed = updated.get_text()[:10] if updated else date.today().isoformat()
                if filed < since.isoformat():
                    continue
                hits[accession[1]] = {'cik': int(cik[1]), 'accession': accession[1],
                    'form': detected, 'filed_date': filed, 'accepted_at': '', 'primary': ''}
        except Exception as exc:
            errors.append(f'{form}: latest-filings feed failed: {exc}')
        return hits, errors, warnings
    hits, errors, warnings = {}, [], []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch_form, form)
                   for form in ('8-K', '424B2', '424B3', '424B5', 'FWP')]
        for future in as_completed(futures):
            found, failed, limits = future.result()
            hits.update(found)
            errors.extend(failed)
            warnings.extend(limits)
    return list(hits.values()), errors, warnings


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
    # Supplements have no 8-K release exhibits to discover. Use the exact
    # primary document from submissions rather than fetching its index first.
    if not hit['form'].startswith('8-K') and hit['primary']:
        url = base + hit['primary']
        return [(url, client.get(url).text)]
    index = base + hit['accession'] + '-index.htm'
    soup = BeautifulSoup(client.get(index).text, 'html.parser')
    if not hit['accepted_at']:
        for label in soup.select('.infoHead'):
            if label.get_text(strip=True) == 'Accepted':
                value = label.find_next_sibling(class_='info')
                if value:
                    try:
                        accepted = datetime.fromisoformat(value.get_text(strip=True))
                        hit['accepted_at'] = accepted.replace(tzinfo=ZoneInfo('America/New_York')).isoformat()
                    except ValueError:
                        pass
                break
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


def scan(universe, store, since, progress=None, client=None, activity=None,
         issuer_budget=45, max_workers=6, mode='full'):
    client = client or SecClient()
    errors, new, checked = [], 0, 0
    issuers = {}
    for company in universe:
        issuers.setdefault(int(company['cik']), company)  # e.g. GOOG/GOOGL share a CIK
    warnings = []
    live_hits = None
    if mode == 'live':
        if activity:
            activity('Checking five EDGAR latest-filings feeds…')
        hits, feed_errors, warnings = latest_candidates(client, issuers, since)
        errors.extend(feed_errors)
        live_hits = {}
        for hit in hits:
            if not store.seen(hit['accession']):
                live_hits.setdefault(hit['cik'], []).append(hit)
        issuers = {cik: company for cik, company in issuers.items() if cik in live_hits}
    messages = Queue()

    def process(cik, company):
        started = time.monotonic()
        local_errors, local_new, local_checked = [], 0, 0
        messages.put(f'{company["ticker"]}: checking submissions')
        try:
            hits = candidates(client, cik, since) if live_hits is None else live_hits[cik]
        except Exception as exc:
            return 0, 0, [f'{company["ticker"]}: submissions failed: {exc}']
        pending = [h for h in hits if not store.seen(h['accession'])]
        for number, hit in enumerate(pending, 1):
            if time.monotonic() - started >= issuer_budget:
                local_errors.append(f'{company["ticker"]}: time budget reached; '
                                    f'{len(pending) - number + 1} filings remain for the next scan')
                break
            messages.put(f'{company["ticker"]}: reading {hit["form"]} '
                         f'{number}/{len(pending)} · {hit["accession"]}')
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
                local_new += store.save(hit['accession'], list(unique.values()))
                local_checked += 1
            except Exception as exc:
                local_errors.append(f'{company["ticker"]} {hit["accession"]}: {exc}')
        return local_new, local_checked, local_errors

    # Only the main Streamlit thread updates widgets. Workers send activity
    # through a queue; slow banks cannot block every other issuer's progress.
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {executor.submit(process, cik, company): company
                   for cik, company in issuers.items()}
        done_count = 0
        while pending:
            completed, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            while True:
                try:
                    message = messages.get_nowait()
                except Empty:
                    break
                if activity:
                    activity(message)
            for future in completed:
                company = pending.pop(future)
                try:
                    found, processed, failures = future.result()
                    new += found
                    checked += processed
                    errors.extend(failures)
                except Exception as exc:
                    errors.append(f'{company["ticker"]}: scan failed: {exc}')
                done_count += 1
                if progress:
                    progress(done_count, len(issuers))
    return {'new': new, 'checked': checked, 'errors': errors, 'warnings': warnings}
