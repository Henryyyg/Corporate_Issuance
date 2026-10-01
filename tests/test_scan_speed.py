import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from sec_monitor import scan, latest_candidates, documents
from sec_client import SecClient
from storage import Store


class SpeedTests(unittest.TestCase):
    def test_live_avoids_issuer_submissions_and_reads_only_unseen_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.db')
            hit = dict(cik=1, accession='new', form='8-K', filed_date='2026-10-01', accepted_at='', primary='a.htm')
            companies = [dict(cik=i, ticker=str(i), company='Company') for i in range(1, 501)]
            with patch('sec_monitor.latest_candidates', return_value=([hit], [], [])), \
                 patch('sec_monitor.candidates') as submissions, \
                 patch('sec_monitor.documents', return_value=[('https://www.sec.gov/a.htm', 'Nothing relevant')]) as docs:
                scan(companies, store, date(2026, 10, 1), mode='live')
                submissions.assert_not_called()
                self.assertEqual(docs.call_count, 1)
                scan(companies, store, date(2026, 10, 1), mode='live')
                self.assertEqual(docs.call_count, 1)

    def test_slow_issuer_does_not_block_progress_for_others(self):
        released = threading.Event()
        def discover(client, cik, since):
            if cik == 1:
                if not released.wait(3):
                    raise AssertionError('Other issuer could not progress')
            return []
        with tempfile.TemporaryDirectory() as tmp, patch('sec_monitor.candidates', side_effect=discover):
            report = scan([dict(cik=i, ticker=str(i), company='Company') for i in (1, 2)],
                          Store(Path(tmp) / 'state.db'), date(2026, 10, 1),
                          progress=lambda done, total: released.set())
            self.assertEqual(report['errors'], [])

    def test_issuer_budget_leaves_filings_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.db')
            hit = dict(cik=1, accession='new', form='8-K', filed_date='2026-10-01', accepted_at='', primary='a.htm')
            with patch('sec_monitor.candidates', return_value=[hit]), patch('sec_monitor.documents') as docs:
                report = scan([dict(cik=1, ticker='A', company='Company')], store, date(2026, 10, 1), issuer_budget=0)
                docs.assert_not_called()
                self.assertIn('1 filings remain', report['errors'][0])
                self.assertFalse(store.seen('new'))

    def test_feed_filters_universe_and_forms(self):
        xml = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>8-K - ACME</title><updated>2026-10-01T12:00:00Z</updated>
          <link href="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/0000000001-26-000001-index.htm"/>
          </entry></feed>'''
        client = Mock()
        client.get.return_value = SimpleNamespace(text=xml)
        hits, errors, warnings = latest_candidates(client, {1: {}}, date(2026, 10, 1))
        self.assertEqual(len(hits), 1)
        self.assertEqual(client.get.call_count, 5)
        self.assertEqual(errors, [])
        self.assertEqual(latest_candidates(client, {2: {}}, date(2026, 10, 1))[0], [])

    def test_read_timeout_is_retried_only_twice_and_closed(self):
        response = Mock(status_code=200)
        response.iter_content.side_effect = requests.Timeout('slow')
        with patch('sec_client.requests.get', return_value=response) as get, patch('sec_client.time.sleep'):
            with self.assertRaises(requests.Timeout):
                SecClient().get('https://www.sec.gov/example')
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args.kwargs['timeout'], (5, 5))
            self.assertEqual(response.close.call_count, 2)

    def test_supplement_uses_primary_without_index_request(self):
        client = Mock()
        client.get.return_value = SimpleNamespace(text='Offering')
        docs = documents(client, dict(cik=1, accession='0000000001-26-000001', primary='notes.htm', form='FWP'))
        self.assertEqual(client.get.call_count, 1)
        self.assertTrue(docs[0][0].endswith('/notes.htm'))
