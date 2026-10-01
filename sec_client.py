"""Shared SEC client: identified requests, serial rate limit and visible failures."""
import os
import threading
import time

import requests

_lock = threading.Lock()
_next_request = 0.0


class SecClient:
    def __init__(self, user_agent=None):
        self.user_agent = user_agent or os.getenv(
            'SEC_USER_AGENT', 'Newsquawk Corporate Issuance henry.gilbert@newsquawk.com')

    def get(self, url, params=None):
        global _next_request
        for attempt in range(4):
            with _lock:
                time.sleep(max(0, _next_request - time.monotonic()))
                _next_request = time.monotonic() + 0.25  # max 4 starts/sec/process
            try:
                response = requests.get(url, params=params, headers={
                    'User-Agent': self.user_agent, 'Accept-Encoding': 'gzip, deflate'}, timeout=30)
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < 3:
                        time.sleep(2 ** attempt)
                        continue
                response.raise_for_status()
                return response
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError('SEC request failed')
