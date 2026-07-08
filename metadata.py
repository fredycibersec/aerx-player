"""Read/write MP3/audio file metadata and extract/embed cover art."""

import io
import os
from pathlib import Path
from typing import Optional

try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TRCK, ID3NoHeaderError, error as ID3Error
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.ogg import OggFileType
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False


class TagWriteError(Exception):
    """Raised when writing tags/cover art to an audio file fails."""


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


def _normalize_track(track) -> str:
    """Return track number as plain 'N' string, accepting 'N', 'N/M' or int."""
    if track is None:
        return ''
    s = str(track).strip()
    if not s:
        return ''
    return s.split('/')[0].strip()


def _cover_mime_to_mp4_format(cover_mime: str) -> int:
    if cover_mime and 'png' in cover_mime.lower():
        return MP4Cover.FORMAT_PNG
    return MP4Cover.FORMAT_JPEG


def _write_mp3_tags(path: str, title: str, artist: str, album: str,
                     track: str, cover_data: Optional[bytes],
                     cover_mime: str) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()  # fichero sin frames ID3 aún: creamos contenedor nuevo

    if title is not None:
        tags.setall('TIT2', [TIT2(encoding=3, text=[title])])
    if artist is not None:
        tags.setall('TPE1', [TPE1(encoding=3, text=[artist])])
    if album is not None:
        tags.setall('TALB', [TALB(encoding=3, text=[album])])

    track_str = _normalize_track(track)
    if track_str:
        tags.setall('TRCK', [TRCK(encoding=3, text=[track_str])])

    if cover_data:
        # delall + add evita duplicar APIC si ya había una carátula
        tags.delall('APIC')
        tags.add(APIC(
            encoding=3,
            mime=cover_mime or 'image/jpeg',
            type=3,  # 3 = "Cover (front)"
            desc='Cover',
            data=cover_data,
        ))

    tags.save(path, v2_version=3)


def _write_flac_tags(path: str, title: str, artist: str, album: str,
                      track: str, cover_data: Optional[bytes],
                      cover_mime: str) -> None:
    audio = FLAC(path)

    if title is not None:
        audio['title'] = [title]
    if artist is not None:
        audio['artist'] = [artist]
    if album is not None:
        audio['album'] = [album]

    track_str = _normalize_track(track)
    if track_str:
        audio['tracknumber'] = [track_str]

    if cover_data:
        audio.clear_pictures()  # reemplaza cualquier picture existente
        pic = Picture()
        pic.data = cover_data
        pic.type = 3  # front cover
        pic.mime = cover_mime or 'image/jpeg'
        audio.add_picture(pic)

    audio.save()


def _write_mp4_tags(path: str, title: str, artist: str, album: str,
                     track: str, cover_data: Optional[bytes],
                     cover_mime: str) -> None:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()

    if title is not None:
        audio.tags['\xa9nam'] = [title]
    if artist is not None:
        audio.tags['\xa9ART'] = [artist]
    if album is not None:
        audio.tags['\xa9alb'] = [album]

    track_str = _normalize_track(track)
    if track_str.isdigit():
        # 'trkn' es una lista de tuplas (track, total); total=0 si desconocido
        existing_total = 0
        prev = audio.tags.get('trkn')
        if prev and len(prev[0]) > 1:
            existing_total = prev[0][1]
        audio.tags['trkn'] = [(int(track_str), existing_total)]

    if cover_data:
        fmt = _cover_mime_to_mp4_format(cover_mime)
        audio.tags['covr'] = [MP4Cover(cover_data, imageformat=fmt)]

    audio.save()


def write_tags(path: str, title: str, artist: str, album: str,
                track: str, cover_data: Optional[bytes] = None,
                cover_mime: str = 'image/jpeg') -> None:
    """Write title/artist/album/track (+ optional cover) to an audio file.

    Supports MP3 (ID3v2.3), FLAC and MP4/M4A. Raises TagWriteError with a
    human-readable message on any failure (unsupported format, read-only
    file, corrupt container, etc.) so the caller can show it to the user.
    """
    if not HAS_MUTAGEN:
        raise TagWriteError('mutagen no está instalado; no se pueden guardar etiquetas.')

    p = Path(path)
    if not p.exists():
        raise TagWriteError(f'El fichero no existe: {path}')
    if not p.is_file() or not os.access(path, os.W_OK):
        raise TagWriteError(f'El fichero no tiene permisos de escritura: {path}')

    suffix = p.suffix.lower()
    try:
        if suffix == '.mp3':
            _write_mp3_tags(path, title, artist, album, track, cover_data, cover_mime)
        elif suffix == '.flac':
            _write_flac_tags(path, title, artist, album, track, cover_data, cover_mime)
        elif suffix in ('.m4a', '.mp4', '.aac'):
            _write_mp4_tags(path, title, artist, album, track, cover_data, cover_mime)
        else:
            raise TagWriteError(f'Formato no soportado para escritura de etiquetas: {suffix}')
    except TagWriteError:
        raise
    except ID3NoHeaderError as exc:
        raise TagWriteError(f'No se pudieron crear las etiquetas ID3: {exc}') from exc
    except ID3Error as exc:
        raise TagWriteError(f'Error al escribir etiquetas ID3: {exc}') from exc
    except PermissionError as exc:
        raise TagWriteError(f'Permiso denegado al guardar el fichero: {exc}') from exc
    except OSError as exc:
        raise TagWriteError(f'Error de E/S al guardar el fichero: {exc}') from exc
    except Exception as exc:
        raise TagWriteError(f'No se pudo guardar la etiqueta: {exc}') from exc
