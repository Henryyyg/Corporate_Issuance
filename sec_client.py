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
        for attempt in range(2):
            with _lock:
                time.sleep(max(0, _next_request - time.monotonic()))
                _next_request = time.monotonic() + 0.25  # max 4 starts/sec/process
            try:
                started = time.monotonic()
                response = requests.get(url, params=params, headers={
                    'User-Agent': self.user_agent, 'Accept-Encoding': 'gzip, deflate'},
                    timeout=(5, 5), stream=True)
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < 1:
                        response.close()
                        time.sleep(2 ** attempt)
                        continue
                try:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=16384):
                        if time.monotonic() - started > 20:
                            raise requests.Timeout('SEC download exceeded 20 seconds')
                        size += len(chunk)
                        if size > 12 * 1024 * 1024:
                            raise requests.RequestException('SEC document exceeds 12 MB; left unprocessed')
                        chunks.append(chunk)
                    response._content = b''.join(chunks)
                    response._content_consumed = True
                finally:
                    response.close()
                return response
            except requests.RequestException:
                if attempt == 1:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError('SEC request failed')
