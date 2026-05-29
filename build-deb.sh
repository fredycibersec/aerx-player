#!/usr/bin/env bash
# RadioES – construye el paquete .deb
# Uso: bash build-deb.sh
# Requiere: dpkg-deb (paquete dpkg-dev)
set -euo pipefail

APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

PKG_NAME="radioes"
PKG_VERSION="1.2.0"
PKG_ARCH="all"
PKG_FILE="${PKG_NAME}_${PKG_VERSION}_${PKG_ARCH}.deb"

STAGE="${APP_DIR}/dist/.stage/${PKG_NAME}_${PKG_VERSION}"
INSTALL_DIR="usr/share/radioes"

# ── Limpieza previa ──────────────────────────────────────────────────────────
rm -rf "$STAGE"

# ── Árbol de instalación ─────────────────────────────────────────────────────
echo "==> Preparando árbol de instalación…"

install -d \
    "$STAGE/DEBIAN" \
    "$STAGE/$INSTALL_DIR/data" \
    "$STAGE/usr/bin" \
    "$STAGE/usr/share/applications" \
    "$STAGE/usr/share/icons/hicolor/512x512/apps" \
    "$STAGE/usr/share/icons/hicolor/256x256/apps" \
    "$STAGE/usr/share/icons/hicolor/128x128/apps" \
    "$STAGE/usr/share/icons/hicolor/64x64/apps"  \
    "$STAGE/usr/share/icons/hicolor/48x48/apps"  \
    "$STAGE/usr/share/doc/$PKG_NAME"

# Fuentes Python
for f in main.py player.py radio_browser.py metadata.py; do
    install -m644 "$APP_DIR/$f" "$STAGE/$INSTALL_DIR/$f"
done

# Datos
install -m644 "$APP_DIR/data/spanish_stations.json" \
              "$STAGE/$INSTALL_DIR/data/spanish_stations.json"

# Lanzador en /usr/bin
cat > "$STAGE/usr/bin/radioes" << 'LAUNCHER'
#!/usr/bin/env bash
exec python3 /usr/share/radioes/main.py "$@"
LAUNCHER
chmod 755 "$STAGE/usr/bin/radioes"

# Entrada del escritorio
sed "s|RADIOES_BIN|/usr/bin/radioes|g" "$APP_DIR/radioes.desktop" \
    > "$STAGE/usr/share/applications/radioes.desktop"
chmod 644 "$STAGE/usr/share/applications/radioes.desktop"

# Iconos
for SIZE in 48 64 128 256 512; do
    install -m644 "$APP_DIR/data/icons/radioes-${SIZE}.png" \
        "$STAGE/usr/share/icons/hicolor/${SIZE}x${SIZE}/apps/radioes.png"
done

# Copyright mínimo
cat > "$STAGE/usr/share/doc/$PKG_NAME/copyright" << 'COPYRIGHT'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: radioes
Upstream-Contact: alfredo.ramirez@nologin.es

Files: *
Copyright: 2026 Alfredo Ramirez <alfredo.ramirez@nologin.es>
License: MIT
COPYRIGHT

# ── DEBIAN/control ───────────────────────────────────────────────────────────
INSTALLED_KB=$(du -sk "$STAGE" | cut -f1)

cat > "$STAGE/DEBIAN/control" << EOF
Package: ${PKG_NAME}
Version: ${PKG_VERSION}
Architecture: ${PKG_ARCH}
Maintainer: Alfredo Ramirez <alfredo.ramirez@nologin.es>
Installed-Size: ${INSTALLED_KB}
Depends: python3 (>= 3.10),
 python3-gi,
 python3-gi-cairo,
 gir1.2-gtk-4.0,
 gir1.2-adw-1,
 gir1.2-gstreamer-1.0,
 gir1.2-gst-plugins-base-1.0,
 gir1.2-gdkpixbuf-2.0,
 gstreamer1.0-plugins-base,
 gstreamer1.0-plugins-good,
 python3-requests
Recommends: gstreamer1.0-plugins-bad,
 gstreamer1.0-plugins-ugly,
 gstreamer1.0-libav,
 python3-mutagen
Section: sound
Priority: optional
Description: Radio española online y reproductor MP3
 Aplicación GTK4/Adwaita para escuchar emisoras de radio españolas
 en línea y reproducir archivos de audio locales (MP3, FLAC, OGG, AAC).
 .
 Incluye más de 20 emisoras preconfiguradas (RNE, SER, Cadena 100,
 Rock FM, Los 40, Cadena Dial, Café del Mar…) y descubrimiento de
 nuevas emisoras mediante la API de Radio Browser.
 .
 Características:
  - Lista de emisoras por géneros colapsables con sección de Favoritas
  - Añadir emisoras manualmente; exportar e importar favoritos en JSON
  - Sleep timer configurable (15/30/60/90 min)
  - Notificaciones de escritorio al cambiar el tema en radio
  - Atajos de teclado: Espacio=play/pause, ←/→=anterior/siguiente, M=mute
  - Ordenar lista MP3 por nombre, título, artista o álbum
  - Visualizador de espectro tipo campana de Gauss con gradiente de color
  - Caché persistente de lista MP3 y carpeta configurable con escaneo recursivo
  - Interfaz responsiva con panel lateral adaptable (Adw.OverlaySplitView)
  - Carátulas, metadatos ICY y lectura de etiquetas ID3/FLAC/MP4
EOF

# ── DEBIAN/postinst ───────────────────────────────────────────────────────────
cat > "$STAGE/DEBIAN/postinst" << 'POSTINST'
#!/bin/bash
set -e
case "$1" in
    configure)
        gtk-update-icon-cache    -f -t /usr/share/icons/hicolor 2>/dev/null || true
        update-desktop-database  /usr/share/applications         2>/dev/null || true
        ;;
esac
POSTINST
chmod 755 "$STAGE/DEBIAN/postinst"

# ── DEBIAN/postrm ─────────────────────────────────────────────────────────────
cat > "$STAGE/DEBIAN/postrm" << 'POSTRM'
#!/bin/bash
set -e
case "$1" in
    remove|purge)
        gtk-update-icon-cache    -f -t /usr/share/icons/hicolor 2>/dev/null || true
        update-desktop-database  /usr/share/applications         2>/dev/null || true
        ;;
esac
POSTRM
chmod 755 "$STAGE/DEBIAN/postrm"

# ── md5sums ───────────────────────────────────────────────────────────────────
(cd "$STAGE" && find . -type f ! -path './DEBIAN/*' -exec md5sum {} \; \
    | sed 's|\./||' > DEBIAN/md5sums)

# ── Construir .deb ────────────────────────────────────────────────────────────
echo "==> Construyendo ${PKG_FILE}…"
mkdir -p "$APP_DIR/dist"
dpkg-deb --build --root-owner-group "$STAGE" "$APP_DIR/dist/$PKG_FILE"

echo ""
echo "✓  Paquete listo: dist/${PKG_FILE}"
echo ""
echo "   Instalar:      sudo apt install ./dist/${PKG_FILE}"
echo "   Desinstalar:   sudo apt remove ${PKG_NAME}"
echo "   Información:   dpkg -I dist/${PKG_FILE}"
