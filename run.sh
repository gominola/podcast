#!/usr/bin/env bash
set -euo pipefail

# Pipeline completo: guion -> audio -> imágenes -> video
# Requisitos: python, ffmpeg

echo "==> Generando guion"
python podcast.py

echo "==> Generando audio TTS"
python podcast.py --audio

echo "==> Descargando imágenes del tema"
python images.py --tema-from-config --n 20

# Detecta el slug del tema automáticamente
THEME_SLUG=$(python - <<'PY'
import json, re
cfg=json.load(open('config.json'))
t=cfg.get('tema','El universo').lower()
t=re.sub(r'[^a-z0-9áéíóúüñ\s-]','',t)
t=re.sub(r'\s+','-',t).strip('-')
print(t)
PY
)

echo "==> Generando vídeo"
python podcast.py --video --images "imagenes/${THEME_SLUG}"

echo "==> Listo. Busca los archivos podcast_${THEME_SLUG}.txt/.wav/.srt/.mp4"