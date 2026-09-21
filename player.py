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

    # Retraso mínimo (ms) aplicado a cada frame del espectro antes de
    # emitirlo. GST_QUERY_LATENCY (ver _refresh_output_latency) apenas
    # aporta nada aquí: ese mecanismo está pensado para fuentes "live"
    # (cámara/micrófono) y en un pipeline de solo audio no-live (como este,
    # incluso con streams de radio por red) suele devolver 0, aunque el
    # sink de audio (ALSA/Pulse/PipeWire) sí tenga su propio buffer de
    # salida real. Este valor fijo es una estimación razonable de ese
    # buffer + el jitter de red típico de un stream de radio; si el
    # espectro se sigue viendo desincronizado, ajustar este número.
    SPECTRUM_SYNC_FLOOR_MS = 150

    def __init__(self):
        super().__init__()
        self._playing = False
        self._volume = 0.8
        self._output_latency_ns = 0
        self._pending_spectrum_timers = set()
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
        # Evita que un frame del espectro de la pista anterior, ya en
        # camino, llegue tarde y se pinte encima de la nueva.
        self._cancel_pending_spectrum()

    def stop(self):
        self._pipeline.set_state(Gst.State.NULL)
        self._playing = False   # sync update so toggle_pause() is correct immediately
        self._cancel_pending_spectrum()

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
        self._cancel_pending_spectrum()

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
        if not mags:
            return
        delay_ms = max(self.SPECTRUM_SYNC_FLOOR_MS,
                       min(500, self._output_latency_ns // 1_000_000))

        # No se puede pasar el propio id de GLib.timeout_add() a su propio
        # callback, así que se captura en un dict mutable para que el
        # callback pueda quitarse solo de _pending_spectrum_timers al
        # disparar (si no, la lista de pendientes crecería sin límite
        # durante una reproducción larga).
        holder = {}

        def _fire():
            self._pending_spectrum_timers.discard(holder.get('id'))
            self.emit('spectrum', mags)
            return GLib.SOURCE_REMOVE

        holder['id'] = GLib.timeout_add(delay_ms, _fire)
        self._pending_spectrum_timers.add(holder['id'])

    def _cancel_pending_spectrum(self):
        """Cancela los frames del espectro ya programados pero aún sin
        emitir — necesario al parar/cambiar de pista (para que no lleguen
        tarde y pinten datos obsoletos encima de la nueva reproducción) y
        al cambiar el retraso a mitad de stream (para que no se desordenen
        frames programados con retrasos distintos)."""
        for source_id in self._pending_spectrum_timers:
            GLib.source_remove(source_id)
        self._pending_spectrum_timers.clear()

    def _on_state_changed(self, _bus, message):
        if message.src is self._pipeline:
            _old, new, _pending = message.parse_state_changed()
            self._playing = new == Gst.State.PLAYING
            if self._playing:
                self._refresh_output_latency()
            GLib.idle_add(self.emit, 'state-changed', self._playing)

    def _refresh_output_latency(self):
        """El elemento `spectrum` analiza el audio justo tras decodificar,
        antes del buffer del sink (ALSA/Pulse/PipeWire) — sin compensar
        esto, el espectrograma se "adelanta" a lo que realmente se oye.
        Se consulta la latencia real del pipeline (estándar de GStreamer
        para estos casos) y se usa para retrasar la emisión de cada frame."""
        query = Gst.Query.new_latency()
        if self._pipeline.query(query):
            _live, min_latency, _max_latency = query.parse_latency()
            if (min_latency and min_latency != Gst.CLOCK_TIME_NONE
                    and min_latency != self._output_latency_ns):
                self._output_latency_ns = min_latency
                # Si hay frames ya programados con el retraso anterior,
                # cancelarlos evita que lleguen desordenados respecto a los
                # que se programen a partir de ahora con el retraso nuevo.
                self._cancel_pending_spectrum()
