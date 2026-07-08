#!/usr/bin/env python3
"""RadioES – Reproductor de radio española online y archivos MP3 (GTK4/Adwaita)."""

import sys
import json
import base64
import threading
import urllib.parse
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
import metadata as meta_mod

Gst.init(None)

APP_VERSION = '1.2.2'

DATA_DIR      = Path(__file__).parent / 'data'
STATIONS_FILE = DATA_DIR / 'spanish_stations.json'
CONFIG_DIR    = Path.home() / '.local' / 'share' / 'radioes'
CONFIG_FILE   = CONFIG_DIR / 'config.json'
CACHE_FILE    = CONFIG_DIR / 'mp3_cache.json'

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

_FULLSCREEN_MIN_SIDE = 500   # lado menor mínimo (px) para activar el fondo a pantalla completa
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


def _save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def _load_mp3_cache() -> list:
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_mp3_cache(tracks: list):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(tracks, f, indent=2)
    except Exception:
        pass


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
        self._logo.set_from_icon_name('audio-x-generic')
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
            badge.add_css_class('caption')
            badge.add_css_class('dim-label')
            box.append(badge)

        self._fav_btn = Gtk.Button()
        self._fav_btn.set_icon_name('starred-symbolic' if is_favorite else 'non-starred-symbolic')
        self._fav_btn.add_css_class('flat')
        self._fav_btn.add_css_class('circular')
        self._fav_btn.set_valign(Gtk.Align.CENTER)
        if on_toggle_fav:
            self._fav_btn.connect('clicked', lambda btn: on_toggle_fav(station, btn))
        box.append(self._fav_btn)

    def set_favorite(self, is_fav: bool):
        self._fav_btn.set_icon_name('starred-symbolic' if is_fav else 'non-starred-symbolic')

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
            star_img = Gtk.Image.new_from_icon_name('starred-symbolic')
            star_img.add_css_class('warning')
            box.append(star_img)

        lbl = Gtk.Label(label=genre)
        lbl.add_css_class('heading')
        lbl.set_hexpand(True)
        lbl.set_xalign(0)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(lbl)

        self._arrow = Gtk.Image.new_from_icon_name('pan-down-symbolic')
        box.append(self._arrow)

        gc = Gtk.GestureClick()
        gc.connect('released', lambda g, n, x, y: on_toggle(genre))
        self.add_controller(gc)

    def set_collapsed(self, collapsed: bool):
        self._arrow.set_from_icon_name(
            'pan-end-symbolic' if collapsed else 'pan-down-symbolic'
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
        self._edit_btn.set_icon_name('document-edit-symbolic')
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
                self._art.set_from_icon_name('audio-x-generic')
        else:
            self._art.set_from_icon_name('audio-x-generic')

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
        btn.set_icon_name('media-playlist-repeat-symbolic')
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
        self.set_title('RadioES')
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
        self._sleep_timer_id      = None
        self._sleep_remaining     = 0
        self._current_cover_data  = None
        self._cover_fullscreen_active = False

        self._install_cover_bg_css()
        self._sleep_btn           = None
        self._mp3_sort_mode       = 'filename'
        self._mp3_sort_btn        = None
        self._muted               = False
        self._pre_mute_vol        = 0.8
        self._last_notified_title = ''

        self._config       = _load_config()
        self._favorites: set[str] = set(self._config.get('favorites', []))
        self._music_folder = self._config.get(
            'music_folder', str(Path.home() / 'musica')
        )
        self._check_updates_on_startup = self._config.get('check_updates_on_startup', True)
        self._desktop_notifications    = self._config.get('desktop_notifications', True)
        self._saved_volume  = self._config.get('volume', 0.8)
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

        self._view_stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self._view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self._view_stack.connect('notify::visible-child', self._on_tab_switched)

        if _HAS_OVERLAY_SPLIT:
            self._sidebar_btn = Gtk.ToggleButton()
            self._sidebar_btn.set_icon_name('sidebar-show-symbolic')
            self._sidebar_btn.set_tooltip_text('Mostrar/ocultar panel lateral')
            self._sidebar_btn.set_active(True)
            header.pack_start(self._sidebar_btn)

        about_btn = Gtk.Button()
        about_btn.set_icon_name('help-about-symbolic')
        about_btn.set_tooltip_text('Acerca de RadioES')
        about_btn.add_css_class('flat')
        about_btn.connect('clicked', self._on_about)
        header.pack_end(about_btn)

        prefs_btn = Gtk.Button()
        prefs_btn.set_icon_name('preferences-system-symbolic')
        prefs_btn.set_tooltip_text('Preferencias')
        prefs_btn.add_css_class('flat')
        prefs_btn.connect('clicked', self._on_preferences)
        header.pack_end(prefs_btn)

        root.add_top_bar(header)

        self._build_radio_page()
        self._build_mp3_page()

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

        if _HAS_OVERLAY_SPLIT:
            self._split_view = Adw.OverlaySplitView()
            self._split_view.set_sidebar(self._view_stack)
            self._split_view.set_content(content_overlay)
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
            paned.set_start_child(self._view_stack)
            paned.set_end_child(content_overlay)
            split_container = paned

        controls = self._build_controls()

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(split_container)
        content_box.append(controls)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(content_box)
        root.set_content(self._toast_overlay)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self.add_controller(key_ctrl)

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

        discover_btn = Gtk.Button()
        discover_btn.set_icon_name('network-wireless-symbolic')
        discover_btn.set_tooltip_text('Descubrir más emisoras (Radio Browser)')
        discover_btn.add_css_class('flat')
        discover_btn.connect('clicked', self._on_discover)
        search_bar.append(discover_btn)

        add_btn = Gtk.Button()
        add_btn.set_icon_name('list-add-symbolic')
        add_btn.set_tooltip_text('Añadir emisora manualmente')
        add_btn.add_css_class('flat')
        add_btn.connect('clicked', self._on_add_station)
        search_bar.append(add_btn)

        fav_menu_btn = Gtk.MenuButton()
        fav_menu_btn.set_icon_name('document-save-symbolic')
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

        fav_header = GenreHeaderRow('Favoritas', self._toggle_genre_collapse)
        self._genre_headers['Favoritas'] = fav_header
        self._radio_list.append(fav_header)

        scroll.set_child(self._radio_list)
        page_box.append(scroll)

        self._view_stack.add_titled_with_icon(
            page_box, 'radio', 'Radio', 'audio-input-microphone-symbolic'
        )

    def _build_mp3_page(self):
        mp3_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Folder selector row
        folder_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        folder_bar.set_margin_start(8); folder_bar.set_margin_end(8)
        folder_bar.set_margin_top(8);   folder_bar.set_margin_bottom(4)

        folder_icon = Gtk.Image.new_from_icon_name('folder-music-symbolic')
        folder_bar.append(folder_icon)

        self._folder_label = Gtk.Label(label=self._music_folder)
        self._folder_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._folder_label.set_hexpand(True)
        self._folder_label.set_xalign(0)
        self._folder_label.add_css_class('caption')
        folder_bar.append(self._folder_label)

        choose_btn = Gtk.Button()
        choose_btn.set_icon_name('document-open-symbolic')
        choose_btn.set_tooltip_text('Elegir carpeta de música')
        choose_btn.add_css_class('flat')
        choose_btn.connect('clicked', self._on_choose_folder)
        folder_bar.append(choose_btn)

        self._scan_btn = Gtk.Button()
        self._scan_btn.set_icon_name('view-refresh-symbolic')
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
        self._mp3_sort_btn.set_icon_name('view-sort-ascending-symbolic')
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

        self._view_stack.add_titled_with_icon(
            mp3_box, 'mp3', 'MP3', 'audio-x-generic-symbolic'
        )

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
        self._cover_image.set_from_icon_name('audio-x-generic')
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
        info_box.append(self._artist_label)

        self._album_label = Gtk.Label(label='')
        self._album_label.add_css_class('caption')
        self._album_label.add_css_class('dim-label')
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
        self._prev_btn.set_icon_name('media-skip-backward-symbolic')
        self._prev_btn.add_css_class('circular')
        self._prev_btn.connect('clicked', self._on_prev_track)
        bar.append(self._prev_btn)

        self._play_btn = Gtk.Button()
        self._play_btn.set_icon_name('media-playback-start-symbolic')
        self._play_btn.add_css_class('circular')
        self._play_btn.add_css_class('suggested-action')
        self._play_btn.set_size_request(48, 48)
        self._play_btn.connect('clicked', self._on_play_pause)
        bar.append(self._play_btn)

        stop_btn = Gtk.Button()
        stop_btn.set_icon_name('media-playback-stop-symbolic')
        stop_btn.add_css_class('circular')
        stop_btn.connect('clicked', self._on_stop)
        bar.append(stop_btn)

        self._next_btn = Gtk.Button()
        self._next_btn.set_icon_name('media-skip-forward-symbolic')
        self._next_btn.add_css_class('circular')
        self._next_btn.connect('clicked', self._on_next_track)
        bar.append(self._next_btn)

        self._mode_btn = Gtk.Button()
        _mode_icon, _mode_label = next(
            ((icon, label) for name, icon, label in self._PLAY_MODES if name == self._play_mode),
            ('media-playlist-consecutive-symbolic', 'Modo: Secuencial'))
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

        vol_icon = Gtk.Image.new_from_icon_name('audio-volume-high-symbolic')
        bar.append(vol_icon)

        self._vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.05)
        self._vol_scale.set_value(self._player.get_volume())
        self._vol_scale.set_size_request(100, -1)
        self._vol_scale.set_draw_value(False)
        self._vol_scale.connect('value-changed', self._on_volume_changed)
        bar.append(self._vol_scale)

        self._sleep_btn = Gtk.MenuButton()
        self._sleep_btn.set_icon_name('alarm-symbolic')
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
        def _key(row):
            if isinstance(row, GenreHeaderRow):
                # Favoritas header always first; other genre headers before their stations
                if row.genre == 'Favoritas':
                    return ('\x00', 0, '')
                return (row.genre.lower(), 0, '')
            if isinstance(row, StationRow):
                url = row.station.get('url', '')
                if url in self._favorites:
                    return ('\x00', 1, row.station.get('name', '').lower())
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
            btn.set_icon_name('non-starred-symbolic')
        else:
            self._favorites.add(url)
            btn.set_icon_name('starred-symbolic')
        self._config['favorites'] = list(self._favorites)
        threading.Thread(target=lambda: _save_config(self._config), daemon=True).start()
        self._radio_list.invalidate_sort()
        self._radio_list.invalidate_filter()

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
        self._scan_btn.set_icon_name('process-working-symbolic')

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
        self._scan_btn.set_icon_name('view-refresh-symbolic')
        self._save_mp3_cache_now()
        msg = (f'Se encontraron {added} canciones nuevas (incluyendo subcarpetas)'
               if added else 'No hay canciones nuevas')
        self._toast_overlay.add_toast(Adw.Toast(title=msg))

    # ── Play mode ──────────────────────────────────────────────────────────────

    _PLAY_MODES = [
        ('sequential', 'media-playlist-consecutive-symbolic', 'Modo: Secuencial'),
        ('repeat',     'media-playlist-repeat-symbolic',      'Modo: Repetir lista'),
        ('shuffle',    'media-playlist-shuffle-symbolic',     'Modo: Aleatorio'),
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

        url = row.station.get('url', '')
        section = 'Favoritas' if url in self._favorites else (
            row.station.get('genre', '') or 'Sin género'
        )
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
            'Género':  row.station.get('genre', ''),
            'Bitrate': f"{row.station.get('bitrate', '')}kbps",
        })

        if row.logo_bytes:
            self._set_cover_from_bytes(row.logo_bytes)
        else:
            self._cover_image.set_from_icon_name('audio-input-microphone')
            self._cover_image.set_pixel_size(160)
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
            self._cover_image.set_from_icon_name('audio-x-generic')
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
        self._play_btn.set_icon_name('media-playback-start-symbolic')
        self._current_cover_data = None
        self._update_cover_display_mode()

    def _on_volume_changed(self, scale):
        self._player.set_volume(scale.get_value())
        if self._volume_save_timer:
            GLib.source_remove(self._volume_save_timer)
        self._volume_save_timer = GLib.timeout_add(500, self._save_volume_now, scale.get_value())

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
        icon = 'media-playback-pause-symbolic' if playing else 'media-playback-start-symbolic'
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
        return True

    # ── UI helpers ─────────────────────────────────────────────────────────────

    def _set_radio_mode(self, is_radio: bool):
        self._live_box.set_visible(is_radio)
        self._progress_box.set_visible(not is_radio)

    def _on_tab_switched(self, stack, _param):
        if self._mode_btn is None:
            return  # la pila puede emitir el cambio inicial antes de _build_controls()
        is_mp3 = stack.get_visible_child_name() == 'mp3'
        self._mode_btn.set_visible(is_mp3)

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
            self._cover_image.set_from_icon_name('audio-input-microphone')
            self._cover_image.set_pixel_size(160)

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
        self._view_stack.set_visible_child_name('mp3')

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
                preview_img.set_from_icon_name('audio-x-generic')

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
                self._cover_image.set_from_icon_name('audio-x-generic')
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

    # ── Discover (Radio Browser API) ───────────────────────────────────────────

    def _on_discover(self, _btn):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading='Buscar en Radio Browser',
            body='Cargando emisoras de España desde la API de Radio Browser…',
        )
        dialog.add_response('cancel', 'Cancelar')
        dialog.present()

        radio_browser.fetch_stations(
            country='Spain',
            limit=200,
            callback=lambda data, err: GLib.idle_add(
                self._on_browser_result, data, err, dialog
            ),
        )

    def _on_browser_result(self, stations, error, dialog):
        dialog.close()
        if error:
            err_dlg = Adw.MessageDialog(
                transient_for=self,
                heading='Error al obtener emisoras',
                body=str(error),
            )
            err_dlg.add_response('ok', 'Aceptar')
            err_dlg.present()
            return

        added = 0
        for s in (stations or []):
            url = s.get('url_resolved') or s.get('url', '')
            if not url or url in self._station_rows:
                continue
            station = {
                'name':        s.get('name', ''),
                'url':         url,
                'favicon':     s.get('favicon', ''),
                'genre':       s.get('tags', ''),
                'bitrate':     s.get('bitrate', ''),
                'description': s.get('country', ''),
            }
            self._add_station_row(station)
            self._fetch_station_logos([station])
            added += 1

        toast = Adw.Toast(title=f'Se añadieron {added} emisoras nuevas')
        self._toast_overlay.add_toast(toast)


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

    def _toggle_mute(self):
        if self._muted:
            self._muted = False
            self._player.set_volume(self._pre_mute_vol)
            self._vol_scale.set_value(self._pre_mute_vol)
        else:
            self._pre_mute_vol = self._vol_scale.get_value()
            self._muted = True
            self._player.set_volume(0.0)
            self._vol_scale.set_value(0.0)

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
        dialog.set_initial_name('favoritos_radioes.json')
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
        ('filename', 'view-sort-ascending-symbolic', 'Ordenar: Nombre de archivo'),
        ('title',    'format-text-rich-symbolic',    'Ordenar: Título'),
        ('artist',   'system-users-symbolic',        'Ordenar: Artista'),
        ('album',    'media-optical-symbolic',       'Ordenar: Álbum'),
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
            notif = Gio.Notification.new(title or 'RadioES')
            if body:
                notif.set_body(body)
            self.get_application().send_notification('radioes-now-playing', notif)
        except Exception:
            pass

    # ── Preferencias ─────────────────────────────────────────────────────────────

    def _on_preferences(self, _btn):
        dialog = Adw.MessageDialog(transient_for=self, heading='Preferencias')
        dialog.add_response('close', 'Cerrar')
        dialog.set_default_response('close')

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)

        update_check_btn = Gtk.CheckButton(label='Buscar actualizaciones al iniciar la aplicación')
        update_check_btn.set_active(self._check_updates_on_startup)
        box.append(update_check_btn)

        notif_check_btn = Gtk.CheckButton(label='Mostrar notificaciones de escritorio')
        notif_check_btn.set_active(self._desktop_notifications)
        box.append(notif_check_btn)

        dialog.set_extra_child(box)

        dialog.connect('response', lambda d, r: self._on_preferences_response(
            d, r, update_check_btn, notif_check_btn))
        dialog.present()

    def _on_preferences_response(self, _dialog, _response, update_check_btn, notif_check_btn):
        self._check_updates_on_startup = update_check_btn.get_active()
        self._desktop_notifications    = notif_check_btn.get_active()
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
        _icon_name = 'radioes'
        comments = 'Reproductor de radio española online y archivos de audio locales\n\nMade with ❤ by SaruMan'

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
            about.set_application_name('RadioES')
            about.set_version(APP_VERSION)
            about.set_developer_name('SaruMan')
            about.set_license_type(Gtk.License.MIT_X11)
            about.set_comments(comments)
            about.set_application_icon(_icon_name)
            about.add_link(update_label, update_uri)
            about.present(self)
        else:
            about = Adw.AboutWindow(transient_for=self)
            about.set_application_name('RadioES')
            about.set_version(APP_VERSION)
            about.set_developer_name('SaruMan')
            about.set_license_type(Gtk.License.MIT_X11)
            about.set_comments(comments)
            about.set_application_icon(_icon_name)
            about.add_link(update_label, update_uri)
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
            application_id='es.radioes.app',
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.connect('activate', self._on_activate)

    def _on_activate(self, app):
        import os as _os, shutil as _sh, hashlib as _hl
        _base = _os.path.dirname(_os.path.abspath(__file__))
        _src = _os.path.join(_base, 'data', 'icons', 'radioes-256.png')
        if _os.path.exists(_src):
            _dest_dir = _os.path.join(_os.path.expanduser('~'),
                                      '.local', 'share', 'icons', 'hicolor', '256x256', 'apps')
            _os.makedirs(_dest_dir, exist_ok=True)
            _dst = _os.path.join(_dest_dir, 'radioes.png')
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
