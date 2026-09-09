"""Radio Browser API client – fetches online stations by country/tag."""

import threading
import json
from typing import Callable, Optional

_MAX_BYTES = 15 * 1024 * 1024  # 15 MB — de sobra para JSON de emisoras y logos

try:
    import requests as _requests
    _SESSION = _requests.Session()
    _SESSION.headers['User-Agent'] = 'AERxPlayer/0.9-beta (GTK4 Linux; github.com/fredycibersec/aerx-player)'

    def _read_capped(r, max_bytes=_MAX_BYTES):
        total = 0
        chunks = []
        for chunk in r.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f'respuesta demasiado grande (> {max_bytes} bytes)')
            chunks.append(chunk)
        return b''.join(chunks)

    def _get_json(url, **kw):
        r = _SESSION.get(url, timeout=10, stream=True, **kw)
        r.raise_for_status()
        return json.loads(_read_capped(r))
    def _get_raw(url):
        r = _SESSION.get(url, timeout=8, stream=True)
        r.raise_for_status()
        ct = r.headers.get('Content-Type', '')
        if 'text/html' in ct or 'text/xml' in ct:
            raise ValueError(f"URL returned {ct}, not an image")
        return _read_capped(r)
except ImportError:
    import urllib.request, urllib.parse
    def _get_json(url, params=None, **_kw):
        if params:
            url += '?' + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = r.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ValueError(f'respuesta demasiado grande (> {_MAX_BYTES} bytes)')
            return json.loads(raw)
    def _get_raw(url):
        with urllib.request.urlopen(url, timeout=8) as r:
            ct = r.headers.get('Content-Type', '')
            if 'text/html' in ct:
                raise ValueError(f"URL returned HTML, not an image")
            raw = r.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ValueError(f'respuesta demasiado grande (> {_MAX_BYTES} bytes)')
            return raw


API_BASE = "https://de1.api.radio-browser.info/json"


def _run(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def fetch_stations(country: str = "Spain", limit: int = 150,
                   callback: Optional[Callable] = None):
    """Background fetch; callback(list|None, error_str|None)."""
    def _work():
        try:
            params = dict(limit=limit, order='votes', reverse='true',
                          hidebroken='true', countrycode='ES' if country == 'Spain' else country)
            data = _get_json(f"{API_BASE}/stations/search", params=params)
            if callback:
                callback(data, None)
        except Exception as exc:
            if callback:
                callback(None, str(exc))
    _run(_work)


def fetch_by_tag(tag: str, limit: int = 80, callback: Optional[Callable] = None):
    def _work():
        try:
            params = dict(limit=limit, order='votes', reverse='true',
                          hidebroken='true', tag=tag)
            data = _get_json(f"{API_BASE}/stations/search", params=params)
            if callback:
                callback(data, None)
        except Exception as exc:
            if callback:
                callback(None, str(exc))
    _run(_work)


def fetch_image(url: str, callback: Callable):
    """Download raw image bytes in background; callback(bytes|None, err|None)."""
    def _work():
        try:
            raw = _get_raw(url)
            callback(raw, None)
        except Exception as exc:
            callback(None, str(exc))
    _run(_work)
