#!/usr/bin/env python3
"""ÆRx Player – Radio y Audio. Reproductor de radio online y archivos MP3 (GTK4/Adwaita)."""

import sys
import json
import base64
import datetime
import hashlib
import os
import shutil
import tempfile
import threading
import urllib.parse
import zlib
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gst', '1.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Pango', '1.0')

from gi.repository import (
    Gtk, Adw, Gst, GLib, GObject,
    Gio, GdkPixbuf, Gdk, Pango,
)

from player import Player
import radio_browser
import cover_lookup
import update_check
import podcasts
import metadata as meta_mod

Gst.init(None)

APP_VERSION = '0.99-beta'
KOFI_URL    = 'https://ko-fi.com/saruman_dev'

DATA_DIR      = Path(__file__).parent / 'data'
STATIONS_FILE = DATA_DIR / 'spanish_stations.json'
CONFIG_DIR    = Path.home() / '.local' / 'share' / 'aerx-player'
CONFIG_FILE   = CONFIG_DIR / 'config.json'
CACHE_FILE    = CONFIG_DIR / 'mp3_cache.json'
PODCASTS_FILE = CONFIG_DIR / 'podcasts.json'
PODCAST_DOWNLOAD_DIR = CONFIG_DIR / 'podcast_downloads'

_LEGACY_CONFIG_DIR = Path.home() / '.local' / 'share' / 'radioes'


def _migrate_legacy_config_dir():
    """Migra la config de la beta anterior (RadioES, ~/.local/share/radioes) a la
    nueva ruta de ÆRx Player en el primer arranque tras el rebrand. No pisa una
    carpeta nueva que ya exista (p.ej. tras una instalación limpia)."""
    if CONFIG_DIR.exists() or not _LEGACY_CONFIG_DIR.exists():
        return
    try:
        shutil.move(str(_LEGACY_CONFIG_DIR), str(CONFIG_DIR))
    except OSError:
        pass


_migrate_legacy_config_dir()

_HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')
_HAS_BREAKPOINT    = hasattr(Adw, 'Breakpoint')

# ── Helpers ────────────────────────────────────────────────────────────────────

def _pixbuf_from_bytes(data: bytes, size: int = 200) -> GdkPixbuf.Pixbuf | None:
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.set_size(size, size)
        loader.write(data)
        loader.close()
        pb = loader.get_pixbuf()
        if pb:
            w, h = pb.get_width(), pb.get_height()
            if w < 1 or h < 1:
                return None
            scale = size / max(w, h)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            if nw != w or nh != h:
                pb = pb.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
            return pb
    except Exception:
        pass
    return None


def _placeholder_pixbuf(icon_name: str, size: int = 64) -> GdkPixbuf.Pixbuf | None:
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    info  = theme.lookup_icon(icon_name, None, size, 1,
                              Gtk.TextDirection.NONE, 0)
    return info.load_icon() if info else None


# ── Fullscreen cover background (blur + oscurecido) ────────────────────────────

_FULLSCREEN_MIN_SIDE = 150   # lado menor mínimo (px) para activar el fondo a pantalla completa
                              # (los favicons reales de emisoras rara vez superan 180-192px —p.ej.
                              # apple-touch-icon-192x192—, así que un umbral de 500px no se
                              # cumplía casi nunca; al ir siempre desenfocado, una fuente más
                              # pequeña sigue quedando bien)
_COVER_BLUR_FACTOR    = 5    # downscale por pasada (pirámide iterativa, no un solo salto agresivo)
_COVER_BLUR_PASSES    = 5    # nº de pasadas de downscale+upscale acumulativas

_COVER_BG_CSS = b"""
.cover-dim-layer {
    background-color: rgba(0, 0, 0, 0.55);
}
.now-playing-translucent {
    background-color: transparent;
}
"""


def _blur_pixbuf(pb: GdkPixbuf.Pixbuf, factor: int = _COVER_BLUR_FACTOR,
                  passes: int = _COVER_BLUR_PASSES) -> GdkPixbuf.Pixbuf:
    """Cheap blur via a small iterative downscale/upscale pyramid. No new deps.

    Each pass shrinks by a mild factor and rescales back to native size —
    repeated mild passes approximate a real gaussian blur (smooth, rich
    color) far better than one aggressive downscale+upscale jump, which
    looks flat/"low-res"/patchy instead of properly blurred.
    """
    w, h = pb.get_width(), pb.get_height()
    cur = pb
    for _ in range(passes):
        cw, ch = cur.get_width(), cur.get_height()
        small = cur.scale_simple(max(1, cw // factor), max(1, ch // factor), GdkPixbuf.InterpType.BILINEAR)
        cur = small.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
    return cur


def _cover_native_pixbuf(cover_data: bytes) -> GdkPixbuf.Pixbuf | None:
    """Decode cover_data once, at native resolution."""
    try:
        gbytes = GLib.Bytes.new(cover_data)
        stream = Gio.MemoryInputStream.new_from_bytes(gbytes)
        return GdkPixbuf.Pixbuf.new_from_stream(stream, None)
    except Exception:
        return None


def _load_stations_file() -> list:
    try:
        with open(STATIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


_SAVE_LOCK = threading.Lock()


def _atomic_write_json(path: Path, data) -> None:
    """Escritura atómica y serializada: evita que dos hilos (p.ej. guardar
    ajustes y guardar caché de podcasts casi a la vez) entrelacen bytes del
    mismo fichero y lo corrompan en silencio."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _SAVE_LOCK:
        fd, tmp_path = tempfile.mkstemp(dir=str(CONFIG_DIR), prefix=f'.{path.name}.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _save_config(cfg: dict):
    _atomic_write_json(CONFIG_FILE, cfg)


def _load_mp3_cache() -> list:
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_mp3_cache(tracks: list):
    _atomic_write_json(CACHE_FILE, tracks)


def _load_podcasts_data() -> dict:
    try:
        with open(PODCASTS_FILE) as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault('subscriptions', [])
                data.setdefault('episodes', {})
                return data
    except Exception:
        pass
    return {'subscriptions': [], 'episodes': {}}


def _save_podcasts_data(data: dict):
    _atomic_write_json(PODCASTS_FILE, data)


def _episode_download_path(guid: str, audio_url: str) -> Path:
    ext = Path(urllib.parse.urlparse(audio_url).path).suffix or '.mp3'
    name = hashlib.sha1(guid.encode('utf-8')).hexdigest()
    return PODCAST_DOWNLOAD_DIR / f'{name}{ext}'


def _fmt_duration_short(seconds: int) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'


def _fmt_pub_date(raw: str) -> str:
    """RSS pubDate ('Tue, 21 Jul 2026 07:00:00 +0000') → 'dd/mm/aaaa', o
    el texto tal cual si no matchea el formato esperado."""
    import email.utils as _eu
    try:
        dt = _eu.parsedate_to_datetime(raw)
        return dt.strftime('%d/%m/%Y')
    except Exception:
        return raw[:16]


# ── Station row widget ─────────────────────────────────────────────────────────

class StationRow(Gtk.ListBoxRow):
    def __init__(self, station: dict, is_favorite: bool = False, on_toggle_fav=None):
        super().__init__()
        self.station    = station
        self.logo_bytes = None
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        self.set_child(box)

        self._logo = Gtk.Image()
        self._logo.set_pixel_size(40)
        self._logo.set_size_request(40, 40)
        self._logo.set_from_icon_name('m3-music-note-symbolic')
        box.append(self._logo)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_hexpand(True)
        vbox.set_overflow(Gtk.Overflow.HIDDEN)
        box.append(vbox)

        self._name_label = Gtk.Label(label=station.get('name', ''))
        self._name_label.set_xalign(0)
        self._name_label.add_css_class('body')
        self._name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._name_label.set_max_width_chars(28)
        vbox.append(self._name_label)

        sub = station.get('description', '') or station.get('tags', '')
        if isinstance(sub, list):
            sub = ', '.join(sub[:3])
        self._sub_label = Gtk.Label(label=str(sub)[:80])
        self._sub_label.set_xalign(0)
        self._sub_label.add_css_class('caption')
        self._sub_label.add_css_class('dim-label')
        self._sub_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._sub_label.set_max_width_chars(40)
        vbox.append(self._sub_label)

        br = station.get('bitrate', '')
        if br:
            badge = Gtk.Label(label=f"{br}k")
            badge.add_css_class('m3-chip')
            box.append(badge)

        self._fav_btn = Gtk.Button()
        self._fav_btn.set_icon_name('m3-star-symbolic' if is_favorite else 'm3-star-outline-symbolic')
        self._fav_btn.add_css_class('flat')
        self._fav_btn.add_css_class('circular')
        self._fav_btn.set_valign(Gtk.Align.CENTER)
        if on_toggle_fav:
            self._fav_btn.connect('clicked', lambda btn: on_toggle_fav(station, btn))
        box.append(self._fav_btn)

    def set_favorite(self, is_fav: bool):
        self._fav_btn.set_icon_name('m3-star-symbolic' if is_fav else 'm3-star-outline-symbolic')

    def set_logo_bytes(self, data: bytes):
        self.logo_bytes = data
        pb = _pixbuf_from_bytes(data, 40)
        if pb:
            GLib.idle_add(self._logo.set_from_pixbuf, pb)


# ── Podcast episode row widget ──────────────────────────────────────────────────

class EpisodeRow(Gtk.ListBoxRow):
    """Fila de episodio de podcast: portada, título/fecha+duración, estado
    de escuchado (atenuado) y botón de descarga con 3 estados. El play se
    dispara por 'row-activated' del ListBox contenedor, igual que
    StationRow."""

    def __init__(self, episode: dict, state: dict, on_download=None, on_remove_download=None):
        super().__init__()
        self.episode = episode
        self.logo_bytes = None
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        self.set_child(box)

        self._logo = Gtk.Image()
        self._logo.set_pixel_size(40)
        self._logo.set_size_request(40, 40)
        self._logo.set_from_icon_name('m3-podcasts-symbolic')
        box.append(self._logo)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_hexpand(True)
        vbox.set_overflow(Gtk.Overflow.HIDDEN)
        box.append(vbox)

        self._name_label = Gtk.Label(label=episode.get('title', ''))
        self._name_label.set_xalign(0)
        self._name_label.add_css_class('body')
        self._name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._name_label.set_max_width_chars(40)
        vbox.append(self._name_label)

        meta = _fmt_pub_date(episode.get('pub_date', ''))
        dur = episode.get('duration_sec', 0)
        if dur:
            meta = f'{meta} · {_fmt_duration_short(dur)}'
        self._sub_label = Gtk.Label(label=meta)
        self._sub_label.set_xalign(0)
        self._sub_label.add_css_class('caption')
        self._sub_label.add_css_class('dim-label')
        self._sub_label.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(self._sub_label)

        self._dl_btn = Gtk.Button()
        self._dl_btn.add_css_class('flat')
        self._dl_btn.add_css_class('circular')
        self._dl_btn.set_valign(Gtk.Align.CENTER)
        self._on_download = on_download
        self._on_remove_download = on_remove_download
        self._dl_btn.connect('clicked', self._on_dl_btn_clicked)
        box.append(self._dl_btn)

        self.set_listened(bool(state.get('listened')))
        self.set_download_state(
            'done' if state.get('downloaded_path') else 'none')

    def _on_dl_btn_clicked(self, btn):
        if self._dl_state == 'done':
            if self._on_remove_download:
                self._on_remove_download(self)
        elif self._dl_state == 'none':
            if self._on_download:
                self._on_download(self)
        # 'downloading' → sin acción, el botón está deshabilitado

    def set_listened(self, listened: bool):
        self.listened = listened
        if listened:
            self._name_label.add_css_class('dim-label')
        else:
            self._name_label.remove_css_class('dim-label')

    def set_download_state(self, state: str):
        """state: 'none' | 'downloading' | 'done'."""
        self._dl_state = state
        if state == 'downloading':
            self._dl_btn.set_icon_name('m3-sync-symbolic')
            self._dl_btn.set_tooltip_text('Descargando…')
            self._dl_btn.set_sensitive(False)
        elif state == 'done':
            self._dl_btn.set_icon_name('m3-download-done-symbolic')
            self._dl_btn.set_tooltip_text('Descargado — pulsa para eliminar')
            self._dl_btn.set_sensitive(True)
        else:
            self._dl_btn.set_icon_name('m3-download-symbolic')
            self._dl_btn.set_tooltip_text('Descargar para escuchar sin conexión')
            self._dl_btn.set_sensitive(True)

    def set_logo_bytes(self, data: bytes):
        self.logo_bytes = data
        pb = _pixbuf_from_bytes(data, 40)
        if pb:
            GLib.idle_add(self._logo.set_from_pixbuf, pb)


# ── Genre header row (collapsible section) ────────────────────────────────────

class GenreHeaderRow(Gtk.ListBoxRow):
    """Non-selectable row that works as a clickable collapsible section header."""

    def __init__(self, genre: str, on_toggle):
        super().__init__()
        self.genre = genre
        self.set_selectable(False)
        self.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(8)
        box.set_margin_top(6)
        box.set_margin_bottom(4)
        self.set_child(box)

        if genre == 'Favoritas':
            star_img = Gtk.Image.new_from_icon_name('m3-star-symbolic')
            star_img.add_css_class('warning')
            box.append(star_img)

        lbl = Gtk.Label(label=genre)
        lbl.add_css_class('heading')
        lbl.set_hexpand(True)
        lbl.set_xalign(0)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(lbl)

        self._arrow = Gtk.Image.new_from_icon_name('m3-expand-more-symbolic')
        box.append(self._arrow)

        gc = Gtk.GestureClick()
        gc.connect('released', lambda g, n, x, y: on_toggle(genre))
        self.add_controller(gc)

    def set_collapsed(self, collapsed: bool):
        self._arrow.set_from_icon_name(
            'm3-chevron-right-symbolic' if collapsed else 'm3-expand-more-symbolic'
        )


# ── MP3 file row ───────────────────────────────────────────────────────────────

class Mp3Row(Gtk.ListBoxRow):
    def __init__(self, path: str, tags: dict, on_edit=None):
        super().__init__()
        self.path = path
        self.tags = tags

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_start(8); box.set_margin_end(8)
        box.set_margin_top(6);   box.set_margin_bottom(6)
        self.set_child(box)

        self._art = Gtk.Image()
        self._art.set_pixel_size(40)
        self._art.set_size_request(40, 40)
        box.append(self._art)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_hexpand(True)
        vbox.set_overflow(Gtk.Overflow.HIDDEN)
        box.append(vbox)

        self._title_lbl = Gtk.Label()
        self._title_lbl.set_xalign(0)
        self._title_lbl.add_css_class('body')
        self._title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._title_lbl.set_max_width_chars(28)
        vbox.append(self._title_lbl)

        self._artist_lbl = Gtk.Label()
        self._artist_lbl.set_xalign(0)
        self._artist_lbl.add_css_class('caption')
        self._artist_lbl.add_css_class('dim-label')
        self._artist_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(self._artist_lbl)

        self._edit_btn = Gtk.Button()
        self._edit_btn.set_icon_name('m3-edit-symbolic')
        self._edit_btn.set_tooltip_text('Editar etiquetas')
        self._edit_btn.add_css_class('flat')
        self._edit_btn.add_css_class('circular')
        self._edit_btn.set_valign(Gtk.Align.CENTER)
        if on_edit:
            self._edit_btn.connect('clicked', lambda btn: on_edit(self))
        box.append(self._edit_btn)

        self.refresh(tags)

    def refresh(self, tags: dict):
        """Repaint art/labels after tags dict has changed (e.g. after editing)."""
        self.tags = tags
        if tags.get('cover_data'):
            pb = _pixbuf_from_bytes(tags['cover_data'], 40)
            if pb:
                self._art.set_from_pixbuf(pb)
            else:
                self._art.set_from_icon_name('m3-music-note-symbolic')
        else:
            self._art.set_from_icon_name('m3-music-note-symbolic')

        self._title_lbl.set_text(tags.get('title') or Path(self.path).stem)
        self._artist_lbl.set_text(tags.get('artist', ''))


# ── Spectrum visualizer ────────────────────────────────────────────────────────

class SpectrumVisualizer(Gtk.Overlay):
    """Multi-mode spectrum analyzer — six visual styles, cycle with the arrow button."""

    BANDS         = 40
    DISPLAY_BANDS = 26
    THRESHOLD     = -80.0
    DECAY         = 1.2
    HEIGHT        = 160

    _MODES = ('gauss', 'bars', 'scope', 'classic', 'radial', 'mirror', 'vu', 'particles')
    _LABELS = {
        'gauss':     'Onda suave',
        'bars':      'Barras agrupadas',
        'scope':     'Osciloscopio',
        'classic':   'Barras clásicas',
        'radial':    'Radial',
        'mirror':    'Espejo',
        'vu':        'Vúmetro',
        'particles': 'Partículas',
    }

    def __init__(self):
        super().__init__()
        self._mags   = [self.THRESHOLD] * self.BANDS
        self._peaks  = [self.THRESHOLD] * self.BANDS
        self._active = False
        self._mode   = 0

        # Estado del modo "particles" (ondas concéntricas + partículas orbitales)
        self._waves          = []   # lista de dicts {'progress': 0..1, 'strength': 0..1}
        self._prev_bass      = 0.0
        self._wave_cooldown  = 0
        self._particle_phase = 0.0

        self._da = Gtk.DrawingArea()
        self._da.set_size_request(-1, self.HEIGHT)
        self._da.set_hexpand(True)
        self._da.set_draw_func(self._draw)
        self.set_child(self._da)
        self.set_size_request(-1, self.HEIGHT)
        self.set_hexpand(True)

        btn = Gtk.Button()
        btn.set_icon_name('m3-sync-symbolic')
        btn.add_css_class('circular')
        btn.add_css_class('flat')
        btn.set_halign(Gtk.Align.END)
        btn.set_valign(Gtk.Align.START)
        btn.set_margin_end(6)
        btn.set_margin_top(6)
        btn.set_opacity(0.55)
        btn.set_tooltip_text('Modo: ' + self._LABELS[self._MODES[0]])
        btn.connect('clicked', self._on_cycle)
        self._cycle_btn = btn
        self.add_overlay(btn)

        GLib.timeout_add(50, self._tick)

    def _on_cycle(self, _btn):
        self._mode = (self._mode + 1) % len(self._MODES)
        self._cycle_btn.set_tooltip_text('Modo: ' + self._LABELS[self._MODES[self._mode]])
        self._da.queue_draw()

    def push(self, magnitudes: list):
        n = min(len(magnitudes), self.BANDS)
        self._active = True
        for i in range(n):
            v = max(self.THRESHOLD, float(magnitudes[i]))
            self._mags[i] = v
            if v > self._peaks[i]:
                self._peaks[i] = v

        if self._MODES[self._mode] == 'particles':
            # Detección simple de golpe de graves: subida brusca en las bandas más bajas
            bass = sum(self._norm(self._mags[i]) for i in range(4)) / 4
            if (bass - self._prev_bass > 0.15 and bass > 0.35
                    and self._wave_cooldown <= 0):
                self._waves.append({'progress': 0.0, 'strength': bass})
                self._wave_cooldown = 6  # ~300ms a 50ms/tick, evita ráfagas de ondas
            self._prev_bass = bass

        self._da.queue_draw()

    def reset(self):
        self._mags   = [self.THRESHOLD] * self.BANDS
        self._peaks  = [self.THRESHOLD] * self.BANDS
        self._active = False
        self._waves  = []
        self._prev_bass     = 0.0
        self._wave_cooldown = 0
        self._da.queue_draw()

    def _tick(self):
        changed = False
        for i in range(self.BANDS):
            if self._peaks[i] > self._mags[i]:
                self._peaks[i] = max(self._mags[i], self._peaks[i] - self.DECAY)
                changed = True

        if self._MODES[self._mode] == 'particles':
            self._particle_phase = (self._particle_phase + 0.012) % 6.283185307179586
            if self._wave_cooldown > 0:
                self._wave_cooldown -= 1
            if self._waves:
                still_alive = []
                for wave in self._waves:
                    wave['progress'] += 0.035
                    if wave['progress'] < 1.0:
                        still_alive.append(wave)
                self._waves = still_alive
                changed = True
            elif self._active:
                changed = True  # partículas orbitales siguen moviéndose aunque no haya ondas

        if changed:
            self._da.queue_draw()
        return True

    def _norm(self, db: float) -> float:
        return max(0.0, min(1.0, (db - self.THRESHOLD) / -self.THRESHOLD))

    def _amp_color(self, t: float, alpha: float = 1.0):
        """t=0 (low) → green, t=0.5 → yellow, t=1 (high) → red."""
        if t < 0.5:
            s = t / 0.5
            r, g, b = s * 0.95, 0.88, 0.0
        else:
            s = (t - 0.5) / 0.5
            r, g, b = 0.95, 0.88 - s * 0.83, 0.0
        return (r, g, b, alpha)

    def _amp_grad(self, alpha, height, _cairo):
        """Gradiente vertical: y=0 (pico) → rojo, y=height (base) → verde."""
        pat = _cairo.LinearGradient(0, 0, 0, height)
        pat.add_color_stop_rgba(0.00, 0.95, 0.05, 0.0,  alpha)
        pat.add_color_stop_rgba(0.30, 1.00, 0.50, 0.0,  alpha)
        pat.add_color_stop_rgba(0.58, 0.92, 0.88, 0.0,  alpha)
        pat.add_color_stop_rgba(1.00, 0.05, 0.88, 0.05, alpha)
        return pat

    def _build_curve(self, pts, cr):
        if len(pts) < 2:
            return
        cr.move_to(*pts[0])
        for i in range(1, len(pts) - 1):
            mx = (pts[i][0] + pts[i + 1][0]) / 2
            my = (pts[i][1] + pts[i + 1][1]) / 2
            cr.curve_to(pts[i][0], pts[i][1], pts[i][0], pts[i][1], mx, my)
        cr.line_to(*pts[-1])

    def _rounded_bar(self, cr, x, y, w, h, r):
        import math
        r = min(r, w / 2, h / 2)
        if r < 0.5:
            cr.new_path()
            cr.rectangle(x, y, w, h)
            return
        cr.new_path()
        cr.move_to(x, y + h)
        cr.line_to(x, y + r)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.arc(x + w - r, y + r, r, 3 * math.pi / 2, 0)
        cr.line_to(x + w, y + h)
        cr.close_path()

    def _draw(self, _area, cr, width, height):
        import cairo as _cairo
        cr.set_operator(1)
        if not self._active:
            return
        mode = self._MODES[self._mode]
        if   mode == 'gauss':   self._draw_gauss(cr, width, height, _cairo)
        elif mode == 'bars':    self._draw_bars(cr, width, height, _cairo)
        elif mode == 'scope':   self._draw_scope(cr, width, height, _cairo)
        elif mode == 'classic': self._draw_classic(cr, width, height, _cairo)
        elif mode == 'radial':  self._draw_radial(cr, width, height, _cairo)
        elif mode == 'mirror':  self._draw_mirror(cr, width, height, _cairo)
        elif mode == 'vu':        self._draw_vu(cr, width, height, _cairo)
        elif mode == 'particles': self._draw_particles(cr, width, height, _cairo)

    # ── Modo 0: Gauss — campana suave simétrica ─────────────────────────────────

    def _draw_gauss(self, cr, width, height, _cairo):
        D = self.DISPLAY_BANDS
        mirror = list(range(D - 1, -1, -1)) + list(range(1, D))
        n      = len(mirror)
        draw_w = width * 0.88
        x_off  = (width - draw_w) / 2
        bw     = draw_w / n

        pts = []
        for i, bi in enumerate(mirror):
            norm = self._norm(self._mags[bi])
            pts.append((x_off + (i + 0.5) * bw, height - norm * (height - 4)))

        # Gradiente vertical: azul en base (baja energía) → rojo en pico (alta energía)
        cr.save()
        self._build_curve(pts, cr)
        cr.line_to(pts[-1][0], height)
        cr.line_to(pts[0][0],  height)
        cr.close_path()
        cr.set_source(self._amp_grad(0.22, height, _cairo))
        cr.fill()
        cr.restore()

        cr.save()
        self._build_curve(pts, cr)
        cr.set_source(self._amp_grad(0.14, height, _cairo))
        cr.set_line_width(14)
        cr.stroke()
        cr.restore()

        cr.save()
        self._build_curve(pts, cr)
        cr.set_source(self._amp_grad(0.92, height, _cairo))
        cr.set_line_width(2.0)
        cr.stroke()
        cr.restore()

        cr.save()
        self._build_curve(pts, cr)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.25)
        cr.set_line_width(0.8)
        cr.stroke()
        cr.restore()

        for i, bi in enumerate(mirror):
            norm = self._norm(self._peaks[bi])
            if norm < 0.03:
                continue
            rv, gv, bv, _ = self._amp_color(norm)
            x = x_off + (i + 0.5) * bw
            y = height - norm * (height - 4)
            cr.set_source_rgba(rv, gv, bv, 0.90)
            cr.rectangle(x - bw * 0.28, y - 1.5, bw * 0.56, 2.5)
            cr.fill()

    # ── Modo 1: Bars — barras anchas agrupadas ──────────────────────────────────

    def _draw_bars(self, cr, width, height, _cairo):
        D = self.DISPLAY_BANDS
        N = 10
        groups = []
        for g in range(N):
            lo = int(g * D / N)
            hi = max(int((g + 1) * D / N), lo + 1)
            avg  = sum(self._mags[lo:hi]) / (hi - lo)
            peak = max(self._peaks[lo:hi])
            groups.append((avg, peak))

        gap   = 5
        bar_w = (width - gap * (N + 1)) / N

        # Gradiente global vertical: se aplica a todas las barras (baja energía=azul, alta=rojo)
        ag = self._amp_grad(0.92, height, _cairo)

        for g, (avg_db, peak_db) in enumerate(groups):
            norm   = self._norm(avg_db)
            norm_p = self._norm(peak_db)

            x     = gap + g * (bar_w + gap)
            bar_h = norm * (height - 8)
            y     = height - bar_h

            if bar_h < 2:
                continue

            cr.save()
            self._rounded_bar(cr, x, y, bar_w, bar_h, 5)
            cr.set_source(ag)
            cr.fill()
            cr.restore()

            if norm_p > 0.04:
                py   = height - norm_p * (height - 8)
                rv, gv, bv, _ = self._amp_color(norm_p)
                cr.set_source_rgba(rv, gv, bv, 0.95)
                cr.rectangle(x, py - 2.5, bar_w, 3.5)
                cr.fill()

    # ── Modo 2: Scope — osciloscopio ────────────────────────────────────────────

    def _draw_scope(self, cr, width, height, _cairo):
        D  = self.DISPLAY_BANDS
        cy = height / 2

        import math as _math
        pts = [(0.0, cy)]
        for i in range(D):
            norm = self._norm(self._mags[i])
            x    = width * (i + 1) / (D + 1)
            # Onda suave con periodo de ~8 bandas — simula analizador de voz
            sign = _math.sin(_math.pi * i / 4.0)
            pts.append((x, cy + sign * norm * (height * 0.44)))
        pts.append((float(width), cy))

        # Línea de referencia central
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.07)
        cr.set_line_width(0.6)
        cr.move_to(0, cy)
        cr.line_to(width, cy)
        cr.stroke()

        # Gradiente vertical simétrico: verde en centro (reposo) → rojo en extremos (alta energía)
        line_grad = _cairo.LinearGradient(0, 0, 0, height)
        line_grad.add_color_stop_rgba(0.00, 0.95, 0.05, 0.0,  0.9)
        line_grad.add_color_stop_rgba(0.28, 1.00, 0.50, 0.0,  0.9)
        line_grad.add_color_stop_rgba(0.50, 0.05, 0.90, 0.05, 0.9)
        line_grad.add_color_stop_rgba(0.72, 1.00, 0.50, 0.0,  0.9)
        line_grad.add_color_stop_rgba(1.00, 0.95, 0.05, 0.0,  0.9)

        # Halo exterior
        cr.save()
        self._build_curve(pts, cr)
        glow = _cairo.LinearGradient(0, 0, 0, height)
        glow.add_color_stop_rgba(0.00, 0.95, 0.05, 0.0, 0.10)
        glow.add_color_stop_rgba(0.50, 0.05, 0.88, 0.05, 0.10)
        glow.add_color_stop_rgba(1.00, 0.95, 0.05, 0.0, 0.10)
        cr.set_source(glow)
        cr.set_line_width(12)
        cr.stroke()
        cr.restore()

        cr.save()
        self._build_curve(pts, cr)
        cr.set_source(line_grad)
        cr.set_line_width(2.0)
        cr.stroke()
        cr.restore()

        cr.save()
        self._build_curve(pts, cr)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.38)
        cr.set_line_width(0.6)
        cr.stroke()
        cr.restore()

    # ── Modo 3: Classic — barras finas individuales ─────────────────────────────

    def _draw_classic(self, cr, width, height, _cairo):
        D     = self.DISPLAY_BANDS
        gap   = 2
        bar_w = (width - gap * (D + 1)) / D

        # Gradiente global vertical aplicado a todas las barras
        ag = self._amp_grad(0.92, height, _cairo)

        for i in range(D):
            norm   = self._norm(self._mags[i])
            norm_p = self._norm(self._peaks[i])

            x     = gap + i * (bar_w + gap)
            bar_h = norm * (height - 4)
            y     = height - bar_h

            if bar_h < 1.5:
                continue

            cr.set_source(ag)
            cr.rectangle(x, y, bar_w, bar_h)
            cr.fill()

            cr.set_source_rgba(1.0, 1.0, 1.0, 0.22)
            cr.rectangle(x, y, bar_w, min(2.5, bar_h))
            cr.fill()

            if norm_p > 0.03:
                py = height - norm_p * (height - 4) - 2
                rv, gv, bv, _ = self._amp_color(norm_p)
                cr.set_source_rgba(rv, gv, bv, 0.95)
                cr.rectangle(x, py, bar_w, 2.5)
                cr.fill()

    # ── Modo 4: Radial — circular ────────────────────────────────────────────────

    def _draw_radial(self, cr, width, height, _cairo):
        import math
        D     = self.DISPLAY_BANDS
        cx    = width  / 2
        cy    = height / 2
        # Usar el espacio disponible completo (limitado por el lado más corto)
        r_max = min(width / 2, height / 2) * 0.92
        r_min = r_max * 0.18

        step  = 2 * math.pi / D
        outer = []
        for i in range(D):
            norm = self._norm(self._mags[i])
            # Curva de potencia: señales típicas (0.3-0.6) se ven grandes visualmente
            norm_v = norm ** 0.55
            r     = r_min + norm_v * (r_max - r_min)
            angle = -math.pi / 2 + i * step
            outer.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))

        # Polígono relleno con gradiente radial verde→rojo
        fill_grad = _cairo.RadialGradient(cx, cy, r_min * 0.5, cx, cy, r_max)
        fill_grad.add_color_stop_rgba(0.0,  0.05, 0.88, 0.05, 0.40)
        fill_grad.add_color_stop_rgba(0.50, 0.92, 0.88, 0.0,  0.25)
        fill_grad.add_color_stop_rgba(1.0,  0.95, 0.05, 0.0,  0.15)

        cr.save()
        cr.move_to(*outer[0])
        for pt in outer[1:]:
            cr.line_to(*pt)
        cr.close_path()
        cr.set_source(fill_grad)
        cr.fill()
        cr.restore()

        # Rayos coloreados por amplitud
        for i in range(D):
            norm  = self._norm(self._mags[i])
            norm_v = norm ** 0.55
            r     = r_min + norm_v * (r_max - r_min)
            angle = -math.pi / 2 + i * step
            rv, gv, bv, _ = self._amp_color(norm_v)
            x_out = cx + r      * math.cos(angle)
            y_out = cy + r      * math.sin(angle)
            x_in  = cx + r_min  * math.cos(angle)
            y_in  = cy + r_min  * math.sin(angle)

            cr.set_source_rgba(rv, gv, bv, 0.80)
            cr.set_line_width(1.6)
            cr.move_to(x_in, y_in)
            cr.line_to(x_out, y_out)
            cr.stroke()

            if norm_v > 0.15:
                cr.set_source_rgba(rv, gv, bv, 0.95)
                cr.arc(x_out, y_out, 2.5, 0, 2 * math.pi)
                cr.fill()

        # Contorno del polígono
        cr.save()
        cr.move_to(*outer[0])
        for pt in outer[1:]:
            cr.line_to(*pt)
        cr.close_path()
        out_grad = _cairo.RadialGradient(cx, cy, r_min, cx, cy, r_max)
        out_grad.add_color_stop_rgba(0.0,  0.05, 0.88, 0.05, 0.85)
        out_grad.add_color_stop_rgba(0.55, 0.95, 0.88, 0.0,  0.85)
        out_grad.add_color_stop_rgba(1.0,  0.95, 0.05, 0.0,  0.85)
        cr.set_source(out_grad)
        cr.set_line_width(1.5)
        cr.stroke()
        cr.restore()

        # Círculo central
        cr.set_source_rgba(0.05, 0.88, 0.05, 0.22)
        cr.arc(cx, cy, r_min, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.05, 0.88, 0.05, 0.60)
        cr.arc(cx, cy, r_min, 0, 2 * math.pi)
        cr.set_line_width(1.0)
        cr.stroke()

    # ── Modo 5: Mirror — espejo vertical ────────────────────────────────────────

    def _draw_mirror(self, cr, width, height, _cairo):
        D     = self.DISPLAY_BANDS
        gap   = 2
        bar_w = (width - gap * (D + 1)) / D
        cy    = height / 2

        for i in range(D):
            norm = self._norm(self._mags[i])

            x      = gap + i * (bar_w + gap)
            half_h = norm * (cy - 2)

            if half_h < 1.0:
                continue

            # Gradiente: verde en centro (reposo) → amarillo → rojo en punta (alta energía)
            rv, gv, bv, _ = self._amp_color(norm)

            # Barra superior (centro → arriba)
            pat_up = _cairo.LinearGradient(0, cy, 0, cy - half_h)
            pat_up.add_color_stop_rgba(0.0, 0.05, 0.88, 0.05, 0.55)
            pat_up.add_color_stop_rgba(0.5, 0.92, 0.88, 0.0,  0.78)
            pat_up.add_color_stop_rgba(1.0, rv,   gv,   bv,   0.95)
            cr.set_source(pat_up)
            cr.rectangle(x, cy - half_h, bar_w, half_h)
            cr.fill()

            # Barra inferior (centro → abajo): espejo idéntico
            pat_dn = _cairo.LinearGradient(0, cy, 0, cy + half_h)
            pat_dn.add_color_stop_rgba(0.0, 0.05, 0.88, 0.05, 0.55)
            pat_dn.add_color_stop_rgba(0.5, 0.92, 0.88, 0.0,  0.78)
            pat_dn.add_color_stop_rgba(1.0, rv,   gv,   bv,   0.90)
            cr.set_source(pat_dn)
            cr.rectangle(x, cy, bar_w, half_h)
            cr.fill()

            # Brillo en los extremos
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.22)
            cr.rectangle(x, cy - half_h, bar_w, min(2.0, half_h))
            cr.fill()
            cr.rectangle(x, cy + half_h - min(2.0, half_h), bar_w, min(2.0, half_h))
            cr.fill()

        # Línea divisoria central
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.10)
        cr.set_line_width(1.0)
        cr.move_to(0, cy)
        cr.line_to(width, cy)
        cr.stroke()

    # ── Modo 6: VU — vúmetro de doble canal (graves/agudos) con escala LED ─────

    def _draw_vu(self, cr, width, height, _cairo):
        D    = self.DISPLAY_BANDS
        half = D // 2
        low_norm  = sum(self._norm(self._mags[i])  for i in range(half))     / half
        high_norm = sum(self._norm(self._mags[i])  for i in range(half, D))  / (D - half)
        low_peak  = sum(self._norm(self._peaks[i]) for i in range(half))     / half
        high_peak = sum(self._norm(self._peaks[i]) for i in range(half, D))  / (D - half)

        SEGMENTS  = 22
        GAP_FRAC  = 0.24
        # Reservar espacio arriba-derecha para el botón de cambio de modo
        reserved_right = min(40, width * 0.15)
        usable_w  = max(width - reserved_right, width * 0.6)
        bar_gap   = usable_w * 0.10
        bar_w     = (usable_w - bar_gap * 3) / 2
        margin_v  = 6
        seg_h     = (height - margin_v * 2) / SEGMENTS
        led_r     = min(1.5, bar_w * 0.12)

        def draw_channel(x, norm, peak):
            lit = int(norm * SEGMENTS + 0.5)
            for s in range(SEGMENTS):
                t  = s / (SEGMENTS - 1)
                y  = height - margin_v - (s + 1) * seg_h
                on = s < lit
                rv, gv, bv, _ = self._amp_color(t)
                cr.set_source_rgba(rv, gv, bv, 0.95 if on else 0.14)
                self._rounded_bar(cr, x, y + seg_h * GAP_FRAC / 2,
                                   bar_w, seg_h * (1 - GAP_FRAC), led_r)
                cr.fill()

            peak_seg = int(peak * SEGMENTS)
            if peak_seg > 0:
                py = height - margin_v - peak_seg * seg_h
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.85)
                cr.rectangle(x, py - 2, bar_w, 2)
                cr.fill()

        draw_channel(bar_gap, low_norm, low_peak)
        draw_channel(bar_gap * 2 + bar_w, high_norm, high_peak)

    # ── Modo 7: Partículas — ondas concéntricas + partículas orbitales ─────────

    def _draw_particles(self, cr, width, height, _cairo):
        import math
        cx, cy = width / 2, height / 2
        r_max  = min(width, height) * 0.46
        D      = self.DISPLAY_BANDS

        overall = sum(self._norm(m) for m in self._mags[:D]) / D

        # Halo de fondo pulsante según energía general
        glow = _cairo.RadialGradient(cx, cy, 0, cx, cy, r_max * 0.9)
        glow.add_color_stop_rgba(0.0, 0.15, 0.55, 0.95, 0.20 * overall)
        glow.add_color_stop_rgba(1.0, 0.15, 0.55, 0.95, 0.0)
        cr.set_source(glow)
        cr.arc(cx, cy, r_max * 0.9, 0, 2 * math.pi)
        cr.fill()

        # Ondas concéntricas nacidas en golpes de graves — mezcladas con blanco
        # para que se distingan del racimo de partículas en vez de fundirse con él
        for wave in self._waves:
            progress = wave['progress']
            r     = progress * r_max
            alpha = (1.0 - progress) ** 0.6
            rv, gv, bv, _ = self._amp_color(min(1.0, wave['strength']))
            rv, gv, bv = (rv + 1.0) / 2, (gv + 1.0) / 2, (bv + 1.0) / 2
            cr.set_source_rgba(rv, gv, bv, alpha)
            cr.set_line_width(3.0 * (1.0 - progress) + 1.0)
            cr.arc(cx, cy, max(1.0, r), 0, 2 * math.pi)
            cr.stroke()

        # Partículas orbitales — una por banda, tamaño/brillo según su amplitud
        for i in range(D):
            norm  = self._norm(self._mags[i])
            angle = self._particle_phase + i * (2 * math.pi / D)
            r     = r_max * 0.25 + norm * r_max * 0.65
            x     = cx + r * math.cos(angle)
            y     = cy + r * math.sin(angle)
            rv, gv, bv, _ = self._amp_color(norm)
            size = 1.5 + norm * 4.5
            cr.set_source_rgba(rv, gv, bv, 0.55 + norm * 0.4)
            cr.arc(x, y, size, 0, 2 * math.pi)
            cr.fill()

        # Núcleo central pulsante
        core_r = r_max * 0.12 * (0.8 + overall * 0.6)
        core_grad = _cairo.RadialGradient(cx, cy, 0, cx, cy, max(1.0, core_r))
        core_grad.add_color_stop_rgba(0.0, 1.0, 1.0, 1.0, 0.9)
        core_grad.add_color_stop_rgba(1.0, 0.2, 0.8, 0.6, 0.0)
        cr.set_source(core_grad)
        cr.arc(cx, cy, max(1.0, core_r), 0, 2 * math.pi)
        cr.fill()


# ── Main Window ────────────────────────────────────────────────────────────────

class RadioWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title('ÆRx Player')
        self.set_default_size(960, 640)
        self.set_size_request(480, 500)

        self._player = Player()
        self._player.connect('metadata-changed', self._on_metadata)
        self._player.connect('cover-data',       self._on_cover_data)
        self._player.connect('state-changed',    self._on_state_changed)
        self._player.connect('error',            self._on_player_error)
        self._player.connect('spectrum',         self._on_spectrum)
        self._player.connect('eos',              self._on_eos)

        self._current_station     = None
        self._current_file        = None
        self._current_episode     = None
        self._podcasts_data       = _load_podcasts_data()
        self._current_podcast_show = None
        self._podcast_search      = None
        self._podcast_shows_stack = None
        self._podcast_sub_list    = None
        self._podcast_results_list = None
        self._podcast_episode_list = None
        self._podcast_show_title_label = None
        self._podcast_progress_save_timer = None
        self._station_rows: dict[str, StationRow] = {}
        self._position_timer      = None
        self._is_radio            = True
        self._current_track_index = -1
        self._known_paths: set[str] = set()
        self._cache_save_timer    = None
        self._split_view          = None
        self._sidebar_btn         = None
        self._mode_btn            = None
        self._genre_headers: dict[str, GenreHeaderRow] = {}
        self._collapsed_genres: set[str] = set()
        self._nav_rows: dict[str, Gtk.ListBoxRow] = {}
        self._nav_list_bottom     = None
        self._update_check_btn    = None
        self._notif_check_btn     = None
        self._section_stack       = None
        self._sidebar_stack       = None
        self._section_title_label = None
        self._content_stack       = None
        self._home_flowbox        = None
        self._favorites_list      = None
        self._favorites_search    = None
        self._explore_list        = None
        self._explore_search      = None
        self._explore_status      = None
        self._explore_stack       = None
        self._header_search       = None
        self._sleep_timer_id      = None
        self._sleep_remaining     = 0
        self._current_cover_data  = None
        self._cover_fullscreen_active = False

        self._config       = _load_config()
        self._theme_mode   = self._config.get('theme_mode', 'system')
        Adw.StyleManager.get_default().set_color_scheme(
            self._THEME_SCHEME_MAP.get(self._theme_mode, Adw.ColorScheme.DEFAULT)
        )

        self._install_material_icons()
        self._install_cover_bg_css()
        self._install_material_css()
        self._sleep_btn           = None
        self._mp3_sort_mode       = 'filename'
        self._mp3_sort_btn        = None
        self._muted               = False
        self._last_nonzero_vol    = 0.8
        self._vol_btn              = None
        self._last_notified_title = ''

        self._favorites: set[str] = set(self._config.get('favorites', []))
        self._music_folder = self._config.get(
            'music_folder', str(Path.home() / 'musica')
        )
        self._check_updates_on_startup = self._config.get('check_updates_on_startup', True)
        self._desktop_notifications    = self._config.get('desktop_notifications', True)
        self._saved_volume  = self._config.get('volume', 0.8)
        if self._saved_volume > 0.0001:
            self._last_nonzero_vol = self._saved_volume
        self._play_mode     = self._config.get('play_mode', 'sequential')
        self._volume_save_timer = None
        self._player.set_volume(self._saved_volume)

        self._build_ui()
        self.connect('close-request', self._on_close_request)
        GLib.idle_add(self._load_builtin_stations)
        GLib.idle_add(self._load_persistent_mp3s)
        if self._check_updates_on_startup:
            GLib.timeout_add(1500, self._check_updates_silently)

    # ── UI construction ────────────────────────────────────────────────────────

    def _install_cover_bg_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(_COVER_BG_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _install_material_icons(self):
        """Registra data/icons como raíz de tema de iconos adicional, para que los
        -symbolic propios (Material Symbols descargados de fonts.google.com/icons)
        se recoloreen automáticamente igual que los iconos symbolic de sistema."""
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_search_path(str(DATA_DIR / 'icons'))

    def _install_material_css(self):
        """Carga la línea de diseño Material Design 3: forma/tipografía/elevación
        (fija) + paleta de color (claro u oscuro, reactiva al tema del sistema)."""
        display = Gdk.Display.get_default()

        base_provider = Gtk.CssProvider()
        base_provider.load_from_path(str(DATA_DIR / 'style-m3-base.css'))
        Gtk.StyleContext.add_provider_for_display(
            display, base_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._m3_scheme_provider = None
        style_manager = Adw.StyleManager.get_default()
        style_manager.connect('notify::dark', lambda *_a: self._apply_m3_scheme())
        self._apply_m3_scheme()

    def _apply_m3_scheme(self):
        display = Gdk.Display.get_default()
        if self._m3_scheme_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._m3_scheme_provider)

        dark = Adw.StyleManager.get_default().get_dark()
        fname = 'style-m3-dark.css' if dark else 'style-m3-light.css'
        provider = Gtk.CssProvider()
        provider.load_from_path(str(DATA_DIR / fname))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        )
        self._m3_scheme_provider = provider

    def _update_cover_display_mode(self):
        """Decide fullscreen-blur-background vs small-thumbnail mode for the cover."""
        cover_data = self._current_cover_data
        show_sidebar = self._split_view.get_show_sidebar() if self._split_view else True

        use_fullscreen = False
        native_pb = None
        if cover_data and not show_sidebar:
            native_pb = _cover_native_pixbuf(cover_data)
            if native_pb and min(native_pb.get_width(), native_pb.get_height()) >= _FULLSCREEN_MIN_SIDE:
                use_fullscreen = True

        if use_fullscreen == self._cover_fullscreen_active:
            return
        self._cover_fullscreen_active = use_fullscreen

        if use_fullscreen and native_pb is not None:
            blurred = _blur_pixbuf(native_pb)
            self._cover_bg_picture.set_pixbuf(blurred)
            self._cover_bg_picture.set_visible(True)
            self._cover_dim_layer.set_visible(True)
        else:
            self._cover_bg_picture.set_visible(False)
            self._cover_dim_layer.set_visible(False)

    def _build_ui(self):
        root = Adw.ToolbarView()
        self.set_content(root)

        header = Adw.HeaderBar()
        header.add_css_class('flat')

        if _HAS_OVERLAY_SPLIT:
            self._sidebar_btn = Gtk.ToggleButton()
            self._sidebar_btn.add_css_class('flat')
            self._sidebar_btn.set_tooltip_text('Mostrar/ocultar panel lateral')
            self._sidebar_btn.set_active(True)

            logo_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            logo_icon = Gtk.Image.new_from_file(str(DATA_DIR / 'icons' / 'aerx-mark.svg'))
            logo_icon.set_pixel_size(28)
            logo_box.append(logo_icon)
            logo_label = Gtk.Label(label='ÆRx')
            logo_label.add_css_class('heading')
            logo_box.append(logo_label)
            self._sidebar_btn.set_child(logo_box)

            header.pack_start(self._sidebar_btn)

        # Ajustes y Acerca de viven ahora en el rail de navegación (parte
        # baja del menú), no en la cabecera.

        self._header_search = Gtk.SearchEntry()
        self._header_search.set_placeholder_text('Buscar emisoras, géneros o podcasts…')
        self._header_search.set_hexpand(False)
        self._header_search.set_size_request(360, -1)
        self._header_search.connect('activate', self._on_header_search)
        header.set_title_widget(self._header_search)

        root.add_top_bar(header)

        nav_rail = self._build_nav_rail()

        now_playing = self._build_now_playing()
        now_playing.add_css_class('now-playing-translucent')

        self._cover_bg_picture = Gtk.Picture()
        self._cover_bg_picture.set_content_fit(Gtk.ContentFit.COVER)
        self._cover_bg_picture.set_can_shrink(True)
        self._cover_bg_picture.set_visible(False)

        self._cover_dim_layer = Gtk.Box()
        self._cover_dim_layer.add_css_class('cover-dim-layer')
        self._cover_dim_layer.set_visible(False)

        content_overlay = Gtk.Overlay()
        content_overlay.set_child(self._cover_bg_picture)
        content_overlay.add_overlay(self._cover_dim_layer)
        content_overlay.add_overlay(now_playing)

        # Content slot: swaps between the Home dashboard, the shared
        # now-playing panel (used by Radio/Local Music/Favorites) and Explore.
        self._content_stack = Adw.ViewStack()
        self._content_stack.add_named(self._build_home_page(), 'home')
        self._content_stack.add_named(content_overlay, 'player')
        self._content_stack.add_named(self._build_explore_page(), 'explore')
        self._content_stack.add_named(self._build_settings_page(), 'settings')

        if _HAS_OVERLAY_SPLIT:
            self._split_view = Adw.OverlaySplitView()
            self._split_view.set_sidebar(nav_rail)
            self._split_view.set_content(self._content_stack)
            self._split_view.set_sidebar_width_fraction(0.38)
            self._split_view.set_min_sidebar_width(260)
            self._split_view.set_max_sidebar_width(440)
            self._sidebar_btn.bind_property(
                'active', self._split_view, 'show-sidebar',
                GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
            )
            self._split_view.connect('notify::show-sidebar',
                                      lambda *_a: self._update_cover_display_mode())
            if _HAS_BREAKPOINT:
                try:
                    cond = Adw.BreakpointCondition.parse('max-width: 640sp')
                    bp   = Adw.Breakpoint.new(cond)
                    bp.add_setter(self._split_view, 'collapsed', True)
                    self.add_breakpoint(bp)
                except Exception:
                    pass
            split_container = self._split_view
        else:
            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            paned.set_position(340)
            paned.set_shrink_start_child(False)
            paned.set_shrink_end_child(False)
            paned.set_start_child(nav_rail)
            paned.set_end_child(self._content_stack)
            split_container = paned

        controls = self._build_controls()

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(split_container)
        content_box.append(controls)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(content_box)
        root.set_content(self._toast_overlay)

        self._nav_list.select_row(self._nav_rows['home'])

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self.add_controller(key_ctrl)

    _NAV_LABELS = {
        'radio': 'Radio', 'local': 'Música local', 'favorites': 'Favoritos',
        'podcasts': 'Podcasts',
    }

    _THEME_SCHEME_MAP = {
        'system': Adw.ColorScheme.DEFAULT,
        'light':  Adw.ColorScheme.FORCE_LIGHT,
        'dark':   Adw.ColorScheme.FORCE_DARK,
    }

    def _build_nav_rail(self) -> Gtk.Widget:
        """Left column, drill-down style: shows either the section MENU
        (logo + 5 nav rows) or, for sections with a list (Radio/Local
        Music/Favorites), that section's LIST with a back arrow to return
        to the menu — never both at once. Home/Explore have no list of
        their own, so selecting them always keeps the menu visible."""
        self._sidebar_stack = Gtk.Stack()
        self._sidebar_stack.set_vexpand(True)

        # ── "menu" page: section navigation (logo now lives in the
        # headerbar, replacing the old plain toggle icon) ──
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        menu_box.set_margin_top(8)

        self._nav_list = Gtk.ListBox()
        self._nav_list.add_css_class('navigation-sidebar')
        self._nav_list.set_selection_mode(Gtk.SelectionMode.SINGLE)

        for nav_id, icon_name, label_text in (
            ('home',      'm3-home-symbolic',          'Inicio'),
            ('radio',     'm3-radio-symbolic',         'Radio'),
            ('local',     'm3-library-music-symbolic', 'Música local'),
            ('favorites', 'm3-star-symbolic',           'Favoritos'),
            ('podcasts',  'm3-podcasts-symbolic',       'Podcasts'),
            ('explore',   'm3-explore-symbolic',        'Explorar'),
        ):
            row = Gtk.ListBoxRow()
            row.nav_id = nav_id
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_start(12); row_box.set_margin_end(12)
            row_box.set_margin_top(9);    row_box.set_margin_bottom(9)
            row_box.append(Gtk.Image.new_from_icon_name(icon_name))
            label = Gtk.Label(label=label_text)
            label.set_xalign(0)
            row_box.append(label)
            row.set_child(row_box)
            self._nav_list.append(row)
            self._nav_rows[nav_id] = row

        # 'row-selected' covers programmatic self._nav_list.select_row(...)
        # (Home cards, Explore add, etc.); 'row-activated' additionally
        # covers the user re-clicking the row that's already selected —
        # select_row() alone wouldn't re-emit 'row-selected' for that case,
        # which would leave the rail stuck on the list page with no way
        # back in short of the back arrow.
        self._nav_list.connect('row-selected', self._on_nav_selected)
        self._nav_list.connect('row-activated', self._on_nav_selected)
        menu_box.append(self._nav_list)

        # Spacer pushes Ajustes/Acerca de to the bottom of the menu.
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        menu_box.append(spacer)

        self._nav_list_bottom = Gtk.ListBox()
        self._nav_list_bottom.add_css_class('navigation-sidebar')
        self._nav_list_bottom.set_selection_mode(Gtk.SelectionMode.SINGLE)

        for nav_id, icon_name, label_text, selectable in (
            ('settings', 'm3-settings-symbolic', 'Ajustes', True),
            # "Acerca de" abre un modal — no es un destino de navegación,
            # así que no debe quedar marcado como sección activa.
            ('about',    'm3-info-symbolic',     'Acerca de', False),
        ):
            row = Gtk.ListBoxRow()
            row.nav_id = nav_id
            row.set_selectable(selectable)
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.set_margin_start(12); row_box.set_margin_end(12)
            row_box.set_margin_top(9);    row_box.set_margin_bottom(9)
            row_box.append(Gtk.Image.new_from_icon_name(icon_name))
            label = Gtk.Label(label=label_text)
            label.set_xalign(0)
            row_box.append(label)
            row.set_child(row_box)
            self._nav_list_bottom.append(row)
            self._nav_rows[nav_id] = row

        self._nav_list_bottom.connect('row-selected', self._on_nav_selected)
        self._nav_list_bottom.connect('row-activated', self._on_nav_selected)
        menu_box.append(self._nav_list_bottom)

        # ── "list" page: back arrow + section title + that section's list ──
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        back_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_row.set_margin_start(4); back_row.set_margin_end(12)
        back_row.set_margin_top(8);   back_row.set_margin_bottom(8)
        back_btn = Gtk.Button()
        back_btn.set_icon_name('m3-arrow-back-symbolic')
        back_btn.set_tooltip_text('Volver al menú')
        back_btn.add_css_class('flat')
        back_btn.add_css_class('circular')
        back_btn.connect('clicked', self._on_nav_back)
        back_row.append(back_btn)
        self._section_title_label = Gtk.Label()
        self._section_title_label.add_css_class('title-2')
        self._section_title_label.set_xalign(0)
        back_row.append(self._section_title_label)
        list_box.append(back_row)
        list_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._section_stack = Adw.ViewStack()
        self._section_stack.set_vexpand(True)
        self._section_stack.add_named(self._build_radio_page(), 'radio')
        self._section_stack.add_named(self._build_mp3_page(), 'local')
        self._section_stack.add_named(self._build_favorites_page(), 'favorites')
        self._section_stack.add_named(self._build_podcasts_page(), 'podcasts')
        list_box.append(self._section_stack)

        self._sidebar_stack.add_named(menu_box, 'menu')
        self._sidebar_stack.add_named(list_box, 'list')
        return self._sidebar_stack

    def _on_nav_back(self, _btn):
        self._sidebar_stack.set_visible_child_name('menu')

    def _on_nav_selected(self, listbox, row):
        if row is None:
            return
        nav_id = row.nav_id

        if nav_id == 'about':
            self._on_about(None)
            return

        # The two nav lists (main + bottom) each keep their own selection
        # state — clear the other one so only one pill is ever lit.
        other = self._nav_list_bottom if listbox is self._nav_list else self._nav_list
        other.unselect_all()

        content_page = {
            'home': 'home', 'radio': 'player', 'local': 'player',
            'favorites': 'player', 'podcasts': 'player',
            'explore': 'explore', 'settings': 'settings',
        }[nav_id]
        self._content_stack.set_visible_child_name(content_page)

        has_list = nav_id in self._NAV_LABELS
        if has_list:
            self._section_stack.set_visible_child_name(nav_id)
            self._section_title_label.set_label(self._NAV_LABELS[nav_id])
            self._sidebar_stack.set_visible_child_name('list')
        else:
            self._sidebar_stack.set_visible_child_name('menu')

        if self._mode_btn is not None:
            self._mode_btn.set_visible(nav_id == 'local')

    def _build_home_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(24); box.set_margin_end(24)
        box.set_margin_top(20);   box.set_margin_bottom(16)

        # Hero: banner degradado (torre de radio + cordillera, motivo de la
        # guía de marca) con el saludo superpuesto — como en el mockup,
        # en vez de un simple texto suelto sobre el fondo de la app.
        hero = Gtk.Overlay()
        hero.add_css_class('card')
        hero.set_overflow(Gtk.Overflow.HIDDEN)
        hero.set_size_request(-1, 160)
        hero.set_margin_bottom(16)

        hero_pic = Gtk.Picture.new_for_filename(str(DATA_DIR / 'hero-banner.png'))
        hero_pic.set_content_fit(Gtk.ContentFit.COVER)
        hero_pic.set_can_shrink(True)
        # Sin esto, Picture se dimensiona según su propio aspecto (más alto
        # que los 160px del hero) en vez de rellenar la caja entera, y
        # content_fit=COVER nunca llega a recortar — se veía el fondo del
        # .card asomando por arriba/abajo de la imagen.
        hero_pic.set_hexpand(True)
        hero_pic.set_vexpand(True)
        hero.set_child(hero_pic)

        # Logo arriba a la izquierda, alineado con el texto de abajo (mismo
        # margen izquierdo) para que ambos lean como una sola columna.
        hero_logo = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        hero_logo.set_valign(Gtk.Align.START)
        hero_logo.set_halign(Gtk.Align.START)
        hero_logo.set_margin_start(20); hero_logo.set_margin_top(16)

        hero_logo_icon = Gtk.Image.new_from_file(str(DATA_DIR / 'icons' / 'aerx-mark.svg'))
        hero_logo_icon.set_pixel_size(51)
        hero_logo.append(hero_logo_icon)

        hero_logo_label = Gtk.Label(label='ÆRx')
        hero_logo_label.add_css_class('m3-hero-logo-text')
        hero_logo_label.add_css_class('m3-hero-title')
        hero_logo.append(hero_logo_label)

        hero.add_overlay(hero_logo)

        hero_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hero_text.set_valign(Gtk.Align.END)
        hero_text.set_halign(Gtk.Align.START)
        hero_text.set_margin_start(20); hero_text.set_margin_bottom(16)

        greeting = Gtk.Label(label='Buenas vibras')
        greeting.add_css_class('title-1')
        greeting.add_css_class('m3-hero-title')
        greeting.set_xalign(0)
        hero_text.append(greeting)

        subtitle = Gtk.Label(label='Radio y Audio, sin fronteras.')
        subtitle.add_css_class('body')
        subtitle.add_css_class('m3-hero-subtitle')
        subtitle.set_xalign(0)
        hero_text.append(subtitle)

        hero.add_overlay(hero_text)
        box.append(hero)

        section_lbl = Gtk.Label(label='Emisoras destacadas')
        section_lbl.add_css_class('heading')
        section_lbl.set_xalign(0)
        section_lbl.set_margin_bottom(8)
        box.append(section_lbl)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self._home_flowbox = Gtk.FlowBox()
        self._home_flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._home_flowbox.set_homogeneous(True)
        self._home_flowbox.set_row_spacing(12)
        self._home_flowbox.set_column_spacing(12)
        self._home_flowbox.set_max_children_per_line(6)
        self._home_flowbox.set_valign(Gtk.Align.START)
        scroll.set_child(self._home_flowbox)
        box.append(scroll)

        return box

    _CARD_ART_PALETTE = (
        'm3-card-art-1', 'm3-card-art-2', 'm3-card-art-3',
        'm3-card-art-4', 'm3-card-art-5', 'm3-card-art-6',
    )

    def _build_station_card(self, station: dict) -> Gtk.Widget:
        """M3 '.card' tile for the Home grid, styled after the ÆRx brand
        mockup: a full-bleed colour block (deterministic per station) fills
        the top of the tile with the station logo centered on it; name +
        genre sit below as a left-aligned caption on the card surface. One
        tile that reads as art + caption, not an icon and a text pill
        stacked as separate stickers."""
        card = Gtk.Button()
        card.add_css_class('flat')
        card.set_tooltip_text(station.get('name', ''))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.add_css_class('card')
        outer.set_overflow(Gtk.Overflow.HIDDEN)
        outer.set_size_request(140, -1)

        name = station.get('name', '')
        palette = self._CARD_ART_PALETTE[
            zlib.crc32(name.encode('utf-8')) % len(self._CARD_ART_PALETTE)
        ]
        art = Gtk.Box(halign=Gtk.Align.FILL, valign=Gtk.Align.FILL)
        art.set_hexpand(True)
        art.add_css_class('m3-card-art')
        art.add_css_class(palette)
        art.set_size_request(-1, 96)

        img = Gtk.Image.new_from_icon_name('m3-radio-symbolic')
        img.set_pixel_size(40)
        img.set_halign(Gtk.Align.CENTER)
        img.set_valign(Gtk.Align.CENTER)
        img.set_hexpand(True)
        img.set_vexpand(True)
        art.append(img)
        outer.append(art)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_margin_top(8); text_box.set_margin_bottom(10)
        text_box.set_margin_start(10); text_box.set_margin_end(10)

        name_label = Gtk.Label(label=name)
        name_label.add_css_class('body')
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_max_width_chars(15)
        name_label.set_xalign(0)
        text_box.append(name_label)

        genre = station.get('genre', '')
        if isinstance(genre, list):
            genre = ', '.join(genre[:1])
        genre_label = Gtk.Label(label=str(genre)[:20] if genre else '​')
        genre_label.add_css_class('caption')
        genre_label.add_css_class('dim-label')
        genre_label.set_ellipsize(Pango.EllipsizeMode.END)
        genre_label.set_max_width_chars(18)
        genre_label.set_xalign(0)
        text_box.append(genre_label)

        outer.append(text_box)

        card.set_child(outer)
        card.connect('clicked', lambda _b, s=station: self._on_home_card_clicked(s))

        favicon = station.get('favicon') or station.get('favicon_url', '')
        if favicon and favicon.startswith('http'):
            radio_browser.fetch_image(
                favicon,
                lambda data, err, im=img: self._set_card_logo(im, data),
            )
        return card

    def _set_card_logo(self, img: Gtk.Image, data: bytes):
        if not data:
            return
        pb = _pixbuf_from_bytes(data, 64)
        if pb:
            GLib.idle_add(img.set_from_pixbuf, pb)

    def _on_home_card_clicked(self, station: dict):
        row = self._station_rows.get(station.get('url', ''))
        if row:
            self._nav_list.select_row(self._nav_rows['radio'])
            self._on_station_activated(self._radio_list, row)

    def _refresh_home_cards(self):
        if self._home_flowbox is None:
            return
        while child := self._home_flowbox.get_first_child():
            self._home_flowbox.remove(child)
        stations = [
            self._station_rows[u].station for u in self._favorites
            if u in self._station_rows
        ]
        if not stations:
            stations = [row.station for row in list(self._station_rows.values())[:12]]
        for station in stations:
            self._home_flowbox.append(self._build_station_card(station))

    def _build_favorites_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        search_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_bar.set_margin_start(8); search_bar.set_margin_end(8)
        search_bar.set_margin_top(8);   search_bar.set_margin_bottom(4)

        self._favorites_search = Gtk.SearchEntry()
        self._favorites_search.set_placeholder_text('Buscar en favoritos…')
        self._favorites_search.set_hexpand(True)
        self._favorites_search.connect(
            'search-changed', lambda _w: self._favorites_list.invalidate_filter()
        )
        search_bar.append(self._favorites_search)
        box.append(search_bar)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self._favorites_list = Gtk.ListBox()
        self._favorites_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._favorites_list.add_css_class('boxed-list')
        self._favorites_list.set_margin_start(8)
        self._favorites_list.set_margin_end(8)
        self._favorites_list.set_margin_bottom(8)
        self._favorites_list.connect('row-activated', self._on_favorites_row_activated)
        self._favorites_list.set_filter_func(self._favorites_filter_func)
        scroll.set_child(self._favorites_list)
        box.append(scroll)

        return box

    def _favorites_filter_func(self, row):
        query = self._favorites_search.get_text().lower().strip()
        if not query:
            return True
        name  = row.station.get('name', '').lower()
        genre = str(row.station.get('genre', '')).lower()
        return query in name or query in genre

    def _on_favorites_row_activated(self, _listbox, row):
        if not isinstance(row, StationRow):
            return
        real_row = self._station_rows.get(row.station.get('url', ''))
        if real_row:
            self._on_station_activated(self._radio_list, real_row)

    def _refresh_favorites_page(self):
        if self._favorites_list is None:
            return
        while child := self._favorites_list.get_first_child():
            self._favorites_list.remove(child)
        for url in self._favorites:
            src = self._station_rows.get(url)
            if not src:
                continue
            row = StationRow(src.station, is_favorite=True, on_toggle_fav=self._toggle_favorite)
            if src.logo_bytes:
                row.set_logo_bytes(src.logo_bytes)
            else:
                favicon = src.station.get('favicon') or src.station.get('favicon_url', '')
                if favicon and favicon.startswith('http'):
                    radio_browser.fetch_image(
                        favicon,
                        lambda data, err, r=row: r.set_logo_bytes(data) if data else None,
                    )
            self._favorites_list.append(row)

    def _on_favorites_changed(self):
        """Call after self._favorites is mutated, from wherever a star was
        toggled (main list, Explore, Home cards), to keep Home and the
        Favorites page in sync."""
        self._refresh_home_cards()
        self._refresh_favorites_page()

    # ── Podcasts ─────────────────────────────────────────────────────────────

    def _build_podcasts_page(self) -> Gtk.Widget:
        """Drill-down propio de 2 niveles dentro de la sección: 'shows'
        (buscar/suscripciones) ↔ 'episodes' (lista de un show abierto),
        sin tocar el back-arrow del rail exterior (ese siempre vuelve al
        menú principal; el de aquí vuelve de episodios a shows)."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self._podcast_stack = Gtk.Stack()
        self._podcast_stack.set_vexpand(True)

        # ── página "shows": buscador + suscripciones/resultados ──
        shows_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        search_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_bar.set_margin_start(8); search_bar.set_margin_end(8)
        search_bar.set_margin_top(8);   search_bar.set_margin_bottom(4)
        self._podcast_search = Gtk.SearchEntry()
        self._podcast_search.set_placeholder_text('Buscar podcasts…')
        self._podcast_search.set_hexpand(True)
        self._podcast_search.connect('activate', self._on_podcast_search)
        self._podcast_search.connect('search-changed', self._on_podcast_search_changed)
        search_bar.append(self._podcast_search)
        shows_page.append(search_bar)

        self._podcast_shows_stack = Gtk.Stack()
        self._podcast_shows_stack.set_vexpand(True)

        sub_scroll = Gtk.ScrolledWindow()
        sub_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sub_scroll.set_vexpand(True)
        self._podcast_sub_list = Gtk.ListBox()
        self._podcast_sub_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._podcast_sub_list.add_css_class('boxed-list')
        self._podcast_sub_list.set_margin_start(8)
        self._podcast_sub_list.set_margin_end(8)
        self._podcast_sub_list.set_margin_bottom(8)
        self._podcast_sub_list.connect('row-activated', self._on_podcast_show_row_activated)
        sub_scroll.set_child(self._podcast_sub_list)
        self._podcast_shows_stack.add_named(sub_scroll, 'subscriptions')

        res_scroll = Gtk.ScrolledWindow()
        res_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        res_scroll.set_vexpand(True)
        self._podcast_results_list = Gtk.ListBox()
        self._podcast_results_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._podcast_results_list.add_css_class('boxed-list')
        self._podcast_results_list.set_margin_start(8)
        self._podcast_results_list.set_margin_end(8)
        self._podcast_results_list.set_margin_bottom(8)
        self._podcast_results_list.connect('row-activated', self._on_podcast_show_row_activated)
        res_scroll.set_child(self._podcast_results_list)
        self._podcast_shows_stack.add_named(res_scroll, 'results')

        shows_page.append(self._podcast_shows_stack)
        self._podcast_stack.add_named(shows_page, 'shows')

        # ── página "episodes": atrás + título + lista de episodios ──
        episodes_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ep_back_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ep_back_row.set_margin_start(4); ep_back_row.set_margin_end(12)
        ep_back_row.set_margin_top(8);   ep_back_row.set_margin_bottom(4)
        ep_back_btn = Gtk.Button()
        ep_back_btn.set_icon_name('m3-arrow-back-symbolic')
        ep_back_btn.set_tooltip_text('Volver a Podcasts')
        ep_back_btn.add_css_class('flat')
        ep_back_btn.add_css_class('circular')
        ep_back_btn.connect('clicked', self._on_podcast_shows_back)
        ep_back_row.append(ep_back_btn)
        self._podcast_show_title_label = Gtk.Label()
        self._podcast_show_title_label.add_css_class('heading')
        self._podcast_show_title_label.set_xalign(0)
        self._podcast_show_title_label.set_ellipsize(Pango.EllipsizeMode.END)
        ep_back_row.append(self._podcast_show_title_label)
        episodes_page.append(ep_back_row)

        ep_scroll = Gtk.ScrolledWindow()
        ep_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        ep_scroll.set_vexpand(True)
        self._podcast_episode_list = Gtk.ListBox()
        self._podcast_episode_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._podcast_episode_list.add_css_class('boxed-list')
        self._podcast_episode_list.set_margin_start(8)
        self._podcast_episode_list.set_margin_end(8)
        self._podcast_episode_list.set_margin_bottom(8)
        self._podcast_episode_list.connect('row-activated', self._on_episode_activated)
        ep_scroll.set_child(self._podcast_episode_list)
        episodes_page.append(ep_scroll)

        self._podcast_stack.add_named(episodes_page, 'episodes')

        outer.append(self._podcast_stack)
        self._refresh_podcast_subscriptions()
        return outer

    def _set_generic_logo(self, img: Gtk.Image, data: bytes):
        if not data:
            return
        pb = _pixbuf_from_bytes(data, 40)
        if pb:
            GLib.idle_add(img.set_from_pixbuf, pb)

    def _build_podcast_show_row(self, show: dict, subscribed: bool) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.show_meta = show
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_start(8); box.set_margin_end(8)
        box.set_margin_top(6);   box.set_margin_bottom(6)
        row.set_child(box)

        logo = Gtk.Image()
        logo.set_pixel_size(40)
        logo.set_size_request(40, 40)
        logo.set_from_icon_name('m3-podcasts-symbolic')
        box.append(logo)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.set_hexpand(True)
        vbox.set_overflow(Gtk.Overflow.HIDDEN)
        box.append(vbox)

        name_label = Gtk.Label(label=show.get('name', ''))
        name_label.set_xalign(0)
        name_label.add_css_class('body')
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(name_label)

        artist_label = Gtk.Label(label=show.get('artist', ''))
        artist_label.set_xalign(0)
        artist_label.add_css_class('caption')
        artist_label.add_css_class('dim-label')
        artist_label.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(artist_label)

        sub_btn = Gtk.Button()
        sub_btn.set_icon_name('m3-star-symbolic' if subscribed else 'm3-star-outline-symbolic')
        sub_btn.set_tooltip_text('Cancelar suscripción' if subscribed else 'Suscribirse')
        sub_btn.add_css_class('flat')
        sub_btn.add_css_class('circular')
        sub_btn.set_valign(Gtk.Align.CENTER)
        sub_btn.connect('clicked', lambda b, s=show: self._on_podcast_subscribe_toggle(s, b))
        box.append(sub_btn)

        artwork = show.get('artwork_url', '')
        if artwork and artwork.startswith('http'):
            radio_browser.fetch_image(
                artwork, lambda data, err, im=logo: self._set_generic_logo(im, data))

        return row

    def _refresh_podcast_subscriptions(self):
        if self._podcast_sub_list is None:
            return
        while child := self._podcast_sub_list.get_first_child():
            self._podcast_sub_list.remove(child)
        subs = self._podcasts_data.get('subscriptions', [])
        if not subs:
            placeholder = Gtk.ListBoxRow()
            placeholder.set_selectable(False)
            placeholder.set_activatable(False)
            lbl = Gtk.Label(label='Aún no sigues ningún podcast. Búscalos arriba.')
            lbl.add_css_class('dim-label')
            lbl.set_wrap(True)
            lbl.set_margin_top(24); lbl.set_margin_bottom(24)
            lbl.set_margin_start(12); lbl.set_margin_end(12)
            placeholder.set_child(lbl)
            self._podcast_sub_list.append(placeholder)
            return
        for sub in subs:
            self._podcast_sub_list.append(self._build_podcast_show_row(sub, subscribed=True))

    def _is_podcast_subscribed(self, feed_url: str) -> bool:
        return any(s.get('feed_url') == feed_url
                   for s in self._podcasts_data.get('subscriptions', []))

    def _on_podcast_subscribe_toggle(self, show: dict, btn: Gtk.Button):
        feed_url = show.get('feed_url', '')
        subs = self._podcasts_data.setdefault('subscriptions', [])
        if self._is_podcast_subscribed(feed_url):
            self._podcasts_data['subscriptions'] = [
                s for s in subs if s.get('feed_url') != feed_url]
            btn.set_icon_name('m3-star-outline-symbolic')
            btn.set_tooltip_text('Suscribirse')
        else:
            subs.append({
                'feed_url':      feed_url,
                'name':          show.get('name', ''),
                'artist':        show.get('artist', ''),
                'artwork_url':   show.get('artwork_url', ''),
                'subscribed_at': datetime.datetime.now().isoformat(),
            })
            btn.set_icon_name('m3-star-symbolic')
            btn.set_tooltip_text('Cancelar suscripción')
        threading.Thread(target=lambda: _save_podcasts_data(self._podcasts_data), daemon=True).start()
        self._refresh_podcast_subscriptions()

    def _on_podcast_search_changed(self, entry):
        if not entry.get_text().strip():
            self._podcast_shows_stack.set_visible_child_name('subscriptions')

    def _on_podcast_search(self, entry):
        query = entry.get_text().strip()
        if not query:
            self._podcast_shows_stack.set_visible_child_name('subscriptions')
            return
        while child := self._podcast_results_list.get_first_child():
            self._podcast_results_list.remove(child)
        self._podcast_shows_stack.set_visible_child_name('results')
        podcasts.search_shows(query, self._on_podcast_search_result)

    def _on_podcast_search_result(self, shows, err):
        def _apply():
            while child := self._podcast_results_list.get_first_child():
                self._podcast_results_list.remove(child)
            if err or not shows:
                placeholder = Gtk.ListBoxRow()
                placeholder.set_selectable(False)
                placeholder.set_activatable(False)
                lbl = Gtk.Label(
                    label=f'Error al buscar: {err}' if err else 'Sin resultados.')
                lbl.add_css_class('dim-label')
                lbl.set_wrap(True)
                lbl.set_margin_top(24); lbl.set_margin_bottom(24)
                lbl.set_margin_start(12); lbl.set_margin_end(12)
                placeholder.set_child(lbl)
                self._podcast_results_list.append(placeholder)
                return
            for show in shows:
                subscribed = self._is_podcast_subscribed(show['feed_url'])
                self._podcast_results_list.append(
                    self._build_podcast_show_row(show, subscribed=subscribed))
        GLib.idle_add(_apply)

    def _on_podcast_show_row_activated(self, _listbox, row):
        show = getattr(row, 'show_meta', None)
        if show:
            self._open_podcast_show(show)

    def _open_podcast_show(self, show: dict):
        self._current_podcast_show = show
        self._podcast_show_title_label.set_label(show.get('name', ''))
        while child := self._podcast_episode_list.get_first_child():
            self._podcast_episode_list.remove(child)
        loading = Gtk.ListBoxRow()
        loading.set_selectable(False)
        loading.set_activatable(False)
        lbl = Gtk.Label(label='Cargando episodios…')
        lbl.add_css_class('dim-label')
        lbl.set_margin_top(24); lbl.set_margin_bottom(24)
        loading.set_child(lbl)
        self._podcast_episode_list.append(loading)
        self._podcast_stack.set_visible_child_name('episodes')
        podcasts.fetch_episodes(show['feed_url'], self._on_podcast_episodes_fetched)

    def _on_podcast_episodes_fetched(self, result, err):
        show_meta, episodes = result if result else (None, None)

        def _apply():
            while child := self._podcast_episode_list.get_first_child():
                self._podcast_episode_list.remove(child)
            if err or not episodes:
                placeholder = Gtk.ListBoxRow()
                placeholder.set_selectable(False)
                placeholder.set_activatable(False)
                lbl = Gtk.Label(
                    label='No se pudieron cargar los episodios.' if err else 'Sin episodios.')
                lbl.add_css_class('dim-label')
                lbl.set_margin_top(24); lbl.set_margin_bottom(24)
                placeholder.set_child(lbl)
                self._podcast_episode_list.append(placeholder)
                return
            for ep in episodes:
                state = self._podcasts_data.get('episodes', {}).get(ep['guid'], {})
                row = EpisodeRow(ep, state,
                                  on_download=self._on_episode_download_clicked,
                                  on_remove_download=self._on_episode_remove_download_clicked)
                artwork = ep.get('artwork_url', '')
                if artwork and artwork.startswith('http'):
                    radio_browser.fetch_image(
                        artwork,
                        lambda data, e, r=row: r.set_logo_bytes(data) if data else None,
                    )
                self._podcast_episode_list.append(row)
        GLib.idle_add(_apply)

    def _on_podcast_shows_back(self, _btn):
        self._podcast_stack.set_visible_child_name('shows')

    def _on_episode_download_clicked(self, row: 'EpisodeRow'):
        ep = row.episode
        dest = _episode_download_path(ep['guid'], ep['audio_url'])
        PODCAST_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        row.set_download_state('downloading')

        def _progress(done, total):
            if total:
                pct = int(done * 100 / total)
                GLib.idle_add(row.set_tooltip_text, f'Descargando… {pct}%')

        def _done(path, err):
            def _apply():
                if err or not path:
                    row.set_download_state('none')
                    self._toast_overlay.add_toast(
                        Adw.Toast(title=f'No se pudo descargar: {err or "error desconocido"}'))
                    return
                entry = self._podcasts_data.setdefault('episodes', {}).setdefault(ep['guid'], {})
                entry.update({
                    'feed_url':       (self._current_podcast_show or {}).get('feed_url', ''),
                    'title':          ep.get('title', ''),
                    'pub_date':       ep.get('pub_date', ''),
                    'audio_url':      ep.get('audio_url', ''),
                    'duration_sec':   ep.get('duration_sec', 0),
                    'downloaded_path': str(path),
                })
                entry.setdefault('listened', False)
                entry.setdefault('position_sec', 0)
                threading.Thread(target=lambda: _save_podcasts_data(self._podcasts_data), daemon=True).start()
                row.set_download_state('done')
            GLib.idle_add(_apply)

        podcasts.download_episode(ep['audio_url'], str(dest), _done, progress_cb=_progress)

    def _on_episode_remove_download_clicked(self, row: 'EpisodeRow'):
        ep = row.episode
        entry = self._podcasts_data.get('episodes', {}).get(ep['guid'])
        if entry and entry.get('downloaded_path'):
            Path(entry['downloaded_path']).unlink(missing_ok=True)
            entry['downloaded_path'] = None
            threading.Thread(target=lambda: _save_podcasts_data(self._podcasts_data), daemon=True).start()
        row.set_download_state('none')

    def _on_episode_activated(self, _listbox, row):
        if not isinstance(row, EpisodeRow):
            return
        ep = row.episode
        show = self._current_podcast_show or {}
        state = self._podcasts_data.get('episodes', {}).get(ep['guid'], {})

        self._current_track_index = -1
        self._is_radio        = False
        self._current_station = None
        self._current_file    = None
        self._current_episode = {**ep, 'show_name': show.get('name', ''), 'row': row}

        self._title_label.set_text(ep.get('title', ''))
        self._artist_label.set_text(show.get('name', ''))
        self._album_label.set_text(_fmt_pub_date(ep.get('pub_date', '')))
        self._set_radio_mode(False)

        self._current_cover_data = None
        self._cover_image.set_from_icon_name('m3-podcasts-symbolic')
        self._cover_image.set_pixel_size(160)
        self._update_cover_display_mode()
        artwork = ep.get('artwork_url') or show.get('artwork_url', '')
        if artwork and artwork.startswith('http'):
            radio_browser.fetch_image(artwork, self._on_episode_cover_fetched)

        downloaded = state.get('downloaded_path')
        if downloaded and Path(downloaded).exists():
            uri = 'file://' + urllib.parse.quote(downloaded)
        else:
            uri = ep['audio_url']
        self._player.play(uri)

        resume_at = state.get('position_sec', 0)
        if resume_at and resume_at > 2 and not state.get('listened'):
            GLib.timeout_add(800, self._seek_resume_once, int(resume_at * Gst.SECOND))

        self._update_meta_chips({'Podcast': show.get('name', '')})

    def _seek_resume_once(self, pos_ns):
        self._player.seek(pos_ns)
        return False

    def _on_episode_cover_fetched(self, data, err):
        if not data:
            return
        pb = _pixbuf_from_bytes(data, 160)
        if not pb:
            return
        def _apply():
            self._current_cover_data = data
            self._cover_image.set_from_pixbuf(pb)
            self._update_cover_display_mode()
        GLib.idle_add(_apply)

    def _build_explore_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_margin_start(16); box.set_margin_end(16)
        box.set_margin_top(16);   box.set_margin_bottom(16)
        box.set_vexpand(True)

        header = Gtk.Label(label='Explorar nuevas emisoras')
        header.add_css_class('title-2')
        header.set_xalign(0)
        box.append(header)

        search_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_bar.set_margin_top(12); search_bar.set_margin_bottom(8)

        self._explore_search = Gtk.SearchEntry()
        self._explore_search.set_placeholder_text('Buscar por género o etiqueta (p.ej. jazz, pop)…')
        self._explore_search.set_hexpand(True)
        self._explore_search.connect('activate', self._on_explore_search)
        search_bar.append(self._explore_search)

        search_btn = Gtk.Button(label='Buscar')
        search_btn.add_css_class('suggested-action')
        search_btn.connect('clicked', self._on_explore_search)
        search_bar.append(search_btn)
        box.append(search_bar)

        self._explore_status = Adw.StatusPage()
        self._explore_status.set_icon_name('m3-explore-symbolic')
        self._explore_status.set_title('Explorar')
        self._explore_status.set_description(
            'Busca por género o etiqueta, o pulsa Buscar para ver emisoras de España.'
        )
        self._explore_status.set_vexpand(True)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self._explore_list = Gtk.ListBox()
        self._explore_list.add_css_class('boxed-list')
        self._explore_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._explore_list.connect('row-activated', self._on_explore_row_activated)
        scroll.set_child(self._explore_list)

        self._explore_stack = Gtk.Stack()
        self._explore_stack.add_named(self._explore_status, 'empty')
        self._explore_stack.add_named(scroll, 'results')
        self._explore_stack.set_vexpand(True)
        box.append(self._explore_stack)

        return box

    def _on_header_search(self, entry):
        """Barra de búsqueda de la cabecera: atajo hacia Explorar, que ya
        tiene su propia lógica de búsqueda por género/etiqueta."""
        query = entry.get_text().strip()
        if not query:
            return
        self._nav_list.select_row(self._nav_rows['explore'])
        if self._explore_search is not None:
            self._explore_search.set_text(query)
            self._on_explore_search(None)
        entry.set_text('')

    def _on_explore_search(self, _widget):
        query = self._explore_search.get_text().strip()
        while child := self._explore_list.get_first_child():
            self._explore_list.remove(child)
        self._explore_status.set_description('Buscando…')
        self._explore_stack.set_visible_child_name('empty')

        if query:
            radio_browser.fetch_by_tag(
                query,
                callback=lambda data, err: GLib.idle_add(self._on_explore_result, data, err),
            )
        else:
            radio_browser.fetch_stations(
                country='Spain', limit=200,
                callback=lambda data, err: GLib.idle_add(self._on_explore_result, data, err),
            )

    def _on_explore_result(self, stations, error):
        if error:
            self._explore_status.set_description(f'Error al buscar: {error}')
            self._explore_stack.set_visible_child_name('empty')
            return
        stations = stations or []
        if not stations:
            self._explore_status.set_description('Sin resultados para esa búsqueda.')
            self._explore_stack.set_visible_child_name('empty')
            return

        for s in stations:
            url = s.get('url_resolved') or s.get('url', '')
            if not url:
                continue
            station = {
                'name':        s.get('name', ''),
                'url':         url,
                'favicon':     s.get('favicon', ''),
                'genre':       s.get('tags', ''),
                'bitrate':     s.get('bitrate', ''),
                'description': s.get('country', ''),
            }
            already_added = url in self._station_rows
            row = StationRow(station, is_favorite=already_added, on_toggle_fav=self._on_explore_add)
            if already_added:
                row.set_tooltip_text('Ya está en tu lista de emisoras')
            self._explore_list.append(row)

            favicon = station.get('favicon', '')
            if favicon and favicon.startswith('http'):
                radio_browser.fetch_image(
                    favicon, lambda data, err, r=row: r.set_logo_bytes(data) if data else None,
                )

        self._explore_stack.set_visible_child_name('results')

    def _on_explore_add(self, station: dict, btn: Gtk.Button):
        url = station.get('url', '')
        if url in self._station_rows:
            return
        self._add_station_row(station)
        self._fetch_station_logos([station])
        self._refresh_home_cards()
        btn.set_icon_name('m3-star-symbolic')
        btn.set_sensitive(False)
        btn.set_tooltip_text('Ya está en tu lista de emisoras')
        self._toast_overlay.add_toast(Adw.Toast(title=f"Añadida: {station.get('name', '')}"))

    def _on_explore_row_activated(self, _listbox, row):
        if not isinstance(row, StationRow):
            return
        real_row = self._station_rows.get(row.station.get('url', ''))
        if real_row:
            self._nav_list.select_row(self._nav_rows['radio'])
            self._on_station_activated(self._radio_list, real_row)
        else:
            # Preview a not-yet-added station directly.
            self._is_radio        = True
            self._current_station = row.station
            self._current_file    = None
            self._title_label.set_text(row.station.get('name', ''))
            self._artist_label.set_text(row.station.get('description', ''))
            self._album_label.set_text(str(row.station.get('genre', '')))
            self._set_radio_mode(True)
            self._player.play(row.station.get('url', ''))

    def _build_radio_page(self):
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        search_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_bar.set_margin_start(8)
        search_bar.set_margin_end(8)
        search_bar.set_margin_top(8)
        search_bar.set_margin_bottom(4)

        self._radio_search = Gtk.SearchEntry()
        self._radio_search.set_placeholder_text('Buscar emisora…')
        self._radio_search.set_hexpand(True)
        self._radio_search.connect('search-changed', self._filter_stations)
        search_bar.append(self._radio_search)

        add_btn = Gtk.Button()
        add_btn.set_icon_name('m3-add-symbolic')
        add_btn.set_tooltip_text('Añadir emisora manualmente')
        add_btn.add_css_class('flat')
        add_btn.connect('clicked', self._on_add_station)
        search_bar.append(add_btn)

        fav_menu_btn = Gtk.MenuButton()
        fav_menu_btn.set_icon_name('m3-save-symbolic')
        fav_menu_btn.set_tooltip_text('Exportar / Importar favoritos')
        fav_menu_btn.add_css_class('flat')
        fav_popover = Gtk.Popover()
        fav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        fav_box.set_margin_start(8); fav_box.set_margin_end(8)
        fav_box.set_margin_top(8);   fav_box.set_margin_bottom(8)
        exp_btn = Gtk.Button(label='Exportar favoritos')
        exp_btn.add_css_class('flat')
        exp_btn.connect('clicked', lambda b: (fav_popover.popdown(), self._on_export_favorites(b)))
        fav_box.append(exp_btn)
        imp_btn = Gtk.Button(label='Importar favoritos')
        imp_btn.add_css_class('flat')
        imp_btn.connect('clicked', lambda b: (fav_popover.popdown(), self._on_import_favorites(b)))
        fav_box.append(imp_btn)
        fav_popover.set_child(fav_box)
        fav_menu_btn.set_popover(fav_popover)
        search_bar.append(fav_menu_btn)

        page_box.append(search_bar)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self._radio_list = Gtk.ListBox()
        self._radio_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._radio_list.add_css_class('boxed-list')
        self._radio_list.set_margin_start(8)
        self._radio_list.set_margin_end(8)
        self._radio_list.set_margin_bottom(8)
        self._radio_list.connect('row-activated', self._on_station_activated)
        self._radio_list.set_filter_func(self._radio_filter_func)
        self._radio_list.set_sort_func(self._radio_sort_func)

        scroll.set_child(self._radio_list)
        page_box.append(scroll)

        return page_box

    def _build_mp3_page(self):
        mp3_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Folder selector row
        folder_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        folder_bar.set_margin_start(8); folder_bar.set_margin_end(8)
        folder_bar.set_margin_top(8);   folder_bar.set_margin_bottom(4)

        folder_icon = Gtk.Image.new_from_icon_name('m3-library-music-symbolic')
        folder_bar.append(folder_icon)

        self._folder_label = Gtk.Label(label=self._music_folder)
        self._folder_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._folder_label.set_hexpand(True)
        self._folder_label.set_xalign(0)
        self._folder_label.add_css_class('caption')
        folder_bar.append(self._folder_label)

        choose_btn = Gtk.Button()
        choose_btn.set_icon_name('m3-folder-open-symbolic')
        choose_btn.set_tooltip_text('Elegir carpeta de música')
        choose_btn.add_css_class('flat')
        choose_btn.connect('clicked', self._on_choose_folder)
        folder_bar.append(choose_btn)

        self._scan_btn = Gtk.Button()
        self._scan_btn.set_icon_name('m3-refresh-symbolic')
        self._scan_btn.set_tooltip_text('Escanear carpeta de música y subcarpetas')
        self._scan_btn.add_css_class('flat')
        self._scan_btn.connect('clicked', self._on_scan_folder)
        folder_bar.append(self._scan_btn)

        mp3_box.append(folder_bar)
        mp3_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Search + clear
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.set_margin_start(8); bar.set_margin_end(8)
        bar.set_margin_top(4);   bar.set_margin_bottom(4)

        self._mp3_search = Gtk.SearchEntry()
        self._mp3_search.set_placeholder_text('Buscar pista…')
        self._mp3_search.set_hexpand(True)
        self._mp3_search.connect('search-changed', self._filter_mp3)
        bar.append(self._mp3_search)

        clear_btn = Gtk.Button(label='Limpiar')
        clear_btn.add_css_class('flat')
        clear_btn.connect('clicked', self._clear_mp3_list)
        bar.append(clear_btn)

        self._mp3_sort_btn = Gtk.Button()
        self._mp3_sort_btn.set_icon_name('m3-sort-alpha-symbolic')
        self._mp3_sort_btn.set_tooltip_text('Ordenar: Nombre de archivo')
        self._mp3_sort_btn.add_css_class('flat')
        self._mp3_sort_btn.connect('clicked', self._on_mp3_sort_toggle)
        bar.append(self._mp3_sort_btn)

        mp3_box.append(bar)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        self._mp3_list = Gtk.ListBox()
        self._mp3_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._mp3_list.add_css_class('boxed-list')
        self._mp3_list.set_margin_start(8)
        self._mp3_list.set_margin_end(8)
        self._mp3_list.set_margin_bottom(8)
        self._mp3_list.connect('row-activated', self._on_mp3_activated)
        self._mp3_list.set_filter_func(self._mp3_filter_func)
        self._mp3_list.set_sort_func(self._mp3_sort_func)
        scroll.set_child(self._mp3_list)
        mp3_box.append(scroll)

        return mp3_box

    def _build_now_playing(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(24); box.set_margin_end(24)
        box.set_margin_top(12);   box.set_margin_bottom(24)
        box.set_hexpand(True)

        art_frame = Gtk.Frame()
        art_frame.set_halign(Gtk.Align.CENTER)
        art_frame.add_css_class('card')

        self._cover_image = Gtk.Image()
        self._cover_image.set_pixel_size(160)
        self._cover_image.set_from_icon_name('m3-music-note-symbolic')
        self._cover_image.set_size_request(160, 160)
        self._cover_image.set_margin_start(8)
        self._cover_image.set_margin_end(8)
        self._cover_image.set_margin_top(8)
        self._cover_image.set_margin_bottom(8)
        art_frame.set_child(self._cover_image)
        box.append(art_frame)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info_box.set_halign(Gtk.Align.CENTER)

        self._title_label = Gtk.Label(label='Sin reproducir')
        self._title_label.add_css_class('title-2')
        self._title_label.set_wrap(True)
        self._title_label.set_justify(Gtk.Justification.CENTER)
        self._title_label.set_max_width_chars(28)
        info_box.append(self._title_label)

        self._artist_label = Gtk.Label(label='')
        self._artist_label.add_css_class('body')
        self._artist_label.add_css_class('dim-label')
        self._artist_label.set_wrap(True)
        self._artist_label.set_justify(Gtk.Justification.CENTER)
        self._artist_label.set_max_width_chars(28)
        info_box.append(self._artist_label)

        self._album_label = Gtk.Label(label='')
        self._album_label.add_css_class('caption')
        self._album_label.add_css_class('dim-label')
        self._album_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._album_label.set_max_width_chars(28)
        info_box.append(self._album_label)

        box.append(info_box)

        self._meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._meta_box.set_homogeneous(True)
        self._meta_box.set_hexpand(True)
        box.append(self._meta_box)

        self._spectrum_viz = SpectrumVisualizer()
        box.append(self._spectrum_viz)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(box)
        scroll.set_hexpand(True)
        return scroll

    def _build_controls(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class('toolbar')
        bar.set_margin_start(16); bar.set_margin_end(16)
        bar.set_margin_top(8);    bar.set_margin_bottom(8)

        self._prev_btn = Gtk.Button()
        self._prev_btn.set_icon_name('m3-skip-previous-symbolic')
        self._prev_btn.add_css_class('circular')
        self._prev_btn.connect('clicked', self._on_prev_track)
        bar.append(self._prev_btn)

        self._play_btn = Gtk.Button()
        self._play_btn.set_icon_name('m3-play-arrow-symbolic')
        self._play_btn.add_css_class('circular')
        self._play_btn.add_css_class('suggested-action')
        self._play_btn.add_css_class('m3-fab')
        self._play_btn.set_size_request(56, 56)
        self._play_btn.connect('clicked', self._on_play_pause)
        bar.append(self._play_btn)

        stop_btn = Gtk.Button()
        stop_btn.set_icon_name('m3-stop-symbolic')
        stop_btn.add_css_class('circular')
        stop_btn.connect('clicked', self._on_stop)
        bar.append(stop_btn)

        self._next_btn = Gtk.Button()
        self._next_btn.set_icon_name('m3-skip-next-symbolic')
        self._next_btn.add_css_class('circular')
        self._next_btn.connect('clicked', self._on_next_track)
        bar.append(self._next_btn)

        self._mode_btn = Gtk.Button()
        _mode_icon, _mode_label = next(
            ((icon, label) for name, icon, label in self._PLAY_MODES if name == self._play_mode),
            ('m3-playlist-play-symbolic', 'Modo: Secuencial'))
        self._mode_btn.set_icon_name(_mode_icon)
        self._mode_btn.set_tooltip_text(_mode_label)
        self._mode_btn.add_css_class('flat')
        self._mode_btn.set_visible(False)
        self._mode_btn.connect('clicked', self._on_toggle_play_mode)
        bar.append(self._mode_btn)

        self._progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._progress_box.set_hexpand(True)

        self._pos_label = Gtk.Label(label='0:00')
        self._pos_label.add_css_class('caption')
        self._pos_label.add_css_class('numeric')
        self._progress_box.append(self._pos_label)

        self._seek_bar = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        self._seek_bar.set_hexpand(True)
        self._seek_bar.set_draw_value(False)
        self._seek_bar.add_css_class('m3-wavy')
        self._seek_bar.connect('change-value', self._on_seek)
        self._progress_box.append(self._seek_bar)

        self._dur_label = Gtk.Label(label='0:00')
        self._dur_label.add_css_class('caption')
        self._dur_label.add_css_class('numeric')
        self._progress_box.append(self._dur_label)

        bar.append(self._progress_box)

        self._live_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._live_box.set_hexpand(True)
        self._live_box.set_halign(Gtk.Align.CENTER)
        live_dot = Gtk.Label(label='●')
        live_dot.add_css_class('error')
        self._live_box.append(live_dot)
        live_lbl = Gtk.Label(label='EN DIRECTO')
        live_lbl.add_css_class('caption')
        self._live_box.append(live_lbl)
        bar.append(self._live_box)
        self._live_box.set_visible(False)

        self._vol_btn = Gtk.Button()
        self._vol_btn.set_icon_name('m3-volume-up-symbolic')
        self._vol_btn.add_css_class('flat')
        self._vol_btn.add_css_class('circular')
        self._vol_btn.set_tooltip_text('Silenciar')
        self._vol_btn.connect('clicked', self._on_vol_btn_clicked)
        bar.append(self._vol_btn)

        self._vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        self._vol_scale.set_value(self._player.get_volume())
        self._vol_scale.set_size_request(100, -1)
        self._vol_scale.set_draw_value(False)
        self._vol_scale.add_css_class('m3-wavy')
        self._vol_scale.connect('value-changed', self._on_volume_changed)
        bar.append(self._vol_scale)

        self._sleep_btn = Gtk.MenuButton()
        self._sleep_btn.set_icon_name('m3-alarm-symbolic')
        self._sleep_btn.set_tooltip_text('Sleep timer')
        self._sleep_btn.add_css_class('flat')
        sleep_pop = Gtk.Popover()
        sleep_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sleep_box.set_margin_start(8); sleep_box.set_margin_end(8)
        sleep_box.set_margin_top(8);   sleep_box.set_margin_bottom(8)
        for mins, lbl in [(0, 'Desactivar'), (15, '15 minutos'),
                          (30, '30 minutos'), (60, '1 hora'), (90, '90 minutos')]:
            sb = Gtk.Button(label=lbl)
            sb.add_css_class('flat')
            sb.connect('clicked', lambda b, m=mins: (sleep_pop.popdown(),
                                                      self._on_sleep_timer_set(m)))
            sleep_box.append(sb)
        sleep_pop.set_child(sleep_box)
        self._sleep_btn.set_popover(sleep_pop)
        bar.append(self._sleep_btn)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrapper.append(separator)
        wrapper.append(bar)
        return wrapper

    # ── Sort / header for radio list ───────────────────────────────────────────

    def _radio_sort_func(self, row1, row2):
        # Favoriting a station no longer pulls it out of its genre group —
        # it always sorts by its real genre; the dedicated Favoritos page
        # (nav rail) is where "just the favorites" lives now. This avoids
        # leaving a genre header with zero stations under it once its only
        # member gets favorited.
        def _key(row):
            if isinstance(row, GenreHeaderRow):
                return (row.genre.lower(), 0, '')
            if isinstance(row, StationRow):
                genre = (row.station.get('genre', '') or 'Sin género').lower()
                return (genre, 1, row.station.get('name', '').lower())
            return ('~', 0, '')

        k1, k2 = _key(row1), _key(row2)
        return -1 if k1 < k2 else (1 if k1 > k2 else 0)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load_builtin_stations(self):
        stations = _load_stations_file()
        for s in stations:
            self._add_station_row(s)
        if stations:
            self._fetch_station_logos(stations)
        self._refresh_home_cards()
        self._refresh_favorites_page()

    def _add_station_row(self, station: dict):
        genre = station.get('genre', '') or 'Sin género'
        if genre not in self._genre_headers:
            header = GenreHeaderRow(genre, self._toggle_genre_collapse)
            self._genre_headers[genre] = header
            self._radio_list.append(header)

        url = station.get('url', '')
        is_fav = url in self._favorites
        row = StationRow(station, is_favorite=is_fav, on_toggle_fav=self._toggle_favorite)
        self._station_rows[url] = row
        self._radio_list.append(row)

    def _fetch_station_logos(self, stations: list):
        for s in stations:
            url     = s.get('url', '')
            favicon = s.get('favicon') or s.get('favicon_url', '')
            row     = self._station_rows.get(url)
            if row and favicon and favicon.startswith('http'):
                radio_browser.fetch_image(
                    favicon,
                    lambda data, err, r=row: r.set_logo_bytes(data) if data else None,
                )

    # ── Favorites & collapse ───────────────────────────────────────────────────

    def _toggle_favorite(self, station: dict, btn: Gtk.Button):
        url = station.get('url', '')
        if url in self._favorites:
            self._favorites.discard(url)
            btn.set_icon_name('m3-star-outline-symbolic')
        else:
            self._favorites.add(url)
            btn.set_icon_name('m3-star-symbolic')
        self._config['favorites'] = list(self._favorites)
        threading.Thread(target=lambda: _save_config(self._config), daemon=True).start()
        self._radio_list.invalidate_sort()
        self._radio_list.invalidate_filter()
        self._on_favorites_changed()

    def _toggle_genre_collapse(self, genre: str):
        if genre in self._collapsed_genres:
            self._collapsed_genres.discard(genre)
            collapsed = False
        else:
            self._collapsed_genres.add(genre)
            collapsed = True
        header = self._genre_headers.get(genre)
        if header:
            header.set_collapsed(collapsed)
        self._radio_list.invalidate_filter()

    # ── Persistent MP3 loading ─────────────────────────────────────────────────

    def _load_persistent_mp3s(self):
        cached = _load_mp3_cache()
        if not cached:
            return
        valid = [t for t in cached if t.get('path') and Path(t['path']).is_file()]
        if len(valid) != len(cached):
            threading.Thread(target=lambda: _save_mp3_cache(valid), daemon=True).start()
        for track in valid:
            path = track['path']
            if path not in self._known_paths:
                thumb_b64  = track.get('thumbnail', '')
                cover_data = base64.b64decode(thumb_b64) if thumb_b64 else None
                tags = {
                    'title':      track.get('title', '') or Path(path).stem,
                    'artist':     track.get('artist', ''),
                    'album':      track.get('album', ''),
                    'track':      track.get('track', ''),
                    'cover_data': cover_data,
                }
                self._add_mp3_row(path, tags)

    # ── Music folder ───────────────────────────────────────────────────────────

    def _on_choose_folder(self, _btn):
        dialog = Gtk.FileDialog()
        dialog.set_title('Elegir carpeta de música')
        initial = Path(self._music_folder)
        if not initial.is_dir():
            initial = Path.home()
        dialog.set_initial_folder(Gio.File.new_for_path(str(initial)))
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder:
            path = folder.get_path()
            if path:
                self._music_folder = path
                self._folder_label.set_text(path)
                self._config['music_folder'] = path
                threading.Thread(
                    target=lambda: _save_config(self._config), daemon=True
                ).start()

    def _on_scan_folder(self, _btn):
        folder = Path(self._music_folder)
        if not folder.is_dir():
            toast = Adw.Toast(title=f'Carpeta no encontrada: {self._music_folder}')
            self._toast_overlay.add_toast(toast)
            return

        self._scan_btn.set_sensitive(False)
        self._scan_btn.set_icon_name('m3-autorenew-symbolic')

        def _scan():
            exts     = {'.mp3', '.flac', '.ogg', '.m4a', '.aac', '.wav', '.opus'}
            snapshot = set(self._known_paths)
            to_add   = [
                str(p) for p in sorted(folder.rglob('*'))
                if p.is_file() and p.suffix.lower() in exts and str(p) not in snapshot
            ]
            for path in to_add:
                tags = meta_mod.read_tags(path)
                GLib.idle_add(self._add_mp3_row, path, tags)
            GLib.idle_add(self._on_scan_done, len(to_add))

        threading.Thread(target=_scan, daemon=True).start()

    def _on_scan_done(self, added: int):
        self._scan_btn.set_sensitive(True)
        self._scan_btn.set_icon_name('m3-refresh-symbolic')
        self._save_mp3_cache_now()
        msg = (f'Se encontraron {added} canciones nuevas (incluyendo subcarpetas)'
               if added else 'No hay canciones nuevas')
        self._toast_overlay.add_toast(Adw.Toast(title=msg))

    # ── Play mode ──────────────────────────────────────────────────────────────

    _PLAY_MODES = [
        ('sequential', 'm3-playlist-play-symbolic', 'Modo: Secuencial'),
        ('repeat',     'm3-repeat-symbolic',      'Modo: Repetir lista'),
        ('shuffle',    'm3-shuffle-symbolic',     'Modo: Aleatorio'),
    ]

    def _on_toggle_play_mode(self, _btn):
        names = [m[0] for m in self._PLAY_MODES]
        idx = (names.index(self._play_mode) + 1) % len(self._PLAY_MODES)
        _mode, icon, label = self._PLAY_MODES[idx]
        self._play_mode = _mode
        self._mode_btn.set_icon_name(icon)
        self._mode_btn.set_tooltip_text(label)
        self._config['play_mode'] = _mode
        threading.Thread(target=lambda: _save_config(self._config), daemon=True).start()
        toast = Adw.Toast(title=label)
        toast.set_timeout(1)
        self._toast_overlay.add_toast(toast)

    # ── MP3 cache persistence ──────────────────────────────────────────────────

    def _on_close_request(self, _win):
        if self._sleep_timer_id:
            GLib.source_remove(self._sleep_timer_id)
        self._save_mp3_cache_sync()
        self._player.dispose()
        return False  # allow the window to close

    def _collect_mp3_rows(self) -> list:
        """Return list of dicts with row data; must be called from main thread."""
        rows = []
        i = 0
        while True:
            row = self._mp3_list.get_row_at_index(i)
            if row is None:
                break
            if isinstance(row, Mp3Row):
                rows.append({
                    'path':       row.path,
                    'title':      row.tags.get('title', ''),
                    'artist':     row.tags.get('artist', ''),
                    'album':      row.tags.get('album', ''),
                    'track':      row.tags.get('track', ''),
                    'cover_data': row.tags.get('cover_data'),
                })
            i += 1
        return rows

    @staticmethod
    def _encode_thumbnail(cover_data: bytes | None) -> str:
        """Scale cover to 40×40 PNG and base64-encode it for the cache."""
        if not cover_data:
            return ''
        pb = _pixbuf_from_bytes(cover_data, 40)
        if not pb:
            return ''
        try:
            ok, buf = pb.save_to_bufferv('png', [], [])
            return base64.b64encode(buf).decode('ascii') if ok else ''
        except Exception:
            return ''

    def _save_mp3_cache_now(self):
        if self._cache_save_timer:
            GLib.source_remove(self._cache_save_timer)
            self._cache_save_timer = None
        rows = self._collect_mp3_rows()

        def _work():
            tracks = []
            for d in rows:
                cover = d.pop('cover_data')
                d['thumbnail'] = self._encode_thumbnail(cover)
                tracks.append(d)
            _save_mp3_cache(tracks)

        threading.Thread(target=_work, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _save_mp3_cache_sync(self):
        """Synchronous save – called from close-request handler."""
        if self._cache_save_timer:
            GLib.source_remove(self._cache_save_timer)
            self._cache_save_timer = None
        rows = self._collect_mp3_rows()
        tracks = []
        for d in rows:
            cover = d.pop('cover_data')
            d['thumbnail'] = self._encode_thumbnail(cover)
            tracks.append(d)
        _save_mp3_cache(tracks)

    # ── Filter functions ───────────────────────────────────────────────────────

    def _radio_filter_func(self, row):
        if isinstance(row, GenreHeaderRow):
            # Hide genre headers while searching (flat results look cleaner)
            return not bool(self._radio_search.get_text().strip())
        if not isinstance(row, StationRow):
            return True

        query = self._radio_search.get_text().lower().strip()
        if query:
            name  = row.station.get('name', '').lower()
            genre = str(row.station.get('genre', '')).lower()
            tags  = str(row.station.get('tags', '')).lower()
            desc  = str(row.station.get('description', '')).lower()
            return query in name or query in genre or query in tags or query in desc

        section = row.station.get('genre', '') or 'Sin género'
        return section not in self._collapsed_genres

    def _mp3_filter_func(self, row):
        query = self._mp3_search.get_text().lower().strip()
        if not query:
            return True
        title  = row.tags.get('title', '').lower()
        artist = row.tags.get('artist', '').lower()
        album  = row.tags.get('album', '').lower()
        name   = Path(row.path).name.lower()
        return query in title or query in artist or query in album or query in name

    def _filter_stations(self, _widget):
        self._radio_list.invalidate_filter()

    def _filter_mp3(self, _widget):
        self._mp3_list.invalidate_filter()

    # ── Playback event handlers ────────────────────────────────────────────────

    def _on_station_activated(self, _listbox, row):
        if not isinstance(row, StationRow):
            return
        self._current_track_index = self._row_index(self._radio_list, row)
        self._is_radio        = True
        self._current_station = row.station
        self._current_file    = None
        self._title_label.set_text(row.station.get('name', ''))
        self._artist_label.set_text(row.station.get('description', ''))
        self._album_label.set_text(row.station.get('genre', ''))
        self._set_radio_mode(True)
        self._player.play(row.station.get('url', ''))
        self._update_meta_chips({
            'Bitrate': f"{row.station.get('bitrate', '')}kbps",
        })

        if row.logo_bytes:
            self._set_cover_from_bytes(row.logo_bytes)
        else:
            self._cover_image.set_from_icon_name('m3-radio-symbolic')
            self._cover_image.set_pixel_size(160)
            self._current_cover_data = None
            self._update_cover_display_mode()
            favicon = row.station.get('favicon', '')
            if favicon and favicon.startswith('http'):
                radio_browser.fetch_image(
                    favicon,
                    lambda data, err, r=row: self._on_station_logo(data, r),
                )

    def _on_mp3_activated(self, _listbox, row):
        if not isinstance(row, Mp3Row):
            return
        self._current_track_index = self._row_index(self._mp3_list, row)
        self._is_radio        = False
        self._current_file    = row.path
        self._current_station = None
        self._title_label.set_text(row.tags.get('title') or Path(row.path).stem)
        self._artist_label.set_text(row.tags.get('artist', ''))
        self._album_label.set_text(row.tags.get('album', ''))
        self._set_radio_mode(False)

        cover = row.tags.get('cover_data')
        self._current_cover_data = cover
        pb = _pixbuf_from_bytes(cover, 160) if cover else None
        if pb:
            self._cover_image.set_from_pixbuf(pb)
        else:
            self._cover_image.set_from_icon_name('m3-music-note-symbolic')
            self._cover_image.set_pixel_size(160)
        self._update_cover_display_mode()

        uri = 'file://' + urllib.parse.quote(row.path)
        self._player.play(uri)
        chips = {}
        if row.tags.get('album'): chips['Álbum'] = row.tags['album']
        if row.tags.get('track'): chips['Pista'] = row.tags['track']
        self._update_meta_chips(chips)

    def _on_play_pause(self, _btn):
        self._player.toggle_pause()

    def _on_prev_track(self, _btn):
        self._navigate(-1)

    def _on_next_track(self, _btn):
        self._navigate(+1)

    def _navigate(self, delta: int):
        listbox = self._radio_list if self._is_radio else self._mp3_list
        target  = self._next_visible_row(listbox, self._current_track_index, delta)
        if target is not None:
            listbox.select_row(target)
            listbox.emit('row-activated', target)

    # ── Playlist helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _row_index(listbox: Gtk.ListBox, target_row: Gtk.ListBoxRow) -> int:
        i = 0
        while True:
            row = listbox.get_row_at_index(i)
            if row is None:
                return -1
            if row is target_row:
                return i
            i += 1

    @staticmethod
    def _next_visible_row(listbox: Gtk.ListBox, current: int,
                          delta: int) -> Gtk.ListBoxRow | None:
        visible = []
        i = 0
        while True:
            row = listbox.get_row_at_index(i)
            if row is None:
                break
            if row.get_visible() and row.get_mapped():
                visible.append((i, row))
            i += 1
        if not visible:
            return None
        pos = next((p for p, (idx, _) in enumerate(visible) if idx == current), -1)
        new_pos = pos + delta
        if new_pos < 0 or new_pos >= len(visible):
            return None
        return visible[new_pos][1]

    def _on_stop(self, _btn):
        self._player.stop()
        self._stop_position_timer()
        self._seek_bar.set_value(0)
        self._pos_label.set_text('0:00')
        self._spectrum_viz.reset()
        self._play_btn.set_icon_name('m3-play-arrow-symbolic')
        self._current_cover_data = None
        self._update_cover_display_mode()

    def _on_volume_changed(self, scale):
        value = scale.get_value()
        self._player.set_volume(value)

        # Mantiene el icono de mute en sync también cuando el volumen se
        # arrastra a mano (no solo al pulsar el botón/atajo M).
        if value > 0.0001:
            self._last_nonzero_vol = value
            if self._muted:
                self._muted = False
                self._update_vol_icon()
        elif not self._muted:
            self._muted = True
            self._update_vol_icon()

        if self._volume_save_timer:
            GLib.source_remove(self._volume_save_timer)
        self._volume_save_timer = GLib.timeout_add(500, self._save_volume_now, value)

    def _save_volume_now(self, value):
        self._volume_save_timer = None
        self._config['volume'] = value
        threading.Thread(target=lambda: _save_config(self._config), daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _on_seek(self, _scale, _scroll, value):
        _pos, dur = self._player.get_position()
        if dur > 0:
            self._player.seek(int(value * dur))
        return False

    # ── Player signal handlers ─────────────────────────────────────────────────

    def _on_metadata(self, _player, title, artist, album):
        if title:
            self._title_label.set_text(title)
            if self._is_radio and title != self._last_notified_title:
                self._last_notified_title = title
                if self._desktop_notifications:
                    self._notify_now_playing(title, artist or '')
        if artist: self._artist_label.set_text(artist)
        if album:  self._album_label.set_text(album)

    def _on_cover_data(self, _player, data):
        self._current_cover_data = data
        pb = _pixbuf_from_bytes(data, 160)
        if pb:
            self._cover_image.set_from_pixbuf(pb)
        self._update_cover_display_mode()

    def _on_spectrum(self, _player, magnitudes):
        self._spectrum_viz.push(magnitudes)

    def _on_eos(self, _player):
        if self._current_episode is not None:
            self._mark_current_episode_listened()
            return
        if self._is_radio:
            return
        if self._play_mode == 'shuffle':
            self._navigate_random()
        elif self._play_mode == 'repeat':
            target = self._next_visible_row(self._mp3_list, self._current_track_index, +1)
            if target is None:
                target = self._first_visible_row(self._mp3_list)
            if target is not None:
                self._mp3_list.select_row(target)
                self._mp3_list.emit('row-activated', target)
        else:
            self._navigate(+1)

    def _navigate_random(self):
        import random
        visible = []
        i = 0
        while True:
            row = self._mp3_list.get_row_at_index(i)
            if row is None:
                break
            if row.get_visible() and row.get_mapped():
                visible.append((i, row))
            i += 1
        if not visible:
            return
        candidates = [(idx, row) for idx, row in visible if idx != self._current_track_index]
        if not candidates:
            candidates = visible
        _idx, row = random.choice(candidates)
        self._mp3_list.select_row(row)
        self._mp3_list.emit('row-activated', row)

    @staticmethod
    def _first_visible_row(listbox: Gtk.ListBox) -> Gtk.ListBoxRow | None:
        i = 0
        while True:
            row = listbox.get_row_at_index(i)
            if row is None:
                return None
            if row.get_visible() and row.get_mapped():
                return row
            i += 1

    def _on_state_changed(self, _player, playing):
        icon = 'm3-pause-symbolic' if playing else 'm3-play-arrow-symbolic'
        self._play_btn.set_icon_name(icon)
        if playing:
            if not self._is_radio:
                self._start_position_timer()
        else:
            self._stop_position_timer()

    def _on_player_error(self, _player, error_msg):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading='Error de reproducción',
            body=error_msg,
        )
        dialog.add_response('ok', 'Aceptar')
        dialog.present()

    # ── Position timer ─────────────────────────────────────────────────────────

    def _start_position_timer(self):
        self._stop_position_timer()
        self._position_timer = GLib.timeout_add(500, self._update_position)

    def _stop_position_timer(self):
        if self._position_timer:
            GLib.source_remove(self._position_timer)
            self._position_timer = None

    def _update_position(self):
        pos, dur = self._player.get_position()
        if dur > 0:
            self._seek_bar.set_value(pos / dur)
            self._pos_label.set_text(_fmt_time(pos))
            self._dur_label.set_text(_fmt_time(dur))
            if self._current_episode is not None:
                self._maybe_save_episode_progress(pos // Gst.SECOND, dur // Gst.SECOND)
        return True

    def _maybe_save_episode_progress(self, pos_sec: int, dur_sec: int):
        """Persiste la posición del episodio actual, con throttling (no en
        cada tick de 500ms del timer, solo cada ~5s de progreso real)."""
        ep = self._current_episode
        if ep is None:
            return
        last = ep.get('_last_saved_pos', -999)
        if abs(pos_sec - last) < 5:
            return
        ep['_last_saved_pos'] = pos_sec
        entry = self._podcasts_data.setdefault('episodes', {}).setdefault(ep['guid'], {})
        entry.update({
            'feed_url':    (self._current_podcast_show or {}).get('feed_url', ''),
            'title':       ep.get('title', ''),
            'pub_date':    ep.get('pub_date', ''),
            'audio_url':   ep.get('audio_url', ''),
            'duration_sec': dur_sec or ep.get('duration_sec', 0),
            'position_sec': pos_sec,
        })
        entry.setdefault('listened', False)
        entry.setdefault('downloaded_path', None)
        threading.Thread(target=lambda: _save_podcasts_data(self._podcasts_data), daemon=True).start()

    def _mark_current_episode_listened(self):
        ep = self._current_episode
        if ep is None:
            return
        entry = self._podcasts_data.setdefault('episodes', {}).setdefault(ep['guid'], {})
        entry.update({
            'feed_url':    (self._current_podcast_show or {}).get('feed_url', ''),
            'title':       ep.get('title', ''),
            'pub_date':    ep.get('pub_date', ''),
            'audio_url':   ep.get('audio_url', ''),
            'listened':    True,
            'position_sec': 0,
        })
        entry.setdefault('downloaded_path', None)
        entry.setdefault('duration_sec', ep.get('duration_sec', 0))
        threading.Thread(target=lambda: _save_podcasts_data(self._podcasts_data), daemon=True).start()
        row = ep.get('row')
        if row is not None:
            row.set_listened(True)

    # ── UI helpers ─────────────────────────────────────────────────────────────

    def _set_radio_mode(self, is_radio: bool):
        self._live_box.set_visible(is_radio)
        self._progress_box.set_visible(not is_radio)

    def _update_meta_chips(self, chips: dict):
        while child := self._meta_box.get_first_child():
            self._meta_box.remove(child)
        valid = [(k, v) for k, v in chips.items() if v]
        for i, (key, val) in enumerate(valid):
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            chip.set_margin_start(4)
            chip.set_margin_end(4)
            if len(valid) == 1:
                chip.set_halign(Gtk.Align.CENTER)
            else:
                chip.set_halign(Gtk.Align.END if i == 0 else Gtk.Align.START)
            k = Gtk.Label(label=key + ':')
            k.add_css_class('caption')
            k.add_css_class('dim-label')
            chip.append(k)
            v = Gtk.Label(label=str(val))
            v.add_css_class('caption')
            v.set_ellipsize(Pango.EllipsizeMode.END)
            v.set_max_width_chars(18)
            chip.append(v)
            self._meta_box.append(chip)

    def _set_cover_from_bytes(self, data: bytes):
        pb = _pixbuf_from_bytes(data, 160)
        if pb:
            self._cover_image.set_from_pixbuf(pb)
        else:
            self._cover_image.set_from_icon_name('m3-radio-symbolic')
            self._cover_image.set_pixel_size(160)
        self._current_cover_data = data
        self._update_cover_display_mode()

    def _on_station_logo(self, data: bytes, row: 'StationRow'):
        if data:
            row.logo_bytes = data
            row.set_logo_bytes(data)
            if self._current_station is row.station:
                GLib.idle_add(self._set_cover_from_bytes, data)

    # ── Open MP3 files dialog ──────────────────────────────────────────────────

    def _on_open_files(self, _btn):
        dialog = Gtk.FileDialog()
        dialog.set_title('Abrir archivos de audio')

        filter_audio = Gtk.FileFilter()
        filter_audio.set_name('Audio (MP3, FLAC, OGG, AAC)')
        for ext in ('*.mp3', '*.flac', '*.ogg', '*.m4a', '*.aac', '*.wav', '*.opus'):
            filter_audio.add_pattern(ext)

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_audio)
        dialog.set_filters(filters)
        dialog.open_multiple(self, None, self._on_files_selected)

    def _on_files_selected(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except Exception:
            return
        if not files:
            return
        self._nav_list.select_row(self._nav_rows['local'])

        def _load_in_background():
            for i in range(files.get_n_items()):
                gfile = files.get_item(i)
                path  = gfile.get_path()
                if path:
                    tags = meta_mod.read_tags(path)
                    GLib.idle_add(self._add_mp3_row, path, tags)
            GLib.idle_add(self._save_mp3_cache_now)

        threading.Thread(target=_load_in_background, daemon=True).start()

    def _add_mp3_row(self, path: str, tags: dict):
        if path in self._known_paths:
            return
        self._known_paths.add(path)
        row = Mp3Row(path, tags, on_edit=self._on_edit_mp3_tags)
        self._mp3_list.append(row)

    def _clear_mp3_list(self, _btn):
        while row := self._mp3_list.get_first_child():
            self._mp3_list.remove(row)
        self._known_paths.clear()
        threading.Thread(target=lambda: _save_mp3_cache([]), daemon=True).start()

    # ── Editar etiquetas MP3 ────────────────────────────────────────────────────

    def _on_edit_mp3_tags(self, row):
        tags = row.tags
        dialog = Adw.MessageDialog(transient_for=self, heading='Editar etiquetas')
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('save', 'Guardar')
        dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('save')

        pending = {'cover_data': tags.get('cover_data'), 'cover_mime': 'image/jpeg'}

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        form.set_margin_top(8)

        preview_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        preview_row.set_halign(Gtk.Align.CENTER)

        preview_img = Gtk.Image()
        preview_img.set_pixel_size(96)
        preview_img.set_size_request(96, 96)
        preview_row.append(preview_img)

        cover_btns = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        cover_btns.set_valign(Gtk.Align.CENTER)
        choose_btn = Gtk.Button(label='Elegir imagen…')
        cover_btns.append(choose_btn)
        auto_btn = Gtk.Button(label='Buscar carátula e info…')
        cover_btns.append(auto_btn)
        status_lbl = Gtk.Label(label='')
        status_lbl.add_css_class('caption')
        status_lbl.add_css_class('dim-label')
        cover_btns.append(status_lbl)
        preview_row.append(cover_btns)
        form.append(preview_row)

        def _labeled_entry(hint: str, value: str) -> Gtk.Entry:
            entry = Gtk.Entry()
            entry.set_text(value)
            entry.set_hexpand(True)

            hint_lbl = Gtk.Label(label=hint)
            hint_lbl.add_css_class('caption')
            hint_lbl.add_css_class('dim-label')
            hint_lbl.set_width_chars(10)
            hint_lbl.set_xalign(0)

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.append(entry)
            row_box.append(hint_lbl)
            form.append(row_box)
            return entry

        title_e  = _labeled_entry('Título',      tags.get('title') or Path(row.path).stem)
        artist_e = _labeled_entry('Artista',     tags.get('artist', ''))
        album_e  = _labeled_entry('Álbum',       tags.get('album', ''))
        track_e  = _labeled_entry('Nº de pista', tags.get('track', ''))

        dialog.set_extra_child(form)

        def _apply_cover(data, mime):
            pending['cover_data'] = data
            pending['cover_mime'] = mime
            pb = _pixbuf_from_bytes(data, 96) if data else None
            if pb:
                preview_img.set_from_pixbuf(pb)
            else:
                preview_img.set_from_icon_name('m3-music-note-symbolic')

        _apply_cover(pending['cover_data'], pending['cover_mime'])

        def _on_choose_cover(_btn):
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title('Elegir imagen de carátula')
            img_filter = Gtk.FileFilter()
            img_filter.set_name('Imágenes')
            for ext in ('*.jpg', '*.jpeg', '*.png', '*.webp'):
                img_filter.add_pattern(ext)
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(img_filter)
            file_dialog.set_filters(filters)

            def _on_image_chosen(fd, result):
                try:
                    gfile = fd.open_finish(result)
                except Exception:
                    return
                if not gfile:
                    return
                path = gfile.get_path()
                try:
                    _ok, data, _etag = gfile.load_contents()
                except Exception:
                    return
                ctype, _uncertain = Gio.content_type_guess(path, data)
                mime = Gio.content_type_get_mime_type(ctype) if ctype else 'image/jpeg'
                _apply_cover(data, mime or 'image/jpeg')

            file_dialog.open(self, None, _on_image_chosen)

        choose_btn.connect('clicked', _on_choose_cover)

        def _on_auto_search(_btn):
            auto_btn.set_sensitive(False)
            status_lbl.set_text('Buscando…')

            def _cb(result, error):
                def _apply():
                    auto_btn.set_sensitive(True)
                    if error:
                        status_lbl.set_text('Error en la búsqueda')
                    elif not result:
                        status_lbl.set_text('Sin resultados')
                    else:
                        if result.get('title') and not title_e.get_text().strip():
                            title_e.set_text(result['title'])
                        if result.get('artist') and not artist_e.get_text().strip():
                            artist_e.set_text(result['artist'])
                        if result.get('album') and not album_e.get_text().strip():
                            album_e.set_text(result['album'])
                        if result.get('cover_data'):
                            _apply_cover(result['cover_data'], result.get('cover_mime', 'image/jpeg'))
                        status_lbl.set_text('Datos encontrados')
                    return GLib.SOURCE_REMOVE
                GLib.idle_add(_apply)

            cover_lookup.search(artist_e.get_text().strip(), title_e.get_text().strip(),
                                 album_e.get_text().strip(), _cb)

        auto_btn.connect('clicked', _on_auto_search)

        dialog.connect('response', lambda d, r: self._on_edit_mp3_tags_response(
            d, r, row, pending, title_e, artist_e, album_e, track_e))
        dialog.present()

    def _on_edit_mp3_tags_response(self, _dialog, response, row, pending,
                                    title_e, artist_e, album_e, track_e):
        if response != 'save':
            return

        title  = title_e.get_text().strip()
        artist = artist_e.get_text().strip()
        album  = album_e.get_text().strip()
        track  = track_e.get_text().strip()
        cover_data = pending.get('cover_data')
        cover_mime = pending.get('cover_mime', 'image/jpeg')

        # Si el fichero está cargado en el reproductor hay que liberarlo antes de
        # reescribirlo: GStreamer mantiene el fichero abierto y una escritura
        # concurrente (sobre todo si cambia de tamaño, p.ej. al incrustar una
        # carátula nueva) corrompe el stream en curso con un
        # "gst-stream-error-quark: Internal data stream error".
        editing_current_track = not self._is_radio and self._current_file == row.path
        was_playing = editing_current_track and self._player.is_playing
        if editing_current_track:
            self._player.stop()

        def _work():
            try:
                meta_mod.write_tags(row.path, title, artist, album, track,
                                     cover_data, cover_mime)
                error = None
            except meta_mod.TagWriteError as exc:
                error = str(exc)
            GLib.idle_add(self._on_tags_saved, row, error, title, artist, album, track,
                          cover_data, editing_current_track, was_playing)

        threading.Thread(target=_work, daemon=True).start()

    def _on_tags_saved(self, row, error, title, artist, album, track, cover_data,
                        editing_current_track=False, was_playing=False):
        if error:
            self._toast_overlay.add_toast(Adw.Toast(title=f'No se pudo guardar: {error}'))
            return GLib.SOURCE_REMOVE

        new_tags = dict(row.tags)
        new_tags['title']      = title
        new_tags['artist']     = artist
        new_tags['album']      = album
        new_tags['track']      = track
        new_tags['cover_data'] = cover_data
        row.refresh(new_tags)

        if editing_current_track:
            self._title_label.set_text(title or Path(row.path).stem)
            self._artist_label.set_text(artist)
            self._album_label.set_text(album)
            self._current_cover_data = cover_data
            pb = _pixbuf_from_bytes(cover_data, 160) if cover_data else None
            if pb:
                self._cover_image.set_from_pixbuf(pb)
            else:
                self._cover_image.set_from_icon_name('m3-music-note-symbolic')
                self._cover_image.set_pixel_size(160)
            self._update_cover_display_mode()
            chips = {}
            if album: chips['Álbum'] = album
            if track: chips['Pista'] = track
            self._update_meta_chips(chips)
            if was_playing:
                # Recargar el fichero (ya reescrito) desde el principio
                uri = 'file://' + urllib.parse.quote(row.path)
                self._player.play(uri)

        self._save_mp3_cache_now()
        toast = Adw.Toast(title='Etiquetas guardadas')
        toast.set_timeout(2)
        self._toast_overlay.add_toast(toast)
        return GLib.SOURCE_REMOVE

# ── Keyboard shortcuts ─────────────────────────────────────────────────────────

    def _on_key_pressed(self, _ctrl, keyval, _keycode, _state):
        if keyval == Gdk.KEY_space:
            self._on_play_pause(None)
            return True
        if keyval == Gdk.KEY_Left:
            self._on_prev_track(None)
            return True
        if keyval == Gdk.KEY_Right:
            self._on_next_track(None)
            return True
        if keyval in (Gdk.KEY_m, Gdk.KEY_M):
            self._toggle_mute()
            return True
        return False

    def _on_vol_btn_clicked(self, _btn):
        self._toggle_mute()

    def _toggle_mute(self):
        if self._muted:
            target = self._last_nonzero_vol if self._last_nonzero_vol > 0.0001 else 0.5
            self._muted = False
            self._player.set_volume(target)
            self._vol_scale.set_value(target)
        else:
            self._muted = True
            self._player.set_volume(0.0)
            self._vol_scale.set_value(0.0)
        self._update_vol_icon()

    def _update_vol_icon(self):
        if self._vol_btn is None:
            return
        self._vol_btn.set_icon_name(
            'm3-volume-off-symbolic' if self._muted else 'm3-volume-up-symbolic')
        self._vol_btn.set_tooltip_text('Activar sonido' if self._muted else 'Silenciar')

    # ── Sleep timer ───────────────────────────────────────────────────────────

    def _on_sleep_timer_set(self, minutes: int):
        if self._sleep_timer_id:
            GLib.source_remove(self._sleep_timer_id)
            self._sleep_timer_id = None
        self._sleep_remaining = 0
        if minutes == 0:
            self._sleep_btn.set_tooltip_text('Sleep timer')
            toast = Adw.Toast(title='Sleep timer desactivado')
            toast.set_timeout(2)
            self._toast_overlay.add_toast(toast)
            return
        self._sleep_remaining = minutes
        self._sleep_btn.set_tooltip_text(f'Sleep: {minutes} min restantes')
        self._sleep_timer_id = GLib.timeout_add_seconds(60, self._tick_sleep_timer)
        toast = Adw.Toast(title=f'Sleep timer: {minutes} minutos')
        toast.set_timeout(2)
        self._toast_overlay.add_toast(toast)

    def _tick_sleep_timer(self):
        self._sleep_remaining -= 1
        if self._sleep_remaining <= 0:
            self._sleep_timer_id = None
            self._sleep_btn.set_tooltip_text('Sleep timer')
            self._on_stop(None)
            self._toast_overlay.add_toast(Adw.Toast(title='Sleep timer: reproducción detenida'))
            return GLib.SOURCE_REMOVE
        self._sleep_btn.set_tooltip_text(f'Sleep: {self._sleep_remaining} min restantes')
        return GLib.SOURCE_CONTINUE

    # ── Añadir emisora manualmente ────────────────────────────────────────────

    def _on_add_station(self, _btn):
        dialog = Adw.MessageDialog(transient_for=self, heading='Añadir emisora')
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('add', 'Añadir')
        dialog.set_response_appearance('add', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('add')

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        form.set_margin_top(8)

        name_e = Gtk.Entry()
        name_e.set_placeholder_text('Nombre de la emisora *')
        form.append(name_e)

        url_e = Gtk.Entry()
        url_e.set_placeholder_text('URL del stream (http://…) *')
        form.append(url_e)

        genre_e = Gtk.Entry()
        genre_e.set_placeholder_text('Género (ej: Pop, Rock, Jazz)')
        form.append(genre_e)

        bitrate_e = Gtk.Entry()
        bitrate_e.set_placeholder_text('Bitrate kbps (ej: 128)')
        form.append(bitrate_e)

        dialog.set_extra_child(form)
        dialog.connect('response',
                       lambda d, r: self._on_add_station_response(
                           d, r, name_e, url_e, genre_e, bitrate_e))
        dialog.present()

    def _on_add_station_response(self, _dialog, response, name_e, url_e, genre_e, bitrate_e):
        if response != 'add':
            return
        name    = name_e.get_text().strip()
        url     = url_e.get_text().strip()
        genre   = genre_e.get_text().strip() or 'Sin género'
        bitrate = bitrate_e.get_text().strip()
        if not name or not url:
            self._toast_overlay.add_toast(
                Adw.Toast(title='El nombre y la URL son obligatorios'))
            return
        station = {'name': name, 'url': url, 'genre': genre,
                   'bitrate': bitrate, 'description': '', 'favicon': ''}
        self._add_station_row(station)
        toast = Adw.Toast(title=f'Emisora "{name}" añadida')
        toast.set_timeout(2)
        self._toast_overlay.add_toast(toast)

    # ── Exportar / Importar favoritos ─────────────────────────────────────────

    def _on_export_favorites(self, _btn):
        if not self._favorites:
            self._toast_overlay.add_toast(
                Adw.Toast(title='No hay favoritos para exportar'))
            return
        dialog = Gtk.FileDialog()
        dialog.set_title('Exportar favoritos')
        dialog.set_initial_name('favoritos_aerx.json')
        dialog.save(self, None, self._on_export_finish)

    def _on_export_finish(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except Exception:
            return
        if not gfile:
            return
        stations = [self._station_rows[u].station
                    for u in self._favorites if u in self._station_rows]
        try:
            with open(gfile.get_path(), 'w', encoding='utf-8') as f:
                json.dump(stations, f, indent=2, ensure_ascii=False)
            toast = Adw.Toast(title=f'{len(stations)} favoritos exportados')
            self._toast_overlay.add_toast(toast)
        except Exception as e:
            self._toast_overlay.add_toast(Adw.Toast(title=f'Error al exportar: {e}'))

    def _on_import_favorites(self, _btn):
        dialog = Gtk.FileDialog()
        dialog.set_title('Importar favoritos')
        ff = Gtk.FileFilter()
        ff.set_name('JSON')
        ff.add_pattern('*.json')
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(ff)
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_import_finish)

    def _on_import_finish(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except Exception:
            return
        if not gfile:
            return
        try:
            with open(gfile.get_path(), encoding='utf-8') as f:
                stations = json.load(f)
            if not isinstance(stations, list):
                raise ValueError('Formato inválido')
        except Exception as e:
            self._toast_overlay.add_toast(Adw.Toast(title=f'Error al importar: {e}'))
            return
        added = 0
        for s in stations:
            url = s.get('url', '')
            if not url:
                continue
            if url not in self._station_rows:
                self._add_station_row(s)
            self._favorites.add(url)
            row = self._station_rows.get(url)
            if row:
                row.set_favorite(True)
            added += 1
        if added:
            self._config['favorites'] = list(self._favorites)
            threading.Thread(target=lambda: _save_config(self._config), daemon=True).start()
            self._radio_list.invalidate_sort()
            self._radio_list.invalidate_filter()
        self._toast_overlay.add_toast(Adw.Toast(title=f'{added} favoritos importados'))

    # ── Ordenar MP3 ───────────────────────────────────────────────────────────

    _MP3_SORT_MODES = [
        ('filename', 'm3-sort-alpha-symbolic', 'Ordenar: Nombre de archivo'),
        ('title',    'm3-title-symbolic',      'Ordenar: Título'),
        ('artist',   'm3-person-symbolic',     'Ordenar: Artista'),
        ('album',    'm3-album-symbolic',      'Ordenar: Álbum'),
    ]

    def _on_mp3_sort_toggle(self, _btn):
        names = [m[0] for m in self._MP3_SORT_MODES]
        idx = (names.index(self._mp3_sort_mode) + 1) % len(self._MP3_SORT_MODES)
        mode, icon, label = self._MP3_SORT_MODES[idx]
        self._mp3_sort_mode = mode
        self._mp3_sort_btn.set_icon_name(icon)
        self._mp3_sort_btn.set_tooltip_text(label)
        self._mp3_list.invalidate_sort()
        toast = Adw.Toast(title=label)
        toast.set_timeout(1)
        self._toast_overlay.add_toast(toast)

    def _mp3_sort_func(self, row1, row2):
        if not isinstance(row1, Mp3Row) or not isinstance(row2, Mp3Row):
            return 0
        mode = self._mp3_sort_mode
        if mode == 'title':
            k1 = (row1.tags.get('title') or Path(row1.path).stem).lower()
            k2 = (row2.tags.get('title') or Path(row2.path).stem).lower()
        elif mode == 'artist':
            k1 = (row1.tags.get('artist') or '').lower()
            k2 = (row2.tags.get('artist') or '').lower()
        elif mode == 'album':
            k1 = (row1.tags.get('album') or '').lower()
            k2 = (row2.tags.get('album') or '').lower()
        else:
            k1 = Path(row1.path).name.lower()
            k2 = Path(row2.path).name.lower()
        return -1 if k1 < k2 else (1 if k1 > k2 else 0)

    # ── Notificaciones de escritorio ──────────────────────────────────────────

    def _notify_now_playing(self, title: str, body: str = ''):
        try:
            notif = Gio.Notification.new(title or 'ÆRx Player')
            if body:
                notif.set_body(body)
            self.get_application().send_notification('aerx-now-playing', notif)
        except Exception:
            pass

    # ── Preferencias ─────────────────────────────────────────────────────────────

    def _build_settings_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(24); box.set_margin_end(24)
        box.set_margin_top(20);   box.set_margin_bottom(16)

        title = Gtk.Label(label='Ajustes')
        title.add_css_class('title-1')
        title.set_xalign(0)
        title.set_margin_bottom(20)
        box.append(title)

        theme_lbl = Gtk.Label(label='Tema')
        theme_lbl.add_css_class('heading')
        theme_lbl.set_xalign(0)
        theme_lbl.set_margin_bottom(8)
        box.append(theme_lbl)

        theme_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        theme_box.add_css_class('linked')
        theme_box.set_halign(Gtk.Align.START)
        theme_box.set_margin_bottom(24)

        first_btn = None
        for mode, label_text in (('system', 'Sistema'), ('light', 'Claro'), ('dark', 'Oscuro')):
            tbtn = Gtk.ToggleButton(label=label_text)
            if first_btn is None:
                first_btn = tbtn
            else:
                tbtn.set_group(first_btn)
            tbtn.set_active(self._theme_mode == mode)
            tbtn.connect('toggled', self._on_theme_mode_toggled, mode)
            theme_box.append(tbtn)
        box.append(theme_box)

        general_lbl = Gtk.Label(label='General')
        general_lbl.add_css_class('heading')
        general_lbl.set_xalign(0)
        general_lbl.set_margin_bottom(8)
        box.append(general_lbl)

        self._update_check_btn = Gtk.CheckButton(
            label='Buscar actualizaciones al iniciar la aplicación')
        self._update_check_btn.set_active(self._check_updates_on_startup)
        self._update_check_btn.connect('toggled', self._on_settings_changed)
        box.append(self._update_check_btn)

        self._notif_check_btn = Gtk.CheckButton(label='Mostrar notificaciones de escritorio')
        self._notif_check_btn.set_active(self._desktop_notifications)
        self._notif_check_btn.connect('toggled', self._on_settings_changed)
        box.append(self._notif_check_btn)

        return box

    def _on_theme_mode_toggled(self, btn: Gtk.ToggleButton, mode: str):
        if not btn.get_active():
            return
        self._theme_mode = mode
        self._config['theme_mode'] = mode
        threading.Thread(target=lambda: _save_config(self._config), daemon=True).start()
        Adw.StyleManager.get_default().set_color_scheme(
            self._THEME_SCHEME_MAP.get(mode, Adw.ColorScheme.DEFAULT)
        )

    def _on_settings_changed(self, _btn=None):
        self._check_updates_on_startup = self._update_check_btn.get_active()
        self._desktop_notifications    = self._notif_check_btn.get_active()
        self._config['check_updates_on_startup'] = self._check_updates_on_startup
        self._config['desktop_notifications']    = self._desktop_notifications
        threading.Thread(target=lambda: _save_config(self._config), daemon=True).start()

    # ── Comprobación de actualizaciones al iniciar ──────────────────────────────

    def _check_updates_silently(self):
        update_check.check_latest(
            APP_VERSION,
            lambda info, err: GLib.idle_add(self._on_startup_update_result, info, err))
        return GLib.SOURCE_REMOVE

    def _on_startup_update_result(self, info, _error):
        if info and info.get('is_newer'):
            toast = Adw.Toast(title=f"Hay una nueva versión disponible: v{info['version']}")
            toast.set_button_label('Descargar')
            toast.set_timeout(0)
            toast.connect('button-clicked',
                          lambda _t: Gtk.show_uri(self, info['download_url'], Gdk.CURRENT_TIME))
            self._toast_overlay.add_toast(toast)
        return GLib.SOURCE_REMOVE

    # ── Acerca de ─────────────────────────────────────────────────────────────

    def _on_about(self, _btn):
        update_check.check_latest(APP_VERSION,
                                   lambda info, err: GLib.idle_add(self._present_about, info, err))

    def _present_about(self, update_info, _update_error):
        _icon_name = 'aerx-player'
        comments = 'Radio y Audio, sin fronteras.\nReproductor de radio online y archivos de audio locales\n\nMade with ❤ by SaruMan'

        if update_info and update_info.get('is_newer'):
            update_label = f"⬇ Descargar la nueva versión v{update_info['version']}"
            update_uri = update_info['download_url']
        elif update_info:
            update_label = f'Buscar actualizaciones (tienes la última versión, v{APP_VERSION})'
            update_uri = update_info['page_url']
        else:
            update_label = 'Buscar actualizaciones'
            update_uri = update_check.RELEASES_PAGE_URL

        if hasattr(Adw, 'AboutDialog'):
            about = Adw.AboutDialog()
            about.set_application_name('ÆRx Player')
            about.set_version(APP_VERSION)
            about.set_developer_name('SaruMan')
            about.set_license_type(Gtk.License.GPL_3_0)
            about.set_comments(comments)
            about.set_application_icon(_icon_name)
            about.add_link(update_label, update_uri)
            about.add_link('☕ Apóyame en Ko-fi', KOFI_URL)
            about.present(self)
        else:
            about = Adw.AboutWindow(transient_for=self)
            about.set_application_name('ÆRx Player')
            about.set_version(APP_VERSION)
            about.set_developer_name('SaruMan')
            about.set_license_type(Gtk.License.GPL_3_0)
            about.set_comments(comments)
            about.set_application_icon(_icon_name)
            about.add_link(update_label, update_uri)
            about.add_link('☕ Apóyame en Ko-fi', KOFI_URL)
            about.present()
        return GLib.SOURCE_REMOVE


# ── Formatting ─────────────────────────────────────────────────────────────────

def _fmt_time(ns: int) -> str:
    s = ns // 1_000_000_000
    return f'{s // 60}:{s % 60:02d}'


# ── Application ────────────────────────────────────────────────────────────────

class RadioApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id='es.aerx.player',
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.connect('activate', self._on_activate)

    def _on_activate(self, app):
        import os as _os, shutil as _sh, hashlib as _hl
        _base = _os.path.dirname(_os.path.abspath(__file__))
        _src = _os.path.join(_base, 'data', 'icons', 'hicolor', '256x256', 'apps', 'aerx-player.png')
        if _os.path.exists(_src):
            _dest_dir = _os.path.join(_os.path.expanduser('~'),
                                      '.local', 'share', 'icons', 'hicolor', '256x256', 'apps')
            _os.makedirs(_dest_dir, exist_ok=True)
            _dst = _os.path.join(_dest_dir, 'aerx-player.png')
            _md5 = lambda p: _hl.md5(open(p, 'rb').read()).hexdigest()
            if not _os.path.exists(_dst) or _md5(_src) != _md5(_dst):
                _sh.copy(_src, _dst)   # copy sin copiar mtime → GTK invalida caché
        win = RadioWindow(app)
        win.present()


def main():
    app = RadioApp()
    sys.exit(app.run(sys.argv))


if __name__ == '__main__':
    main()
