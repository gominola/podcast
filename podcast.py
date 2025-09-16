# -*- coding: utf-8 -*-
import os
import re
import sys
import json
import argparse

from dotenv import load_dotenv
load_dotenv(".env")

from guion import generar_podcast, slugify
from audio import texto_a_audio, reproducir_podcast

CONFIG_PATH = "config.json"

def _leer_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _extrae_path_si_es_msg(s: str) -> str:
    m = re.match(r"^Archivo guardado:\s*(.+)$", s.strip())
    return m.group(1).strip() if m else ""

def _lee_archivo(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def _srt_desde_transcript(transcript_text: str, tema: str, outdir: str) -> str:
    import re as _re
    def fmt_ts(seg: float) -> str:
        ms = int((seg - int(seg)) * 1000)
        s = int(seg) % 60
        m = (int(seg) // 60) % 60
        h = int(seg) // 3600
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    items = []
    for raw in transcript_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _re.match(r"^\[?COLD OPEN\]?\s*:?\s*(.+)$", line, flags=_re.IGNORECASE)
        if m:
            items.append(("Narrador", m.group(1).strip())); continue
        m2 = _re.match(r"^([^:]+)\s*:\s*(.+)$", line)
        if m2:
            items.append((m2.group(1).strip(), m2.group(2).strip()))

    t = 0.0
    idx = 1
    bloques = []
    for spk, text in items:
        palabras = max(1, len(_re.findall(r"\w+", text)))
        dur = max(2.0, palabras / 2.666)  # ~160 wpm
        start, end = t, t + dur
        bloques.append(f"{idx}\n{fmt_ts(start)} --> {fmt_ts(end)}\n{spk}: {text}\n")
        idx += 1
        t = end + 0.12

    srt_path = os.path.join(outdir, f"podcast_{slugify(tema)}.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(bloques) + "\n")
    return srt_path

def main():
    parser = argparse.ArgumentParser(description="Generador de podcast")
    parser.add_argument("--audio", action="store_true", help="Generar audio TTS")
    parser.add_argument("--play", action="store_true", help="Reproducir el audio (implica --audio)")
    parser.add_argument("--video", action="store_true", help="Generar video MP4 (imagen estática + subtítulos)")
    parser.add_argument("--reuse", action="store_true", help="Reutiliza el guion si existe")
    args = parser.parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("❌ Falta OPENAI_API_KEY en .env", file=sys.stderr)
        sys.exit(1)

    cfg = _leer_config()
    tema = cfg.get("tema", "El universo")
    slug = slugify(tema)

    # Salidas por tema
    outdir = os.path.join("outputs", slug)
    os.makedirs(outdir, exist_ok=True)

    # Assets globales
    assets_dir = "assets"
    os.makedirs(assets_dir, exist_ok=True)

    txt_path = os.path.join(outdir, f"podcast_{slug}.txt")
    audio_path = os.path.join(outdir, f"podcast_{slug}.wav")
    srt_path = os.path.join(outdir, f"podcast_{slug}.srt")
    out_video = os.path.join(outdir, f"podcast_{slug}.mp4")

    print(f"📝 Generando guion para: {tema}\n")

    resultado = ""
    transcript_text = ""

    if args.reuse and os.path.exists(txt_path):
        print(f"ℹ️ --reuse activo. Reutilizo {txt_path}\n")
        with open(txt_path, "r", encoding="utf-8") as f:
            transcript_text = f.read()
    else:
        try:
            resultado = generar_podcast(api_key)
        except Exception as e:
            print(f"💥 Error al generar el guion: {e}", file=sys.stderr)
            sys.exit(1)

    if not transcript_text:
        saved_path = _extrae_path_si_es_msg(resultado)
        if saved_path:
            if saved_path.lower().endswith((".txt", ".md", ".srt")):
                if saved_path.lower().endswith(".srt"):
                    srt = _lee_archivo(saved_path)
                    bloques = re.split(r"\n\s*\n", srt.strip())
                    lines = []
                    for b in bloques:
                        lines_b = [l for l in b.splitlines() if l.strip()]
                        if len(lines_b) < 3:
                            continue
                        contenido = lines_b[2:]
                        lines.append(" ".join(contenido).strip())
                    transcript_text = "\n".join(lines)
                else:
                    transcript_text = _lee_archivo(saved_path)
            else:
                transcript_text = resultado
        else:
            transcript_text = resultado

    if transcript_text and not os.path.exists(txt_path):
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(transcript_text)

    print("\n────────────  GUION GENERADO  ────────────\n")
    print(transcript_text[:3000] + ("\n...\n" if len(transcript_text) > 3000 else ""))

    # Audio
    need_audio = args.audio or args.play
    if args.video and not os.path.exists(audio_path):
        print(f"ℹ️ No existe {audio_path} y has pedido --video. Generaré el audio primero.")
        need_audio = True

    if need_audio:
        try:
            print("\n🔊 Convirtiendo texto a audio (voces diferenciadas)...\n")
            audio_path = texto_a_audio(transcript_text, api_key, out_path=audio_path)
            print(f"✅ Audio: {audio_path}")
        except Exception as e:
            print(f"💥 Error durante TTS: {e}", file=sys.stderr)
            if args.play or args.video:
                sys.exit(1)
    else:
        if os.path.exists(audio_path):
            print(f"ℹ️ Usando audio existente: {audio_path}")
        else:
            print("⚠️ No hay audio. Ejecuta con --audio si lo quieres.")

    if args.play and os.path.exists(audio_path):
        try:
            print("\n🎙️ Reproduciendo...\n")
            reproducir_podcast(audio_path)
        except Exception as e:
            print(f"⚠️ No se pudo reproducir: {e}", file=sys.stderr)

    # Vídeo (imagen estática)
    if args.video:
        try:
            from video import generar_video
        except Exception as e:
            print(f"💥 No se pudo cargar módulo de vídeo: {e}", file=sys.stderr)
            sys.exit(1)

        if not os.path.exists(srt_path):
            try:
                print("🧩 No hay SRT. Generando estimado desde transcript…")
                srt_path = _srt_desde_transcript(transcript_text, tema, outdir)
            except Exception as e:
                print(f"💥 Error al generar SRT: {e}", file=sys.stderr)
                sys.exit(1)

        try:
            print("\n🎬 Creando vídeo (imagen estática + subtítulos)…\n")
            generar_video(
                audio_path=audio_path,
                out_path=out_video,
                srt_path=srt_path,
                transcript_text=transcript_text,
                assets_dir=assets_dir,  # cover + estudio estático
            )
            print(f"✅ Vídeo: {out_video}")
        except Exception as e:
            print(f"💥 Error al generar el vídeo: {e}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    main()