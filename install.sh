#!/usr/bin/env bash
# RadioES – instala dependencias del sistema, iconos y entrada del lanzador
set -e

APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BINARY="$APP_DIR/bin/radioes"

# ── 1. Dependencias del sistema ──────────────────────────────────────────────
echo "==> Instalando dependencias del sistema…"
sudo apt-get update -qq
sudo apt-get install -y \
    python3-gi python3-gi-cairo \
    gir1.2-gtk-4.0 gir1.2-adw-1 \
    gir1.2-gst-plugins-base-1.0 gir1.2-gstreamer-1.0 \
    gir1.2-gdkpixbuf-2.0 gir1.2-rsvg-2.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav libgstreamer1.0-0 \
    python3-pip

# ── 2. Dependencias Python ───────────────────────────────────────────────────
echo "==> Instalando dependencias Python…"
pip3 install --user --break-system-packages mutagen requests 2>/dev/null \
  || pip3 install --user mutagen requests

# ── 3. Binario ejecutable ────────────────────────────────────────────────────
chmod +x "$BINARY"

# ── 4. Iconos (hicolor icon theme) ──────────────────────────────────────────
echo "==> Instalando iconos…"
ICON_SRC="$APP_DIR/data/icons"
ICON_DST="$HOME/.local/share/icons/hicolor"

install -Dm644 "$ICON_SRC/radioes.svg" \
    "$ICON_DST/scalable/apps/radioes.svg"

for SIZE in 48 64 128 256; do
    install -Dm644 "$ICON_SRC/radioes-${SIZE}.png" \
        "$ICON_DST/${SIZE}x${SIZE}/apps/radioes.png"
done

# Refresh icon cache (silent if gtk-update-icon-cache absent)
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# ── 5. Entrada del lanzador (.desktop) ───────────────────────────────────────
echo "==> Instalando entrada del lanzador…"
DESK_DIR="$HOME/.local/share/applications"
mkdir -p "$DESK_DIR"

sed "s|RADIOES_BIN|$BINARY|g" "$APP_DIR/radioes.desktop" \
    > "$DESK_DIR/radioes.desktop"

# Refresh desktop database
update-desktop-database "$DESK_DIR" 2>/dev/null || true

# ── 6. Enlace simbólico opcional en ~/.local/bin ─────────────────────────────
BIN_LOCAL="$HOME/.local/bin"
mkdir -p "$BIN_LOCAL"
ln -sf "$BINARY" "$BIN_LOCAL/radioes"

echo ""
echo "✓ RadioES instalado."
echo "  • Lanzar desde terminal:  radioes"
echo "  • Lanzar directo:         $BINARY"
echo "  • Buscarlo en el lanzador de apps como: RadioES"
