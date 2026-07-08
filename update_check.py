"""Check GitHub Releases for a newer RadioES version."""

import json
import threading
from typing import Callable

_USER_AGENT = 'RadioES-UpdateCheck/1.0 (+https://github.com/fredycibersec/radioes)'

try:
    import requests as _requests
    _SESSION = _requests.Session()
    _SESSION.headers['User-Agent'] = _USER_AGENT

    def _get_json(url):
        r = _SESSION.get(url, timeout=6)
        r.raise_for_status()
        return r.json()
except ImportError:
    import urllib.request

    def _get_json(url):
        req = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read())


GITHUB_REPO       = 'fredycibersec/radioes'
RELEASES_API_URL  = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
RELEASES_PAGE_URL = f'https://github.com/{GITHUB_REPO}/releases/latest'


def _parse_version(v: str) -> tuple:
    v = (v or '').strip()
    if v[:1].lower() == 'v':
        v = v[1:]
    parts = []
    for p in v.split('.'):
        digits = ''.join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def _run(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def check_latest(current_version: str, callback: Callable):
    """Background check against GitHub Releases; callback(info|None, error_str|None).

    info keys: version, tag, download_url (.deb asset if present, else the
    release page), page_url, is_newer (bool vs. current_version).
    """
    def _work():
        try:
            data = _get_json(RELEASES_API_URL)
            tag = data.get('tag_name', '')
            version = tag[1:] if tag[:1].lower() == 'v' else tag

            download_url = None
            for asset in data.get('assets') or []:
                if asset.get('name', '').endswith('.deb'):
                    download_url = asset.get('browser_download_url')
                    break

            info = {
                'version':      version,
                'tag':          tag,
                'download_url': download_url or data.get('html_url') or RELEASES_PAGE_URL,
                'page_url':     data.get('html_url') or RELEASES_PAGE_URL,
                'is_newer':     is_newer(tag or version, current_version),
            }
            callback(info, None)
        except Exception as exc:
            callback(None, str(exc))

    _run(_work)
