"""Run: streamlit run CIapp.py"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from headline_generator import headline, filing_times
from sec_monitor import scan
from sp500 import get_universe
from storage import Store

st.set_page_config(page_title='Corporate Issuance', page_icon='📋', layout='wide')
st.title('Corporate Issuance')
st.caption('S&P 500 · SEC EDGAR · Announcement → Launch → Pricing')
data_dir = os.getenv('ISSUANCE_DATA_DIR', 'data')
store = Store(os.path.join(data_dir, 'issuance.sqlite3'))
now = datetime.now(ZoneInfo('Europe/London'))

with st.sidebar:
    st.subheader('Monitor')
    force = st.button('Refresh S&P 500 universe')
    enrich = st.checkbox('Enrich company names with yfinance', value=False,
                         help='Only during refresh. Index membership comes from the constituent list.')
    auto = st.checkbox('Auto-scan while this app is open')
    interval = st.selectbox('Scan every (minutes)', [5, 10, 15, 30], index=1)
    since = st.date_input('Scan filings since', value=now.date() - timedelta(days=7), max_value=now.date())
    stages = st.multiselect('Stages', ['ANNOUNCED', 'LAUNCHED', 'PRICED'], default=['ANNOUNCED', 'LAUNCHED', 'PRICED'])
    min_amount = st.number_input('Minimum known USD size (mln)', min_value=0, value=0, step=50)
    include_unknown = st.checkbox('Include deals with undisclosed size', value=True)
    query = st.text_input('Company or ticker')
    st.caption('Universe refreshes on the first scan/open of each month. Auto-scan needs an active app session.')

try:
    universe, warning = get_universe(data_dir, force=force, enrich=enrich)
except Exception as exc:
    st.error(str(exc))
    st.stop()
if warning:
    st.warning(warning)
st.caption(f'{len(universe["companies"])} securities · Universe updated {universe["updated_at"][:10]}')
refresh_count = st_autorefresh(interval=interval * 60000, key='scan_tick') if auto else None
run = st.button('Scan EDGAR now', type='primary')
if run or (auto and st.session_state.get('last_tick') != refresh_count):
    st.session_state['last_tick'] = refresh_count
    bar = st.progress(0, text='Checking SEC submissions…')
    report = scan(universe['companies'], store, since,
                  progress=lambda done, total: bar.progress(done / total, text=f'Checked {done}/{total} issuers'))
    bar.empty()
    st.session_state['report'] = report
    st.session_state['last_scan'] = now.isoformat()

if 'report' in st.session_state:
    report = st.session_state['report']
    st.info(f'Last scan: {st.session_state["last_scan"]} · {report["new"]} new updates · {report["checked"]} newly processed filings')
    if report['errors']:
        st.warning(f'{len(report["errors"])} scan failures. This scan is incomplete; failed filings remain eligible for retry.')
        with st.expander('Scan errors'):
            st.text('\n'.join(report['errors']))

all_events = store.events()
events = [e for e in all_events if e['stage'] in stages
          and (not query or query.lower() in (e['company'] + ' ' + e['ticker']).lower())
          and (include_unknown or e['amount'] is not None)
          and (e['amount'] is None or e['currency'] != 'USD' or e['amount'] >= min_amount * 1e6)]
events.sort(key=lambda e: (e['filed_date'], e['accepted_at'], e['observed_at']), reverse=True)
if not events:
    st.info('No matching issuance updates. Run a scan or adjust the filters.')
else:
    rows = [{'headline': headline(e), 'stage': e['stage'], 'filed_date': e['filed_date'],
             'accepted_at': e['accepted_at'], 'observed_at': e['observed_at'], 'source': e['source_url']}
            for e in events]
    st.download_button('Download headlines CSV', pd.DataFrame(rows).to_csv(index=False), 'issuance.csv', 'text/csv')
    for event in events:
        with st.container(border=True):
            st.code(headline(event), language=None)  # Native copy button
            st.caption('SEC acceptance: ' + filing_times(event['accepted_at']))
            st.link_button('Open SEC source', event['source_url'])
            with st.expander('Evidence and deal timeline'):
                st.write(event['evidence'])
                if event['use_of_proceeds']:
                    st.write(event['use_of_proceeds'])
                if event.get('bookrunners'):
                    st.write('Bookrunners: ' + event['bookrunners'])
                if event['tranches']:
                    st.dataframe(pd.DataFrame(event['tranches']), hide_index=True)
                if event['stage'] == 'PRICED' and not event['terms_complete']:
                    st.caption('Pricing announcement found; full tranche terms were not extracted. Check the source.')
                timeline = [e for e in all_events if e['deal_id'] == event['deal_id']]
                for e in sorted(timeline, key=lambda x: (x['filed_date'], x['accepted_at'])):
                    st.write(f'{e["stage"]} · {filing_times(e["accepted_at"])}')
                st.caption('Times show SEC acceptance, not the exact market announcement/launch time. '
                           'Deals link automatically only when the securities and size match unambiguously.')
