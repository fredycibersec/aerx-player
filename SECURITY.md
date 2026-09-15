# Política de seguridad

## Versiones soportadas

ÆRx Player está en fase **0.99-beta2**, última beta antes del lanzamiento estable **1.0**. Solo la última versión publicada recibe correcciones de seguridad.

| Versión      | Soportada |
|--------------|-----------|
| 0.99-beta2   | ✅ |
| < 0.99-beta2 (RadioES 1.x, 0.99-beta) | ❌ |

## Reportar una vulnerabilidad

Si encuentras un problema de seguridad, **por favor no abras un issue público**. En su lugar, usa una de estas vías:

1. [GitHub Security Advisories](../../security/advisories/new) del repositorio (recomendado, permite divulgación privada).
2. Correo a [sarumanthegrey@proton.me](mailto:sarumanthegrey@proton.me) con el mayor detalle posible: versión afectada, distro, pasos para reproducirlo y, si aplica, impacto potencial.

Este es un proyecto de código abierto mantenido en tiempo libre por una sola persona, así que no hay un SLA formal, pero se hará lo posible por:

- Confirmar la recepción en un plazo razonable.
- Valorar el impacto y, si procede, publicar una versión corregida.
- Acreditar el descubrimiento (si el reportante lo desea) en las notas de la release.

## Alcance

ÆRx Player es una aplicación de escritorio local (GTK4/Adwaita) sin backend propio ni cuentas de usuario. Los puntos de superficie más relevantes son:

- Descarga y reproducción de streams de radio y episodios de podcast (URLs remotas vía GStreamer/`requests`).
- Escritura de etiquetas ID3/FLAC/MP4 y carátulas en ficheros locales.
- Peticiones a APIs de terceros: [Radio Browser](https://www.radio-browser.info/), [MusicBrainz](https://musicbrainz.org/)/[Cover Art Archive](https://coverartarchive.org/), [iTunes Search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/) y comprobación de actualizaciones contra GitHub Releases.
- Ficheros de configuración y caché en `~/.local/share/aerx-player/`.

Quedan fuera de alcance los problemas que requieran acceso físico a la máquina del usuario o que dependan de vulnerabilidades en GTK4/libadwaita/GStreamer o en las APIs de terceros listadas arriba (repórtalas a sus proyectos correspondientes).
