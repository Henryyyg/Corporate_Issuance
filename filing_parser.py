"""Conservative evidence-based bond offering parser; no inferred launch times."""
import re
from bs4 import BeautifulSoup

STAGES = {'ANNOUNCED': 1, 'LAUNCHED': 2, 'PRICED': 3}
DEBT = r'(?:senior\s+(?:secured\s+|unsecured\s+)?|subordinated\s+|convertible\s+|floating\s+rate\s+)?(?:notes?|bonds?|debentures?)'
AMOUNT = r'(?P<ccy>US\$|USD|\$|EUR|€|GBP|£)\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>billion|million|bln|mln)?'
TRANCHE = re.compile(AMOUNT + r'\s*(?:aggregate principal amount\s*)?(?:of\s*)?(?P<coupon>\d+(?:\.\d+)?\s*%\s*)?' + DEBT + r'\s+due\s+(?:[A-Za-z]+\s+\d{1,2},?\s+)?(?P<year>20\d{2})', re.I)
STRUCTURED = re.compile(r'autocallable|auto-callable|market.linked|index.linked|contingent coupon|barrier event|reference asset|underlying stock|principal at risk', re.I)


def visible_text(html):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(['script', 'style', 'ix:header']):
        node.decompose()
    return re.sub(r'\s+', ' ', soup.get_text(' ', strip=True))


def amount_value(match):
    value = float(match['amount'].replace(',', ''))
    unit = (match['unit'] or '').lower()
    return value * (1e9 if unit in ('billion', 'bln') else 1e6 if unit in ('million', 'mln') else 1)


def currency(match):
    return {'$': 'USD', 'US$': 'USD', '€': 'EUR', '£': 'GBP'}.get(match['ccy'].upper(), match['ccy'].upper())


def table_tranches(html):
    """Read labelled final/pricing tables with one column per series."""
    result = []
    soup = BeautifulSoup(html, 'html.parser')
    for table in soup.find_all('table'):
        fields = {}
        for row in table.find_all('tr', recursive=True):
            cells = [c.get_text(' ', strip=True) for c in row.find_all(['td', 'th'], recursive=False)]
            if len(cells) < 2:
                continue
            label = re.sub(r'[^a-z ]', '', cells[0].lower()).strip()
            if label in ('title', 'security', 'securities', 'title of securities', 'notes',
                         'principal amount', 'aggregate principal amount', 'size',
                         'coupon', 'interest rate', 'maturity', 'maturity date',
                         'yield', 'yield to maturity', 'spread to benchmark treasury',
                         'treasury spread', 'initial price talk'):
                fields[label] = [c for c in cells[1:] if c]
        titles = next((fields[k] for k in ('title', 'security', 'securities', 'title of securities', 'notes') if k in fields), [])
        sizes = next((fields[k] for k in ('principal amount', 'aggregate principal amount', 'size') if k in fields), [])
        if not titles or len(sizes) != len(titles):
            continue
        for i, (title, size) in enumerate(zip(titles, sizes)):
            match = re.search(AMOUNT, size, re.I)
            year = re.search(r'\b20\d{2}\b', title)
            if not match or not year or not re.search(DEBT, title, re.I):
                continue
            rate = re.search(r'\d+(?:\.\d+)?\s*%', title)
            def field(*keys):
                values = next((fields[k] for k in keys if k in fields), [])
                return values[i] if len(values) == len(titles) else None
            tranche = {'currency': currency(match), 'amount': amount_value(match),
                       'coupon': rate.group().replace(' ', '') if rate else field('coupon', 'interest rate'),
                       'maturity': int(year.group()), 'floating': 'floating' in title.lower()}
            for key, labels in [('yield', ('yield', 'yield to maturity')),
                                ('spread', ('spread to benchmark treasury', 'treasury spread')),
                                ('initial_price_talk', ('initial price talk',))]:
                value = field(*labels)
                if value:
                    tranche[key] = value
            if tranche not in result:
                result.append(tranche)
    return result


def parse_document(html, form):
    text = visible_text(html)
    # Cover/current announcement first. Avoid historic debt in long risk sections.
    cover = text[:24000]
    if STRUCTURED.search(cover):
        return None
    if not re.search(DEBT, cover, re.I):
        return None
    if re.search(r'exchange offer|offer to exchange|tender offer|cash tender|redemption notice', cover[:5000], re.I):
        return None
    preliminary = bool(re.search(r'subject to completion|preliminary pricing', cover[:5000], re.I))
    if form != 'FWP':
        preliminary = preliminary or bool(re.search(r'preliminary prospectus', cover[:5000], re.I))
    patterns = [
        ('PRICED', r'(?:announces?\s+(?:the\s+)?pricing|has\s+priced|priced|pricing\s+of)\b.{0,240}(?:offering|' + DEBT + r')'),
        ('LAUNCHED', r'(?:launch(?:es|ed)?|commenced|commences)\b.{0,240}(?:offering|' + DEBT + r')'),
        ('ANNOUNCED', r'(?:proposed\s+(?:public\s+|private\s+)?offering\s+of|(?:intends?|plans?)\s+to\s+(?:offer|issue)\b|announces?\s+(?:a\s+|an\s+|its\s+)?(?:proposed\s+)?(?:public\s+|private\s+)?offering\s+of)\s*.{0,160}?' + DEBT),
    ]
    stage, evidence = None, None
    for label, pattern in patterns:
        match = re.search(pattern, cover, re.I)
        if match and not (label == 'PRICED' and preliminary):
            stage, evidence = label, match.group(0)
            break
    # Specific new securities on a supplement cover support offering, not launch.
    tranches = []
    for match in TRANCHE.finditer(cover[:10000]):
        context = cover[max(0, match.start()-120):match.start()].lower()
        if re.search(r'outstanding|previously issued|repay|redeem|existing', context):
            continue
        tranche = {'currency': currency(match), 'amount': amount_value(match),
                   'coupon': (match['coupon'] or '').strip().replace(' ', '') or None,
                   'maturity': int(match['year']),
                   'floating': bool(re.search(r'floating\s+rate', match.group(), re.I))}
        if tranche not in tranches:
            tranches.append(tranche)
    if not tranches:
        tranches = table_tranches(html)
    # Vertical term sheets often put one labelled section after each series title.
    sections = re.split(r'Terms Applicable to (?:the|each)\s+', cover, flags=re.I)
    for section in sections[1:]:
        title = re.match(r'.{0,150}?notes?\s+due\s+(20\d{2})', section, re.I)
        if not title:
            continue
        matches = [t for t in tranches if t['maturity'] == int(title[1])]
        if len(matches) != 1:
            continue
        tranche = matches[0]
        for key, pattern in [
            ('spread', r'Spread to Benchmark Treasury\s*:\s*([+-]?\s*\d+(?:\.\d+)?\s*bps)'),
            ('yield', r'Yield to Maturity\s*:\s*(\d+(?:\.\d+)?\s*%)'),
            ('issue_price', r'Public Offering Price\s*:\s*(\d+(?:\.\d+)?\s*%)'),
            ('initial_price_talk', r'Initial Price Talk\s*:\s*([+-]?\s*\d+(?:\.\d+)?\s*bps)')]:
            match = re.search(pattern, section, re.I)
            if match:
                tranche[key] = match[1]
    if tranches and form in ('FWP', '424B2', '424B3', '424B5') and not preliminary and re.search(
        r'final (?:pricing|terms)|pricing (?:term sheet|terms)|price to (?:the )?public|public offering price', cover[:10000], re.I):
        stage, evidence = 'PRICED', 'Final pricing term sheet with specific offered securities.'
    if stage is None and tranches and form in ('424B2', '424B3', '424B5', 'FWP'):
        stage = 'ANNOUNCED'
        evidence = 'Specific notes offered in prospectus supplement; launch not stated.'
    if stage is None and preliminary and form in ('424B2', '424B3', '424B5') and re.search(
        r'(?:we are offering|offering of).{0,180}' + DEBT, cover[:8000], re.I):
        stage, evidence = 'ANNOUNCED', 'Preliminary prospectus describes a specific notes offering; launch not stated.'
    if stage is None:
        return None  # A shelf/indenture/credit facility alone is not a new bond.
    # Never upgrade from coupon presence alone: reoffers can list old coupons.
    total, ccy = None, None
    if tranches and len({t['currency'] for t in tranches}) == 1:
        total = sum(t['amount'] for t in tranches)
        ccy = tranches[0]['currency']
    if not tranches:
        for match in re.finditer(AMOUNT + r'\s*(?:aggregate principal amount\s*)?(?:of\s*)?(?:an?\s+)?(?:offering\s+of\s+)?' + DEBT, cover, re.I):
            if amount_value(match) >= 1e6:
                total, ccy = amount_value(match), currency(match)
                break
    purpose = re.search(r'(?:use|uses|intend\w* to use|plans? to use)\s+(?:the\s+)?(?:net\s+)?proceeds[^.]{0,350}', cover, re.I)
    managers = re.search(r'Joint Book[- ]Running Managers\s*:?\s*(.{1,500}?)(?:Co-Managers|Terms Applicable|The date of|Table of Contents)', cover, re.I)
    return {'stage': stage, 'evidence': evidence, 'amount': total, 'currency': ccy,
            'tranches': tranches, 'use_of_proceeds': purpose.group() if purpose else None,
            'bookrunners': managers[1].strip() if managers else None,
            'terms_complete': bool(tranches and all(t['coupon'] or t['floating'] for t in tranches))}
