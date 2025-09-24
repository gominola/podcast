# srt_whisper.py
# -*- coding: utf-8 -*-
"""
Genera subtítulos SRT desde el WAV final usando Whisper (u otra herramienta).
- Lee slug/basename desde config.json cuando se usa --tema-from-config.
- Trabaja con paths ABSOLUTOS para evitar confusiones de cwd.
- Reintenta localizar el WAV unos milisegundos (por seguridad en algunos FS).
- Mensajes de depuración claros.

Asume:
  outputs/<slug>/<basename>.wav  -> entrada
  outputs/<slug>/<basename>.srt  -> salida
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def build_paths_from_config(cfg_path: Path) -> tuple[Path, Path, dict]:
    cfg = read_json(cfg_path)
    slug = cfg.get("output_slug") or cfg.get("tema_slug") or "podcast"
    basename = cfg.get("output_basename") or slug
    outdir = Path.cwd() / "outputs" / slug
    audio = outdir / f"{basename}.wav"
    srt = outdir / f"{basename}.srt"
    return audio, srt, cfg

def ensure_audio_exists(path: Path, retries: int = 15, delay: float = 0.1) -> bool:
    # Algunos FS/macOS pueden tardar una fracción de segundo en reflejar ficheros grandes recién escritos
    for _ in range(retries):
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(delay)
    return path.exists()

def fmt_ts(seconds: float) -> str:
    # Formatea segundos en HH:MM:SS,mmm para SRT
    millis = int(round(seconds * 1000))
    h = millis // 3600000
    m = (millis % 3600000) // 60000
    s = (millis % 60000) // 1000
    ms = millis % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def strip_meta(text: str) -> str:
    # Elimina etiquetas [etiquetas], emojis y puntuación inicial excesiva
    # Eliminar etiquetas [..]
    text = re.sub(r"\[[^\]]*\]", "", text)
    # Quitar emojis (caracteres fuera de ASCII básico, aproximado)
    text = re.sub(r"[^\x00-\x7F]+", "", text)
    # Quitar puntuación al inicio y espacios
    text = text.lstrip(" \t\n\r\f\v-–—:;,.!?")
    return text.strip()

def write_srt(segments: list[dict], out_path: Path) -> None:
    lines = []
    for i, seg in enumerate(segments, 1):
        start = fmt_ts(seg["start"])
        end = fmt_ts(seg["end"])
        text = seg["text"].strip()
        lines.append(f"{i}")
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")

def audio_duration_via_ffprobe(audio_path: Path) -> float | None:
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path.as_posix()]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        duration_str = result.stdout.strip()
        duration = float(duration_str)
        return duration
    except Exception:
        return None

def transcribe_with_python_whisper(audio_path: Path, out_srt: Path, cfg: dict) -> bool:
    try:
        import whisper
    except ImportError:
        print("⚠️ Paquete 'whisper' no disponible, intentando fallback CLI...", file=sys.stderr)
        return False
    model_name = cfg.get("whisper_model", "base")
    idioma = cfg.get("idioma", None)
    language = "es" if idioma == "es" else None
    print(f"[WHISPER-PY] Modelo: {model_name}, idioma: {language or 'auto'}")
    try:
        model = whisper.load_model(model_name)
    except Exception as e:
        print(f"💥 Error cargando modelo Whisper: {e}", file=sys.stderr)
        return False
    print(f"🗣️ Transcribiendo audio con Whisper Python API: {audio_path}")
    try:
        options = {}
        if language:
            options["language"] = language
        result = model.transcribe(audio_path.as_posix(), **options)
    except Exception as e:
        print(f"💥 Error durante transcripción Whisper: {e}", file=sys.stderr)
        return False
    segments = []
    if "segments" in result and result["segments"]:
        for seg in result["segments"]:
            segments.append({
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"].strip()
            })
    else:
        # fallback: whole text as one segment
        segments.append({
            "start": 0.0,
            "end": result.get("duration", 0.0),
            "text": result.get("text", "").strip()
        })
    write_srt(segments, out_srt)
    return True

def transcribe_with_cli_whisper(audio_path: Path, out_srt: Path, cfg: dict) -> bool:
    whisper_cmd = shutil.which("whisper")
    if whisper_cmd is None:
        print("⚠️ Ejecutable 'whisper' no encontrado en PATH para fallback CLI.", file=sys.stderr)
        return False
    model_name = cfg.get("whisper_model", "base")
    idioma = cfg.get("idioma", None)
    language = "es" if idioma == "es" else None
    print(f"[WHISPER-CLI] Ejecutando 'whisper' modelo={model_name} idioma={language or 'auto'}")
    outdir = out_srt.parent
    outdir.mkdir(parents=True, exist_ok=True)
    args = [
        whisper_cmd,
        audio_path.as_posix(),
        "--model", model_name,
        "--task", "transcribe",
        "--output_dir", outdir.as_posix(),
        "--output_format", "srt",
    ]
    if language:
        args.extend(["--language", language])
    try:
        subprocess.run(args, check=True)
    except subprocess.CalledProcessError as e:
        print(f"💥 Error ejecutando whisper CLI: {e}", file=sys.stderr)
        return False
    # El archivo SRT se genera con el mismo basename en outdir
    generated_srt = outdir / (audio_path.stem + ".srt")
    if generated_srt.exists():
        # Mover o copiar al destino exacto si diferente
        if generated_srt.resolve() != out_srt.resolve():
            generated_srt.replace(out_srt)
        return True
    else:
        print(f"❌ No se generó archivo SRT esperado: {generated_srt}", file=sys.stderr)
        return False

def fallback_from_txt(audio_path: Path, out_srt: Path, cfg: dict) -> bool:
    slug = cfg.get("output_slug") or cfg.get("tema_slug") or "podcast"
    basename = cfg.get("output_basename") or slug
    txt_path = audio_path.parent / f"{basename}.txt"
    if not txt_path.exists():
        print(f"❌ No existe archivo TXT para fallback: {txt_path}", file=sys.stderr)
        return False
    print(f"⚠️ Usando fallback TXT para generar SRT: {txt_path}")
    text = txt_path.read_text(encoding="utf-8").strip()
    if not text:
        print("❌ Archivo TXT está vacío.", file=sys.stderr)
        return False

    # Intentar duración con ffprobe
    duration = audio_duration_via_ffprobe(audio_path)
    if duration is None:
        # Estimación: 150 palabras/minuto = 2.5 palabras/segundo
        words = len(text.split())
        duration = max(words / 2.5, 1.0)
        print(f"⚠️ Duración estimada: {duration:.1f}s basada en conteo de palabras.")

    # Dividir texto en bloques por líneas no vacías
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Limpiar líneas y quitar etiquetas/emojis
    clean_lines = [strip_meta(line) for line in raw_lines if strip_meta(line)]

    if not clean_lines:
        print("❌ No hay líneas limpias para generar SRT.", file=sys.stderr)
        return False

    n = len(clean_lines)
    seg_duration = duration / n

    segments = []
    for i, line in enumerate(clean_lines):
        start = i * seg_duration
        end = start + seg_duration
        segments.append({
            "start": start,
            "end": end,
            "text": line
        })
    write_srt(segments, out_srt)
    print(f"✅ SRT generado desde TXT fallback: {out_srt}")
    return True

import shutil

def main():
    ap = argparse.ArgumentParser(description="Genera SRT desde WAV usando Whisper.")
    ap.add_argument("--tema-from-config", action="store_true", help="Usar config.json para inferir rutas")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--audio", default=None, help="Ruta explícita del WAV (anula config)")
    ap.add_argument("--out", default=None, help="Ruta explícita del SRT (anula config)")
    args = ap.parse_args()

    if args.tema_from_config:
        audio_path, srt_path, cfg = build_paths_from_config(Path(args.config))
    else:
        if not args.audio or not args.out:
            print("❌ Debes indicar --audio y --out si no usas --tema-from-config", file=sys.stderr)
            sys.exit(1)
        audio_path = Path(args.audio)
        srt_path = Path(args.out)
        cfg = {}

    audio_path = audio_path.resolve()
    srt_path = srt_path.resolve()

    print(f"[WHISPER] audio={audio_path} | out={srt_path}")

    if not ensure_audio_exists(audio_path):
        try:
            parent = audio_path.parent
            print(f"📂 Contenido de {parent}:", file=sys.stderr)
            for p in sorted(parent.glob("*")):
                try:
                    print(f" - {p.name}  ({p.stat().st_size} bytes)", file=sys.stderr)
                except Exception:
                    print(f" - {p.name}", file=sys.stderr)
        except Exception:
            pass
        print(f"❌ No existe el audio: {audio_path}", file=sys.stderr)
        sys.exit(1)

    # Intentar transcripción con python whisper
    if transcribe_with_python_whisper(audio_path, srt_path, cfg):
        if srt_path.exists() and srt_path.stat().st_size > 0:
            print(f"✅ SRT generado con whisper python: {srt_path}")
            sys.exit(0)
        else:
            print("❌ Whisper Python no generó SRT válido.", file=sys.stderr)

    # Intentar fallback CLI whisper
    if transcribe_with_cli_whisper(audio_path, srt_path, cfg):
        if srt_path.exists() and srt_path.stat().st_size > 0:
            print(f"✅ SRT generado con whisper CLI: {srt_path}")
            sys.exit(0)
        else:
            print("❌ Whisper CLI no generó SRT válido.", file=sys.stderr)

    # Fallback a TXT + estimación
    if fallback_from_txt(audio_path, srt_path, cfg):
        sys.exit(0)

    print("❌ No se pudo generar SRT con ninguna opción.", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()