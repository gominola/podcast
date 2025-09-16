# video.py
# -*- coding: utf-8 -*-
"""
Genera un vídeo estático (imagen + audio) e incrusta subtítulos SRT con estilo forzado
usando FFmpeg (libass). Mucho más rápido y con control total del estilo.

Uso típico (se integra con Makefile):
    python video.py --tema-from-config

Parámetros:
    --tema-from-config          Lee el tema de config.json y resuelve rutas:
                                image=assets/studio_bg.jpg (fallback cover.jpg),
                                audio=outputs/<slug>/podcast_<slug>.wav,
                                srt=outputs/<slug>/podcast_<slug>.srt,
                                out=outputs/<slug>/podcast_<slug>.mp4
    --image <ruta>              Imagen de fondo (1920x1080 ideal). Si falta, se escala.
    --audio <ruta>              Audio .wav o .mp3
    --srt <ruta>                Subtítulos .srt (si falta, construye vídeo sin subs)
    --out <ruta>                Salida .mp4
    --fps <int>                 FPS del contenedor (por defecto 30)
    --res <WxH>                 Resolución (por defecto 1920x1080)
    --font                     Nombre de fuente ASS (por defecto Arial)
    --fontsize                 Tamaño de fuente (por defecto 28)
    --margin_v                 Margen vertical inferior en px (por defecto 36)
    --outline                  Grosor del borde (por defecto 1.5)
    --shadow                   Sombra (por defecto 0)
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

# -----------------------
# Utilidades
# -----------------------

def slugify(text: str) -> str:
    import re
    t = text.lower()
    t = re.sub(r"[^a-z0-9áéíóúüñ\s-]", "", t)
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"-+", "-", t).strip("-")
    return t

def load_config(path: str = "config.json") -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def ensure_parent(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def which(cmd: str) -> str | None:
    from shutil import which as w
    return w(cmd)

def get_audio_duration_seconds(audio_path: Path) -> float:
    """
    Usa ffprobe para obtener la duración exacta del audio. Requiere ffprobe.
    Si no está disponible, retorna 0 (FFmpeg cortará con -shortest igualmente).
    """
    ffprobe = which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        # salida solo con duration
        out = subprocess.check_output([
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nokey=1:noprint_wrappers=1", str(audio_path)
        ], text=True).strip()
        return float(out)
    except Exception:
        return 0.0

# -----------------------
# FFmpeg builder
# -----------------------

def build_ffmpeg_cmd(
    image: Path,
    audio: Path,
    out_path: Path,
    srt: Path | None = None,
    fps: int = 30,
    res: str = "1920x1080",
    font: str = "Arial",
    fontsize: int = 28,
    margin_v: int = 36,
    outline: float = 1.5,
    shadow: int = 0,
) -> list[str]:
    """
    Construye el comando FFmpeg:
    -loop 1 para imagen estática
    -shortest para cortar al final del audio
    -vf con scale + (opcional) subtitles con force_style
    """
    w, h = (int(x) for x in res.lower().split("x"))
    vf_parts = [f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"]

    # Forzar estilo ASS sobre SRT con libass
    if srt and srt.exists():
        # Nota colores ASS: PrimaryColour es AABBGGRR en hex con &H y alpha delante (00=opaco).
        # No forzamos color aquí para mantener contraste del outline. Puedes añadir PrimaryColour si quieres.
        force_style = (
            f"FontName={font},"
            f"Fontsize={fontsize},"
            f"Outline={outline},"
            f"Shadow={shadow},"
            f"Alignment=2,"          # 2 = centrado abajo
            f"MarginV={margin_v}"
        )
        # Ojo con rutas con espacios → hay que escaparlas en el filtro
        subs_filter = f"subtitles='{srt.as_posix()}':force_style='{force_style}'"
        vf_parts.append(subs_filter)

    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg",
        "-y",
        "-r", str(fps),
        "-loop", "1", "-i", str(image),
        "-i", str(audio),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-vf", vf,
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]
    return cmd

# -----------------------
# Main
# -----------------------

def main():
    ap = argparse.ArgumentParser(description="Genera vídeo estático (imagen+audio) con subtítulos estilizados (FFmpeg).")
    ap.add_argument("--tema-from-config", action="store_true", help="Leer tema de config.json y resolver rutas por defecto.")
    ap.add_argument("--image", type=str, help="Imagen de fondo (ej. assets/studio_bg.jpg)")
    ap.add_argument("--audio", type=str, help="Audio .wav/.mp3")
    ap.add_argument("--srt", type=str, help="Subtítulos .srt (opcional)")
    ap.add_argument("--out", type=str, help="Salida .mp4")
    ap.add_argument("--fps", type=int, default=30, help="FPS (default 30)")
    ap.add_argument("--res", type=str, default="1920x1080", help="Resolución WxH (default 1920x1080)")
    ap.add_argument("--font", type=str, default="Arial", help="Fuente ASS (default Arial)")
    ap.add_argument("--fontsize", type=int, default=28, help="Tamaño fuente (default 28)")
    ap.add_argument("--margin_v", type=int, default=36, help="Margen vertical inferior px (default 36)")
    ap.add_argument("--outline", type=float, default=1.5, help="Grosor del borde (default 1.5)")
    ap.add_argument("--shadow", type=int, default=0, help="Sombra (default 0)")
    args = ap.parse_args()

    cfg = {}
    slug = None
    if args.tema_from_config:
        cfg = load_config("config.json")
        tema = cfg.get("tema", "podcast")
        slug = slugify(tema)
        # Rutas por defecto
        if not args.audio:
            args.audio = f"outputs/{slug}/podcast_{slug}.wav"
        if not args.srt:
            args.srt = f"outputs/{slug}/podcast_{slug}.srt"
        if not args.out:
            args.out = f"outputs/{slug}/podcast_{slug}.mp4"
        if not args.image:
            # Prioriza estudio; si no, cover
            if Path("assets/studio_bg.jpg").exists():
                args.image = "assets/studio_bg.jpg"
            elif Path("assets/cover.jpg").exists():
                args.image = "assets/cover.jpg"

    # Validaciones mínimas
    ffmpeg_bin = which("ffmpeg")
    if not ffmpeg_bin:
        print("❌ FFmpeg no está instalado o no está en PATH.", file=sys.stderr)
        sys.exit(1)

    if not args.image:
        print("❌ Debes indicar --image o tener assets/studio_bg.jpg|assets/cover.jpg.", file=sys.stderr)
        sys.exit(1)
    if not args.audio:
        print("❌ Debes indicar --audio o usar --tema-from-config.", file=sys.stderr)
        sys.exit(1)
    if not args.out:
        print("❌ Debes indicar --out o usar --tema-from-config.", file=sys.stderr)
        sys.exit(1)

    image = Path(args.image)
    audio = Path(args.audio)
    out_path = Path(args.out)
    srt = Path(args.srt) if args.srt else None

    if not image.exists():
        print(f"❌ No existe la imagen: {image}", file=sys.stderr)
        sys.exit(1)
    if not audio.exists():
        print(f"❌ No existe el audio: {audio}", file=sys.stderr)
        sys.exit(1)
    if srt and not srt.exists():
        print(f"⚠️  No existe el SRT: {srt}. Se generará vídeo sin subtítulos.", file=sys.stderr)
        srt = None

    # Info útil
    dur = get_audio_duration_seconds(audio)
    if dur > 0:
        mm = int(dur // 60); ss = int(dur % 60)
        print(f"⏱️  Duración audio: {mm:02d}:{ss:02d}")
    else:
        print("⏱️  Duración audio: desconocida (ffprobe no disponible)")

    # Construir y ejecutar FFmpeg
    ensure_parent(out_path)
    cmd = build_ffmpeg_cmd(
        image=image,
        audio=audio,
        out_path=out_path,
        srt=srt,
        fps=args.fps,
        res=args.res,
        font=args.font,
        fontsize=args.fontsize,
        margin_v=args.margin_v,
        outline=args.outline,
        shadow=args.shadow,
    )

    print("🎬 Creando vídeo con FFmpeg…")
    # Mostrar comando “bonito” para depuración
    print("   " + " ".join(shlex.quote(c) for c in cmd))
    try:
        subprocess.check_call(cmd)
        print(f"✅ Vídeo generado: {out_path}")
    except subprocess.CalledProcessError as e:
        print(f"💥 Error al generar el vídeo (FFmpeg): {e}", file=sys.stderr)
        sys.exit(e.returncode)


if __name__ == "__main__":
    main()