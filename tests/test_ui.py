import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from streamlit.testing.v1 import AppTest


class InterfaceTests(unittest.TestCase):
    def test_app_load_and_empty_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict('os.environ', {'ISSUANCE_DATA_DIR': tmp}), \
                 patch('sp500.get_universe', return_value=(dict(companies=[dict(cik=1, ticker='A', company='Acme')], updated_at='2026-10-01'), None)), \
                 patch('sec_monitor.scan', return_value=dict(new=0, checked=0, errors=[])):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'CIapp.py')).run()
                self.assertEqual(len(app.exception), 0)
                next(b for b in app.button if b.label == 'Scan EDGAR now').click().run()
                self.assertEqual(len(app.exception), 0)
                self.assertTrue(any('0 new updates' in box.value for box in app.info))

    def test_financial_tabs_and_independent_size_filters(self):
        from filing_parser import parse_document
        def event(ticker, cik, size, deal):
            e = parse_document(f'Has priced offering of ${size:,} 5.2% senior notes due 2033.', '8-K')
            e.update(ticker=ticker, company=ticker, cik=cik, filed_date='2026-10-01',
                     accepted_at='', observed_at='2026-10-01', source_url='https://www.sec.gov/example', deal_id=deal)
            return e
        saved = [event('BANK_SMALL', 1, 500000000, 's'), event('BANK_BIG', 1, 2000000000, 'b'),
                 event('TECH', 2, 500000000, 't')]
        unknown = event('BANK_UNKNOWN', 1, 500000000, 'u')
        unknown.update(amount=None, tranches=[])
        saved.append(unknown)
        universe = dict(companies=[dict(cik=1, ticker='BANK', company='Bank', sector='Financials'),
                                   dict(cik=2, ticker='TECH', company='Tech', sector='Information Technology')],
                        updated_at='2026-10-01')
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict('os.environ', {'ISSUANCE_DATA_DIR': tmp}), \
             patch('sp500.get_universe', return_value=(universe, None)), \
             patch('storage.Store.events', return_value=saved):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'CIapp.py')).run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual([tab.label for tab in app.tabs], ['Non-financials', 'Financials'])
            self.assertEqual(len(app.tabs[0].code), 1)
            self.assertIn('TECH', app.tabs[0].code[0].value)
            self.assertEqual(len(app.tabs[1].code), 2)
            next(n for n in app.number_input if n.label.startswith('Financials:')).set_value(0).run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.tabs[1].code), 3)
            self.assertEqual(len(app.tabs[0].code), 1)
