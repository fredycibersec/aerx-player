"""Automatic metadata + cover art lookup for local MP3/audio files.

Primary source: MusicBrainz (recording search) + Cover Art Archive (front cover).
Fallback: iTunes Search API, used when MusicBrainz has no match or no cover.
"""

import json
import threading
import urllib.parse
from typing import Callable, Optional

_USER_AGENT = 'RadioES/1.2.2 (+https://github.com/fredycibersec/radioes)'

try:
    import requests as _requests
    _SESSION = _requests.Session()
    _SESSION.headers['User-Agent'] = _USER_AGENT

    def _get_json(url, params=None):
        r = _SESSION.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _get_raw(url):
        r = _SESSION.get(url, timeout=10)
        r.raise_for_status()
        ct = r.headers.get('Content-Type', '')
        if 'text/html' in ct or 'text/xml' in ct:
            raise ValueError(f"URL returned {ct}, not an image")
        return r.content, ct
except ImportError:
    import urllib.request

    def _get_json(url, params=None):
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _get_raw(url):
        req = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            ct = r.headers.get('Content-Type', '')
            if 'text/html' in ct:
                raise ValueError("URL returned HTML, not an image")
            return r.read(), ct


MUSICBRAINZ_BASE  = 'https://musicbrainz.org/ws/2'
COVERART_BASE     = 'https://coverartarchive.org'
ITUNES_SEARCH_URL = 'https://itunes.apple.com/search'


def _run(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def _musicbrainz_lookup(artist: str, title: str, album: str):
    """Return (info_dict, release_mbid) from the best-scoring recording, or (None, None)."""
    parts = []
    if title:
        parts.append(f'recording:"{title}"')
    if artist:
        parts.append(f'artist:"{artist}"')
    if album:
        parts.append(f'release:"{album}"')
    if not parts:
        return None, None

    params = {'query': ' AND '.join(parts), 'fmt': 'json', 'limit': 5}
    data = _get_json(f'{MUSICBRAINZ_BASE}/recording/', params=params)
    recordings = data.get('recordings') or []
    if not recordings:
        return None, None

    rec = recordings[0]
    credit = rec.get('artist-credit') or []
    info = {
        'title':  rec.get('title', ''),
        'artist': credit[0].get('name', '') if credit else '',
        'album':  '',
    }
    releases = rec.get('releases') or []
    mbid = None
    if releases:
        info['album'] = releases[0].get('title', '')
        mbid = releases[0].get('id')
    return info, mbid


def _coverart_fetch(mbid: str):
    if not mbid:
        return None, None
    return _get_raw(f'{COVERART_BASE}/release/{mbid}/front')


def _itunes_lookup(artist: str, title: str, album: str) -> Optional[dict]:
    term = ' '.join(p for p in (artist, title) if p) or (album or '')
    if not term:
        return None

    params = {'term': term, 'media': 'music', 'entity': 'song', 'limit': 1}
    data = _get_json(ITUNES_SEARCH_URL, params=params)
    results = data.get('results') or []
    if not results:
        return None

    r = results[0]
    info = {
        'title':  r.get('trackName', ''),
        'artist': r.get('artistName', ''),
        'album':  r.get('collectionName', ''),
    }
    cover_data, cover_mime = None, None
    art_url = r.get('artworkUrl100', '')
    if art_url:
        hires_url = art_url.replace('100x100bb', '1200x1200bb')
        for candidate in (hires_url, art_url):
            try:
                cover_data, cover_mime = _get_raw(candidate)
                break
            except Exception:
                continue
    info['cover_data'] = cover_data
    info['cover_mime'] = cover_mime or 'image/jpeg'
    return info


def search(artist: str, title: str, album: str, callback: Callable):
    """Background lookup; callback(result|None, error_str|None).

    result keys: title, artist, album, cover_data (bytes|None), cover_mime.
    Tries MusicBrainz + Cover Art Archive first; falls back to the iTunes
    Search API if no release or no cover art was found there.
    """
    def _work():
        try:
            try:
                info, mbid = _musicbrainz_lookup(artist, title, album)
            except Exception:
                # Un fallo de MusicBrainz (red, límite de peticiones, query
                # rara) no debe impedir intentar el fallback de iTunes.
                info, mbid = None, None
            cover_data, cover_mime = None, None
            if mbid:
                try:
                    cover_data, cover_mime = _coverart_fetch(mbid)
                except Exception:
                    cover_data, cover_mime = None, None

            if info is None or not cover_data:
                try:
                    fallback = _itunes_lookup(artist, title, album)
                except Exception:
                    fallback = None
                if fallback:
                    if info is None:
                        info = {'title': fallback['title'], 'artist': fallback['artist'],
                                 'album': fallback['album']}
                    if not cover_data and fallback.get('cover_data'):
                        cover_data = fallback['cover_data']
                        cover_mime = fallback['cover_mime']

            if info is None and not cover_data:
                callback(None, None)
                return

            result = {
                'title':      (info or {}).get('title', ''),
                'artist':     (info or {}).get('artist', ''),
                'album':      (info or {}).get('album', ''),
                'cover_data': cover_data,
                'cover_mime': cover_mime or 'image/jpeg',
            }
            callback(result, None)
        except Exception as exc:
            callback(None, str(exc))

    _run(_work)
