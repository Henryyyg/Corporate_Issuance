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
