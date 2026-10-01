import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import date, datetime

from filing_parser import parse_document
from headline_generator import filing_times, headline
from storage import Store
from sec_monitor import candidates, scan
from sp500 import get_universe


class ParserTests(unittest.TestCase):
    def test_lifecycle(self):
        for text, stage in [
            ('Acme announces proposed offering of senior notes.', 'ANNOUNCED'),
            ('Acme launches an offering of senior notes.', 'LAUNCHED'),
            ('Acme has priced an offering of senior notes.', 'PRICED')]:
            self.assertEqual(parse_document(text, '8-K')['stage'], stage)

    def test_non_deals(self):
        for text in [
            'We may offer debt securities including senior notes under this shelf.',
            'Item 2.03: We entered a revolving credit facility. Existing senior notes remain outstanding.',
            'Announces tender offer for senior notes.',
            'Announces offering of market-linked senior notes with contingent coupon.']:
            self.assertIsNone(parse_document(text, '8-K'))

    def test_tranches_and_currency(self):
        e = parse_document('Announces pricing of senior notes. $500 million 5.200% senior notes due 2033 '
                           'and $500 million 5.450% senior notes due 2036.', '8-K')
        self.assertEqual(e['amount'], 1e9)
        self.assertEqual(len(e['tranches']), 2)
        e.update(company='Xylem', ticker='XYL')
        self.assertIn('USD 1bln 2-part', headline(e))
        self.assertEqual(parse_document('Launches offering of €500 million senior notes.', '8-K')['currency'], 'EUR')

    def test_preliminary_not_priced(self):
        e = parse_document('Subject to completion. Preliminary pricing. Proposed offering of '
                           '$500 million 5.2% senior notes due 2033.', '424B5')
        self.assertEqual(e['stage'], 'ANNOUNCED')

    def test_sec_xylem_regression(self):
        fixtures = Path(__file__).parent / 'fixtures'
        priced = parse_document((fixtures / 'xylem_pricing.txt').read_text(), 'FWP')
        self.assertEqual(priced['stage'], 'PRICED')
        self.assertEqual(priced['amount'], 1.5e9)
        self.assertEqual([t['spread'] for t in priced['tranches']], ['+50 bps', '+65 bps', '+85 bps'])
        preliminary = parse_document((fixtures / 'xylem_preliminary.txt').read_text(), '424B5')
        self.assertEqual(preliminary['stage'], 'ANNOUNCED')
        self.assertIsNone(parse_document('We intend to use proceeds from previously issued senior notes.', '8-K'))

    def test_timezone(self):
        self.assertIn('13:00:00 BST', filing_times('2026-10-01T12:00:00Z'))
        self.assertIn('08:00:00 EDT', filing_times('2026-10-01T12:00:00Z'))
        self.assertIn('12:00:00 GMT', filing_times('2026-11-10T12:00:00Z'))


class StoreTests(unittest.TestCase):
    def test_persistence_stage_updates_and_distinct_deals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.db'
            s = Store(path)
            def event(stage, amount=5e8, day='2026-10-01'):
                e = parse_document(f'{"Launches" if stage == "LAUNCHED" else "Has priced"} offering of '
                                   f'${amount:,.0f} 5.2% senior notes due 2033.', '8-K')
                e.update(cik=1, filed_date=day, accepted_at='', company='Acme', ticker='A')
                return e
            self.assertEqual(s.save('one', [event('LAUNCHED')]), 1)
            self.assertEqual(s.save('two', [event('LAUNCHED')]), 0)
            self.assertEqual(s.save('three', [event('PRICED')]), 1)
            self.assertEqual(len({e['deal_id'] for e in s.events()}), 1)
            s.save('four', [event('PRICED', 7e8)])
            self.assertEqual(len({e['deal_id'] for e in s.events()}), 2)
            self.assertTrue(Store(path).seen('two'))

    def test_failed_filing_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.db')
            hit = dict(cik=1, accession='bad', form='8-K', filed_date='2026-10-01', accepted_at='', primary='a.htm')
            with patch('sec_monitor.candidates', return_value=[hit]), patch('sec_monitor.documents', side_effect=RuntimeError('timeout')):
                r = scan([dict(cik=1, ticker='A', company='Acme')], store, date(2026, 10, 1))
            self.assertEqual(len(r['errors']), 1)
            self.assertFalse(store.seen('bad'))


class UniverseTests(unittest.TestCase):
    def test_month_rollover_and_stale_fallback(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = dict(month='2026-09', companies=[], updated_at='2026-09-01')
            Path(tmp, 'sp500.json').write_text(json.dumps(snapshot))
            with patch('sp500.requests.get', side_effect=RuntimeError('offline')) as get:
                data, warning = get_universe(tmp, now=datetime(2026, 9, 20))
                get.assert_not_called()
                self.assertIsNone(warning)
                data, warning = get_universe(tmp, now=datetime(2026, 10, 1))
                self.assertIn('2026-09', warning)
                self.assertEqual(data, snapshot)


if __name__ == '__main__':
    unittest.main()
