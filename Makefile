PY := python
THEME_SLUG := $(shell $(PY) -c "import json, re; t=json.load(open('config.json')).get('tema','El universo'); t=t.lower(); t=re.sub(r'[^a-z0-9áéíóúüñ\\s-]','',t); t=re.sub(r'\\s+','-',t).strip('-'); print(t)")
OUTDIR      := outputs/$(THEME_SLUG)
ASSETS_DIR  := assets
AUDIO       := $(OUTDIR)/podcast_$(THEME_SLUG).wav
SRT         := $(OUTDIR)/podcast_$(THEME_SLUG).srt
MP4_FAST    := $(OUTDIR)/podcast_$(THEME_SLUG)_fast.mp4

.PHONY: all guion audio srt video_fast

all: guion audio srt video

guion:
	@echo "Generando guion…"
	$(PY) podcast.py

audio:
	@echo "Generando audio…"
	$(PY) podcast.py --reuse --audio

srt:
	$(PY) srt_whisper.py --tema-from-config

video:
	@echo "Generando vídeo rápido (ffmpeg + subtítulos embebidos)…"
	@test -f "$(SRT)" || (echo "No existe $(SRT). Ejecuta 'make srt' primero." && exit 1)
	@test -f "$(ASSETS_DIR)/studio_full.jpg" || (echo "Falta assets/studio_full.jpg" && exit 1)
	# subtítulos hard-burn con estilo (fuente y margen inferior)
	ffmpeg -y -loop 1 -framerate 30 -i "$(ASSETS_DIR)/studio_full.jpg" -i "$(AUDIO)" \
	-vf "subtitles='$(SRT)':force_style='FontName=Arial,FontSize=28,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=1.5,Shadow=0,MarginV=60'" \
	-shortest -c:v libx264 -preset veryfast -tune stillimage -crf 18 -c:a aac -b:a 192k -pix_fmt yuv420p "$(MP4_FAST)"
	@echo "✅ Vídeo rápido listo: $(MP4_FAST)"