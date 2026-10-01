from datetime import datetime
from zoneinfo import ZoneInfo


def money(amount, ccy='USD'):
    return f'{ccy or "USD"} {amount / (1e9 if amount >= 1e9 else 1e6):g}{"bln" if amount >= 1e9 else "mln"}'


def headline(event):
    verb = {'ANNOUNCED': 'announces', 'LAUNCHED': 'launches', 'PRICED': 'prices'}[event['stage']]
    size = money(event['amount'], event['currency']) + ' ' if event['amount'] is not None else ''
    tranches = event['tranches']
    parts = f'{len(tranches)}-part ' if len(tranches) > 1 else ''
    result = f'{event["stage"]}: {event["company"]} ({event["ticker"]}) {verb} {size}{parts}notes offering'
    if tranches:
        terms = []
        for t in tranches:
            rate = 'floating-rate ' if t['floating'] else f'{t["coupon"]} ' if t['coupon'] else ''
            spread = f' at T{t["spread"].replace(" ", "")}' if t.get('spread') else ''
            terms.append(f'{money(t["amount"], t["currency"])} {rate}{t["maturity"]}s{spread}')
        result += '; ' + ', '.join(terms)
    return result + '.'


def filing_times(accepted):
    if not accepted:
        return 'Acceptance time unavailable'
    dt = datetime.fromisoformat(accepted.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        return f'{accepted} (timezone unavailable)'
    return ' / '.join(dt.astimezone(ZoneInfo(tz)).strftime('%d %b %Y %H:%M:%S %Z')
                      for tz in ('America/New_York', 'Europe/London'))
