# RadioES

> Reproductor de radio española online y archivos de audio locales, con interfaz GTK4/Adwaita.

![RadioES screenshot placeholder](data/icons/radioes-256.png)
<p align=center>
    <img width="900" alt="Radio" src="https://github.com/user-attachments/assets/0bb83f7c-810d-4082-9594-d6cb41fa485f" />
    <img width="900" alt="MP3" src="https://github.com/user-attachments/assets/9c1977ab-39b0-433d-a49b-2fc1e600d399" />
    <img width="962" height="639" alt="image" src="https://github.com/user-attachments/assets/a315648c-7997-43f1-aa93-7a29e35e8262" />
</p>

RadioES es una aplicación de escritorio para **distribuciones basadas en Debian/Ubuntu** (Ubuntu 22.04+, Linux Mint 21+, Debian 12+) que permite escuchar emisoras de radio españolas en directo y reproducir archivos de audio locales, todo con una interfaz moderna integrada en el escritorio GNOME/Adwaita.

---

## Características

- **+20 emisoras preconfiguradas** — RNE 1/2/3/4/5, Cadena SER, Cadena 100, Rock FM, Los 40, Europa FM, Cadena Dial, COPE, Onda Cero, Cadena Dial, Megastar FM, Kiss FM, Radio 3, Café del Mar y más.
- **Descubrimiento de emisoras** vía [Radio Browser API](https://www.radio-browser.info/) (búsqueda en tiempo real).
- **Reproductor de archivos de audio** — MP3, FLAC, OGG, M4A, AAC, WAV, OPUS.
- **Visualizador de espectro** en forma de campana de Gauss con gradiente de color.
- **Secciones colapsables por género** con sección de Favoritas.
- **Añadir emisoras manualmente** por URL; exportar/importar favoritos en JSON.
- **Ordenar lista MP3** por nombre, título, artista o álbum.
- **Sleep timer** configurable (15/30/60/90 min).
- **Notificaciones de escritorio** al cambiar la canción en radio.
- **Caché persistente** de la lista MP3 y carpeta de música configurable con escaneo recursivo.
- **Interfaz responsiva** con panel lateral adaptable (`Adw.OverlaySplitView`).
- **Atajos de teclado** — `Espacio` play/pause · `←/→` anterior/siguiente · `M` silenciar.
- Carátulas, metadatos ICY y lectura de etiquetas ID3 / FLAC / MP4.

---

## Instalación (Debian/Ubuntu)

### Opción 1 — Paquete `.deb` (recomendado)

Descarga el último `.deb` desde la sección [Releases](../../releases/latest) e instálalo con:

```bash
sudo apt install ./radioes_1.2.0_all.deb
```

Esto instala todas las dependencias automáticamente. Después busca **RadioES** en el lanzador de aplicaciones o ejecuta `radioes` en la terminal.

Para desinstalar:

```bash
sudo apt remove radioes
```

### Opción 2 — Ejecutar desde el código fuente

**Requisitos previos:**

```bash
sudo apt install \
    python3-gi python3-gi-cairo \
    gir1.2-gtk-4.0 gir1.2-adw-1 \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gir1.2-gdkpixbuf-2.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav
pip3 install --user mutagen requests
```

**Lanzar:**

```bash
git clone https://github.com/fredycibersec/radioes.git
cd radioes
python3 main.py
# o bien:
bash install.sh   # instala iconos y .desktop para el lanzador
bin/radioes
```

---

## Compilar el paquete `.deb`

Requiere `dpkg-dev`:

```bash
sudo apt install dpkg-dev
bash build-deb.sh
# El paquete se genera en dist/radioes_<versión>_all.deb
```

El workflow de CI/CD en `.github/workflows/build-release.yml` construye y publica el `.deb` automáticamente cuando se crea un tag `v*`.

---

## Dependencias del sistema

| Paquete                          | Motivo                         |
|----------------------------------|-------------------------------|
| `python3-gi`, `python3-gi-cairo` | Bindings GTK/GObject           |
| `gir1.2-gtk-4.0`                 | GTK 4                          |
| `gir1.2-adw-1`                   | libadwaita (diseño GNOME HIG)  |
| `gir1.2-gstreamer-1.0`           | GStreamer (reproducción audio) |
| `gstreamer1.0-plugins-*`         | Codecs MP3, AAC, Vorbis, etc.  |
| `python3-requests` *(o pip)*     | Descarga de emisoras/logos     |
| `python3-mutagen` *(opcional)*   | Lectura de metadatos MP3/FLAC  |

---

## Estructura del proyecto

```
radioes/
├── main.py              # Ventana principal, UI GTK4/Adwaita
├── player.py            # Reproductor GStreamer con soporte ICY
├── radio_browser.py     # Cliente API Radio Browser
├── metadata.py          # Lectura de etiquetas ID3/FLAC/MP4
├── bin/radioes          # Lanzador de shell
├── data/
│   ├── icons/           # Iconos PNG (48–512 px)
│   └── spanish_stations.json   # Emisoras preconfiguradas
├── radioes.desktop      # Entrada del lanzador de aplicaciones
├── build-deb.sh         # Script para generar el .deb
├── install.sh           # Instalador para ejecutar desde fuente
└── dist/                # Paquetes .deb generados
```

---

## Versiones

| Versión | Cambios destacados |
|---------|-------------------|
| 1.2.0   | Visualizador de espectro en campana, modo shuffle/repeat/secuencial, sleep timer, notificaciones, mute por teclado, ordenación MP3 |
| 1.1.0   | Soporte `Adw.OverlaySplitView`, icono 512px, escaneo recursivo de carpetas |
| 1.0.0   | Versión inicial: radio + MP3 + Radio Browser + favoritos |

---

## Compatibilidad

| Distribución             | Estado     |
|--------------------------|-----------|
| Ubuntu 24.04 LTS         | ✅ Probado |
| Ubuntu 22.04 LTS         | ✅ Probado |
| Linux Mint 21+           | ✅ Probado |
| Debian 12 (Bookworm)     | ✅ Probado |
| Pop!_OS 22.04            | ✅ Compatible |
| Otras distros Debian/Ubuntu | ⚠️ Sin probar |

> **Nota:** requiere GTK 4.6+ y libadwaita 1.x. En distros más antiguas (Ubuntu 20.04, Debian 11) la versión de GTK del sistema es insuficiente.

---

## Licencia

MIT © 2026 [SaruMan](mailto:alfredo.ramirez@nologin.es)
