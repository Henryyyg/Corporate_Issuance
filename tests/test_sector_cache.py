import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sp500 import get_universe


class SectorCacheTests(unittest.TestCase):
    def test_old_local_cache_uses_enriched_shared_snapshot_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / 'data'
            local.mkdir()
            old = dict(month='2026-10', updated_at='2026-10-01', companies=[dict(cik=1, ticker='A', company='A')])
            enriched = dict(old, companies=[dict(cik=1, ticker='A', company='A', sector='Financials')])
            (local / 'sp500.json').write_text(json.dumps(old))
            (root / 'universe_snapshot.json').write_text(json.dumps(enriched))
            with patch('sp500.__file__', str(root / 'sp500.py')), patch('sp500.requests.get') as network:
                data, warning = get_universe(local, now=datetime(2026, 10, 1))
                network.assert_not_called()
                self.assertIsNone(warning)
                self.assertEqual(data['companies'][0]['sector'], 'Financials')
