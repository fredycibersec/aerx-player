"""Podcasts: búsqueda (iTunes Search API), parseo de feeds RSS y descarga
de episodios. Mismo patrón que radio_browser.py: hilo daemon por petición,
el callback se llama tal cual desde el hilo worker — quien toca widgets
GTK debe envolverlo en GLib.idle_add por su cuenta."""

import json
import threading
import xml.etree.ElementTree as ET
from typing import Callable, Optional

try:
    import requests as _requests
    _SESSION = _requests.Session()
    _SESSION.headers['User-Agent'] = 'AERxPlayer/0.9-beta (GTK4 Linux; github.com/fredycibersec/aerx-player)'

    def _get_json(url, params=None):
        r = _SESSION.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _get_text(url):
        r = _SESSION.get(url, timeout=15)
        r.raise_for_status()
        return r.text

    def _stream_download(url, dest_path, progress_cb, cancel_event):
        with _SESSION.get(url, stream=True, timeout=20) as r:
            r.raise_for_status()
            total = int(r.headers.get('Content-Length', 0))
            done = 0
            with open(dest_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError('cancelado')
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
except ImportError:
    import urllib.request, urllib.parse

    def _get_json(url, params=None):
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'User-Agent': 'AERxPlayer/0.9-beta'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _get_text(url):
        req = urllib.request.Request(url, headers={'User-Agent': 'AERxPlayer/0.9-beta'})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode('utf-8', errors='replace')

    def _stream_download(url, dest_path, progress_cb, cancel_event):
        req = urllib.request.Request(url, headers={'User-Agent': 'AERxPlayer/0.9-beta'})
        with urllib.request.urlopen(req, timeout=20) as r:
            total = int(r.headers.get('Content-Length', 0))
            done = 0
            with open(dest_path, 'wb') as f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError('cancelado')
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)


ITUNES_SEARCH_URL = 'https://itunes.apple.com/search'
_ITUNES_NS = '{http://www.itunes.com/dtds/podcast-1.0.dtd}'


def _run(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def search_shows(query: str, callback: Callable, limit: int = 25):
    """Background search de podcasts (no episodios) vía iTunes Search API.
    callback(list_de_shows | None, error_str | None). Cada show:
    {name, artist, feed_url, artwork_url}."""
    def _work():
        try:
            params = {'term': query, 'media': 'podcast', 'entity': 'podcast', 'limit': limit}
            data = _get_json(ITUNES_SEARCH_URL, params=params)
            shows = []
            for r in data.get('results') or []:
                feed_url = r.get('feedUrl')
                if not feed_url:
                    continue
                shows.append({
                    'name':        r.get('collectionName', ''),
                    'artist':      r.get('artistName', ''),
                    'feed_url':    feed_url,
                    'artwork_url': r.get('artworkUrl600') or r.get('artworkUrl100', ''),
                })
            callback(shows, None)
        except Exception as exc:
            callback(None, str(exc))
    _run(_work)


def _tag(item, name):
    el = item.find(name)
    return el.text.strip() if el is not None and el.text else ''


def _parse_duration(raw: str) -> int:
    """itunes:duration puede venir en segundos ('1234') o 'HH:MM:SS'/'MM:SS'."""
    raw = (raw or '').strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    parts = raw.split(':')
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def fetch_episodes(feed_url: str, callback: Callable, limit: int = 100):
    """Descarga y parsea un feed RSS de podcast en background.
    callback((show_meta, episodes) | (None, None), error_str | None).
    show_meta: {name, artist, artwork_url, feed_url}.
    episode: {guid, title, pub_date, audio_url, duration_sec, artwork_url, summary}."""
    def _work():
        try:
            xml_text = _get_text(feed_url)
            root = ET.fromstring(xml_text)
            channel = root.find('channel')
            if channel is None:
                callback((None, None), 'Feed RSS sin <channel>')
                return

            show_image = ''
            itunes_image = channel.find(f'{_ITUNES_NS}image')
            if itunes_image is not None:
                show_image = itunes_image.get('href', '')
            if not show_image:
                img = channel.find('image/url')
                if img is not None and img.text:
                    show_image = img.text.strip()

            show_meta = {
                'name':        _tag(channel, 'title'),
                'artist':      _tag(channel, f'{_ITUNES_NS}author') or _tag(channel, 'title'),
                'artwork_url': show_image,
                'feed_url':    feed_url,
            }

            episodes = []
            for item in channel.findall('item')[:limit]:
                enclosure = item.find('enclosure')
                audio_url = enclosure.get('url', '') if enclosure is not None else ''
                if not audio_url:
                    continue

                guid_el = item.find('guid')
                guid = (guid_el.text or '').strip() if guid_el is not None and guid_el.text else audio_url

                ep_image = ''
                ep_img_el = item.find(f'{_ITUNES_NS}image')
                if ep_img_el is not None:
                    ep_image = ep_img_el.get('href', '')
                if not ep_image:
                    ep_image = show_image

                summary = _tag(item, f'{_ITUNES_NS}summary') or _tag(item, 'description')

                episodes.append({
                    'guid':         guid,
                    'title':        _tag(item, 'title'),
                    'pub_date':     _tag(item, 'pubDate'),
                    'audio_url':    audio_url,
                    'duration_sec': _parse_duration(_tag(item, f'{_ITUNES_NS}duration')),
                    'artwork_url':  ep_image,
                    'summary':      summary[:500],
                })

            callback((show_meta, episodes), None)
        except Exception as exc:
            callback((None, None), str(exc))
    _run(_work)


def download_episode(url: str, dest_path: str, callback: Callable,
                      progress_cb: Optional[Callable] = None,
                      cancel_event: Optional[threading.Event] = None):
    """Descarga un episodio a disco en background.
    progress_cb(bytes_done, bytes_total) — total puede ser 0 si el
    servidor no manda Content-Length. callback(dest_path | None, error_str | None)."""
    def _work():
        try:
            _stream_download(url, dest_path, progress_cb, cancel_event)
            callback(dest_path, None)
        except InterruptedError:
            callback(None, 'cancelado')
        except Exception as exc:
            callback(None, str(exc))
    _run(_work)
