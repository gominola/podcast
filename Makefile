# Makefile para pipeline de podcast
USING_PY    = python

# Ajusta estos 2 si cambias config.json (output_slug / output_basename)
SLUG        = la-evolucion
BASENAME    = la-evolucion
OUTDIR      = outputs/$(SLUG)

TXT         = $(OUTDIR)/$(BASENAME).txt
AUDIO       = $(OUTDIR)/$(BASENAME).wav
SRT         = $(OUTDIR)/$(BASENAME).srt
MP4_FAST    = $(OUTDIR)/$(BASENAME)_fast.mp4

.PHONY: all guion audio srt video clean debug

all: guion audio srt video

# Generar guion (usa podcast.py simplificado)
guion: $(TXT)

$(TXT):
	@echo "📝 Generando guion…"
	$(USING_PY) podcast.py

# Generar audio (usa audio.py con config.json)
audio: $(AUDIO)

$(AUDIO): $(TXT)
	@echo "🔊 Generando audio…"
	$(USING_PY) audio.py --tema-from-config
	@echo "📄 Listando OUTDIR tras audio:"
	@ls -l $(OUTDIR) || true
	@echo "✅ Audio: $(AUDIO)"

# Generar subtítulos (Whisper)
srt: $(SRT)

$(SRT): $(AUDIO)
	@echo "🗣️  Generando subtítulos (Whisper)…"
	$(USING_PY) srt_whisper.py --tema-from-config
	@echo "📄 Listando OUTDIR tras SRT:"
	@ls -l $(OUTDIR) || true
	@echo "✅ SRT generado: $(SRT)"

# Generar vídeo rápido (con ASS interno de video.py)
video: $(MP4_FAST)

$(MP4_FAST): $(AUDIO) $(SRT)
	@echo "🎬 Generando vídeo (colores por orador, ASS)…"
	$(USING_PY) video.py \
		--tema-from-config \
		--image "assets/studio_full.jpg" \
		--out "$(MP4_FAST)"
	@echo "✅ Vídeo listo: $(MP4_FAST)"

clean:
	rm -rf outputs/*

debug:
	@echo "USING_PY    = $(USING_PY)"
	@echo "SLUG        = $(SLUG)"
	@echo "BASENAME    = $(BASENAME)"
	@echo "OUTDIR      = $(OUTDIR)"
	@echo "TXT         = $(TXT)"
	@echo "AUDIO       = $(AUDIO)"
	@echo "SRT         = $(SRT)"
	@echo "MP4_FAST    = $(MP4_FAST)"