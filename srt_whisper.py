# srt_whisper.py
# -*- coding: utf-8 -*-
"""
Transcribe un audio a SRT con Whisper/OpenAI.
- Si el archivo supera el límite de 25MB, lo recomprime a MP3 (mono, 48 kbps) para la transcripción.
- Reintenta automáticamente con menor bitrate si persiste un 413.
- Puede leer el tema desde config.json (--tema-from-config) y escribir el SRT en outputs/<slug>/.
"""

from __future__ import annotations

import argparse
import os
import sys
import json
import tempfile
import shutil
from pathlib import Path

# SDK compat: intenta usar `from openai import OpenAI`, cae a `openai.OpenAI` si no existe.
try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    import openai  # type: ignore
    OpenAI = openai.OpenAI

from pydub import AudioSegment  # requiere ffmpeg instalado

# ----- Utilidades -----

def slugify(text: str) -> str:
    import re
    t = text.lower()
    t = re.sub(r"[^a-z0-9áéíóúüñ\s-]", "", t)
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"-+", "-", t).strip("-")
    return t

def load_config(config_path="config.json") -> dict:
    if not os.path.exists(config_path):
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        try:
            return json.load(f) or {}
        except Exception:
            return {}

def ensure_dir(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def human_size(b: int) -> str:
    for unit in ["B","KB","MB","GB"]:
        if b < 1024.0:
            return f"{b:.1f}{unit}"
        b /= 1024.0
    return f"{b:.1f}TB"

def compress_to_mp3(in_path: Path, out_path: Path, bitrate: str = "48k") -> Path:
    """Convierte a MP3 mono con el bitrate dado (requiere ffmpeg)."""
    audio = AudioSegment.from_file(in_path)
    audio = audio.set_channels(1)
    # si el sample rate es muy alto, bájalo a 16000 para reducir tamaño y mantener timestamps estables
    if audio.frame_rate > 16000:
        audio = audio.set_frame_rate(16000)
    ensure_dir(out_path)
    audio.export(out_path, format="mp3", bitrate=bitrate)
    return out_path

def transcribe_file(client: OpenAI, path: Path, model: str = "whisper-1", language: str = "es") -> str:
    """Devuelve el contenido SRT (str)."""
    with open(path, "rb") as f:
        tr = client.audio.transcriptions.create(
            model=model,
            file=f,
            response_format="srt",
            language=language
        )
    # SDK devuelve un objeto que se imprime como texto; lo convertimos a str explícitamente
    return str(tr)

# ----- Programa principal -----

def main():
    parser = argparse.ArgumentParser(description="Transcribe audio a SRT con Whisper/OpenAI.")
    parser.add_argument("--audio", type=str, help="Ruta del audio a transcribir.")
    parser.add_argument("--out", type=str, help="Ruta de salida .srt")
    parser.add_argument("--tema-from-config", action="store_true", help="Leer tema activo desde config.json")
    parser.add_argument("--model", type=str, default="whisper-1", help="Modelo de transcripción (por defecto: whisper-1)")
    parser.add_argument("--language", type=str, default="es", help="Idioma (por defecto: es)")
    args = parser.parse_args()

    # Resolución de tema desde config.json si se pide
    if args.tema_from_config:
        cfg = load_config("config.json")
        tema = cfg.get("tema") or "podcast"
        slug = slugify(tema)
        if not args.audio:
            args.audio = f"outputs/{slug}/podcast_{slug}.wav"
        if not args.out:
            args.out = f"outputs/{slug}/podcast_{slug}.srt"

    if not args.audio or not args.out:
        print("❌ Debes indicar --audio y --out, o usar --tema-from-config.", file=sys.stderr)
        sys.exit(2)

    audio_path = Path(args.audio)
    out_path = Path(args.out)

    if not audio_path.exists():
        print(f"❌ No existe el audio: {audio_path}", file=sys.stderr)
        sys.exit(1)

    ensure_dir(out_path)

    # Cliente OpenAI
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_APIKEY")
    if not api_key:
        print("❌ Falta OPENAI_API_KEY en el entorno.", file=sys.stderr)
        sys.exit(1)
    client = OpenAI(api_key=api_key)

    # Si pesa > 24 MB, recomprime a MP3 48k mono (y reintenta si hay 413)
    MAX_BYTES = 25 * 1024 * 1024  # 25MB
    safety_margin = int(0.96 * MAX_BYTES)      # ~24MB para ir seguros
    size = audio_path.stat().st_size
    work_file = audio_path

    tmpdir = None
    try:
        if size > safety_margin:
            print(f"ℹ️ Audio original {human_size(size)} supera el límite. Comprimiendo a MP3 mono 48k…")
            tmpdir = Path(tempfile.mkdtemp(prefix="srt_whisper_"))
            work_file = tmpdir / (audio_path.stem + "_transcribe.mp3")
            compress_to_mp3(audio_path, work_file, bitrate="48k")
            print(f"   → Comprimido: {human_size(work_file.stat().st_size)}")

        # Primer intento
        try:
            print("🗣️  Transcribiendo con Whisper…")
            srt_text = transcribe_file(client, work_file, model=args.model, language=args.language)
        except Exception as e:
            # Si es 413 o similar, bajar bitrate y reintentar
            msg = str(e)
            if "413" in msg or "Maximum content size" in msg or "size limit" in msg:
                print("⚠️  413 recibido. Reintentando con bitrate menor (32k)…")
                if tmpdir is None:
                    tmpdir = Path(tempfile.mkdtemp(prefix="srt_whisper_"))
                work_file = tmpdir / (audio_path.stem + "_transcribe_32k.mp3")
                compress_to_mp3(audio_path, work_file, bitrate="32k")
                srt_text = transcribe_file(client, work_file, model=args.model, language=args.language)
            else:
                raise

        out_path.write_text(srt_text, encoding="utf-8")
        print(f"✅ SRT generado: {out_path}")

    finally:
        # Limpieza temporal
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()