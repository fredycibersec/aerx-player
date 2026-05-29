"""Read MP3/audio file metadata and extract embedded cover art."""

import io
from pathlib import Path
from typing import Optional

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4
    from mutagen.ogg import OggFileType
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False


def read_tags(path: str) -> dict:
    """Return dict with keys: title, artist, album, track, cover_data (bytes|None)."""
    result = {
        'title':      Path(path).stem,
        'artist':     '',
        'album':      '',
        'track':      '',
        'cover_data': None,
    }

    if not HAS_MUTAGEN:
        return result

    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return result

        def _tag(key):
            v = audio.get(key)
            return str(v[0]) if v else ''

        result['title']  = _tag('title')  or result['title']
        result['artist'] = _tag('artist')
        result['album']  = _tag('album')
        result['track']  = _tag('tracknumber')

    except Exception:
        pass

    # Cover art – reload without easy tags for raw access
    try:
        raw = MutagenFile(path)
        if raw is None:
            return result

        # MP3 ID3
        if hasattr(raw, 'tags') and raw.tags:
            for key in raw.tags.keys():
                if key.startswith('APIC'):
                    result['cover_data'] = raw.tags[key].data
                    break

        # FLAC
        if hasattr(raw, 'pictures') and raw.pictures:
            result['cover_data'] = raw.pictures[0].data

        # MP4/AAC
        if hasattr(raw, 'tags') and raw.tags and 'covr' in raw.tags:
            covers = raw.tags['covr']
            if covers:
                result['cover_data'] = bytes(covers[0])

    except Exception:
        pass

    return result
