"""GStreamer audio player with ICY stream metadata support."""

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib, GObject

Gst.init(None)


class Player(GObject.Object):
    """Playbin-based player that emits signals for UI updates."""

    __gsignals__ = {
        'metadata-changed': (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)),
        'cover-data':       (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        'state-changed':    (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        'error':            (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Emitted ~20 fps with a list of float magnitudes in dB (64 bands)
        'spectrum':         (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        # Emitted when the stream ends naturally (not on stop/pause)
        'eos':              (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    SPECTRUM_BANDS    = 40
    SPECTRUM_INTERVAL = 50_000_000   # 50 ms → 20 fps

    def __init__(self):
        super().__init__()
        self._playing = False
        self._volume = 0.8
        self._pipeline = Gst.ElementFactory.make('playbin', 'player')
        if not self._pipeline:
            raise RuntimeError("GStreamer playbin unavailable – install gstreamer1.0-plugins-base")
        self._pipeline.set_property('volume', self._volume)

        # Force audio-only flags: disable video, subtitles, vis; keep audio + soft-volume
        GST_PLAY_FLAG_AUDIO        = 0x00000002
        GST_PLAY_FLAG_SOFT_VOLUME  = 0x00000010
        self._pipeline.set_property('flags', GST_PLAY_FLAG_AUDIO | GST_PLAY_FLAG_SOFT_VOLUME)

        # Insert spectrum analyser as an audio-filter (passthrough + FFT messages)
        self._spectrum_el = Gst.ElementFactory.make('spectrum', 'spectrum')
        if self._spectrum_el:
            self._spectrum_el.set_property('post-messages', True)
            self._spectrum_el.set_property('bands',    self.SPECTRUM_BANDS)
            self._spectrum_el.set_property('threshold', -80)
            self._spectrum_el.set_property('interval',  self.SPECTRUM_INTERVAL)
            self._pipeline.set_property('audio-filter', self._spectrum_el)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message::tag',           self._on_tag)
        bus.connect('message::eos',           self._on_eos)
        bus.connect('message::error',         self._on_error)
        bus.connect('message::buffering',     self._on_buffering)
        bus.connect('message::element',       self._on_element_msg)
        bus.connect('message::state-changed', self._on_state_changed)

    # ── Public API ────────────────────────────────────────────────────────────

    def play(self, uri: str):
        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline.set_property('uri', uri)
        self._pipeline.set_property('volume', self._volume)
        self._pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self._pipeline.set_state(Gst.State.NULL)
        self._playing = False   # sync update so toggle_pause() is correct immediately

    def toggle_pause(self):
        if self._playing:
            self._pipeline.set_state(Gst.State.PAUSED)
        else:
            self._pipeline.set_state(Gst.State.PLAYING)

    @property
    def is_playing(self) -> bool:
        return self._playing

    def set_volume(self, vol: float):
        self._volume = max(0.0, min(1.0, vol))
        self._pipeline.set_property('volume', self._volume)

    def get_volume(self) -> float:
        return self._volume

    def get_position(self) -> tuple:
        """Return (position_ns, duration_ns); -1 when unknown."""
        ok1, pos = self._pipeline.query_position(Gst.Format.TIME)
        ok2, dur = self._pipeline.query_duration(Gst.Format.TIME)
        return (pos if ok1 else -1, dur if ok2 else -1)

    def seek(self, pos_ns: int):
        self._pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            pos_ns,
        )

    def dispose(self):
        self._pipeline.set_state(Gst.State.NULL)

    # ── GStreamer bus callbacks ────────────────────────────────────────────────

    def _on_tag(self, _bus, message):
        tags = message.parse_tag()

        def _get(key):
            ok, val = tags.get_string(key)
            return val if ok else ''

        title  = _get('title') or _get('organization')
        artist = _get('artist')
        album  = _get('album')

        if title or artist or album:
            GLib.idle_add(self.emit, 'metadata-changed', title, artist, album)

        # Embedded cover art
        ok, sample = tags.get_sample('image')
        if ok and sample:
            buf = sample.get_buffer()
            ok2, minfo = buf.map(Gst.MapFlags.READ)
            if ok2:
                data = bytes(minfo.data)
                buf.unmap(minfo)
                GLib.idle_add(self.emit, 'cover-data', data)

    def _on_eos(self, _bus, _msg):
        self._playing = False
        GLib.idle_add(self.emit, 'state-changed', False)
        GLib.idle_add(self.emit, 'eos')

    def _on_buffering(self, _bus, message):
        pct = message.parse_buffering()
        if pct < 100:
            self._pipeline.set_state(Gst.State.PAUSED)
        else:
            self._pipeline.set_state(Gst.State.PLAYING)

    def _on_error(self, _bus, message):
        err, dbg = message.parse_error()
        # Translate the most common ICY/stream errors to Spanish
        msg = str(err)
        if 'Could not determine type' in msg or 'not enough data' in msg.lower():
            msg = 'No se pudo determinar el tipo de stream. La emisora puede estar caída o la URL puede haber cambiado.'
        elif 'Could not connect' in msg or 'Connection refused' in msg:
            msg = 'No se pudo conectar a la emisora. Comprueba tu conexión a internet.'
        elif 'Not found' in msg or '404' in msg:
            msg = 'La URL del stream no existe (404). La emisora puede haber cambiado de dirección.'
        GLib.idle_add(self.emit, 'error', msg)

    # PyGObject cannot auto-convert GstValueArray/GstValueList to Python lists,
    # so we parse the structure's canonical string representation instead.
    # Format: magnitude=(float){ -13.4, -18.6, ... };
    _MAG_RE = __import__('re').compile(r'magnitude=[^{]*\{([^}]+)\}')
    _NUM_RE = __import__('re').compile(r'-?\d+\.?\d*(?:e[+-]?\d+)?')

    def _on_element_msg(self, _bus, message):
        s = message.get_structure()
        if not s or s.get_name() != 'spectrum':
            return
        m = self._MAG_RE.search(s.to_string())
        if not m:
            return
        mags = [float(v) for v in self._NUM_RE.findall(m.group(1))]
        if mags:
            GLib.idle_add(self.emit, 'spectrum', mags)

    def _on_state_changed(self, _bus, message):
        if message.src is self._pipeline:
            _old, new, _pending = message.parse_state_changed()
            self._playing = new == Gst.State.PLAYING
            GLib.idle_add(self.emit, 'state-changed', self._playing)
