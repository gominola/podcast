# video.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# Nota: usamos el filtro 'ass=' (no 'subtitles=') para archivos .ass y escapamos el path
# para evitar problemas con ':' ',' '\' o quotes en rutas al pasar por -vf.

def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def count_ass_events(ass_path: Path) -> int:
    """
    Devuelve el número de líneas 'Dialogue:' en un .ass.
    Si el archivo no existe o falla la lectura, devuelve 0.
    """
    try:
        text = ass_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0
    count = 0
    for line in text.splitlines():
        if line.lstrip().startswith("Dialogue:"):
            count += 1
    return count

def ensure_ass_ready(ass: Path) -> Path:
    """
    Verifica que el ASS existe y contiene al menos un 'Dialogue:'.
    Si no, aborta con un mensaje claro para que el usuario ejecute `make srt`.
    """
    if not ass.exists():
        raise SystemExit(f"❌ No existe ASS: {ass}\n   Ejecuta primero `make srt` para generarlo.")
    n = count_ass_events(ass)
    if n == 0:
        raise SystemExit(f"❌ ASS sin eventos (0 'Dialogue'). Revisa 'timeline_to_subs.py' y ejecuta `make srt`.")
    return ass

def build_paths_from_config(cfg_path: Path) -> dict:
    cfg = read_json(cfg_path)
    slug = cfg.get("output_slug") or cfg.get("tema_slug") or "podcast"
    basename = cfg.get("output_basename") or slug
    outdir = Path.cwd() / "outputs" / slug
    outdir.mkdir(parents=True, exist_ok=True)
    return {
        "slug": slug,
        "basename": basename,
        "outdir": outdir,
        "txt": outdir / f"{basename}.txt",
        "wav": outdir / f"{basename}.wav",
        "srt": outdir / f"{basename}.srt",
        "ass": outdir / f"{basename}.ass",
        "mp4": outdir / f"{basename}_fast.mp4",
    }

def run(cmd: list[str]) -> None:
    # Imprime comando bonito y ejecuta
    print("  $", " ".join(shlex.quote(c) for c in cmd))
    subprocess.run(cmd, check=True)

def _ff_escape(p: Path) -> str:
    """
    Escapa la ruta para filtros de ffmpeg (ass=/subtitles=) donde ':' ',' '\' y comillas rompen el parser.
    """
    s = p.as_posix()
    s = s.replace("\\", "\\\\")
    s = s.replace(":", r"\:")
    s = s.replace(",", r"\,")
    s = s.replace("'", r"\'")
    return s

def ffmpeg_burn(image: Path, audio: Path, ass: Path | None, out_mp4: Path, fps: int = 30, res: str = "1920x1080", vf_override: str | None = None) -> None:
    if not image.exists():
        raise SystemExit(f"❌ No existe imagen: {image}")
    if not audio.exists():
        raise SystemExit(f"❌ No existe audio: {audio}")

    if vf_override is None:
        if ass is None or not ass.exists():
            raise SystemExit(f"❌ No existe ASS: {ass}")
        vf = f"ass={_ff_escape(ass)}"
    else:
        vf = vf_override

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-framerate", str(fps),
        "-i", str(image),
        "-i", str(audio),
        "-vf", vf,
        "-s", res,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "stillimage",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(out_mp4),
    ]
    run(cmd)

def main():
    ap = argparse.ArgumentParser(description="Genera MP4 con imagen estática y subtítulos ASS.")
    ap.add_argument("--tema-from-config", action="store_true", help="Usar config.json para rutas")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--image", required=False, default="assets/studio_full.jpg")
    ap.add_argument("--audio", default=None)
    ap.add_argument("--srt", default=None)
    ap.add_argument("--ass", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--res", default="1920x1080")
    args = ap.parse_args()

    cfg_json = Path(args.config).resolve()

    if args.tema_from_config:
        paths = build_paths_from_config(cfg_json)
        image = Path(args.image).resolve()
        audio = Path(args.audio).resolve() if args.audio else paths["wav"]
        ass   = paths["ass"]
        out_mp4 = Path(args.out).resolve() if args.out else paths["mp4"]
    else:
        # modo manual
        if not (args.audio and (args.ass or args.srt) and args.out):
            raise SystemExit("❌ En modo manual indica --audio --ass --out (o --audio --srt --out si el .ass está junto al .srt).")
        image = Path(args.image).resolve()
        audio = Path(args.audio).resolve()
        if args.ass:
            ass = Path(args.ass).resolve()
        else:
            srt = Path(args.srt).resolve()
            ass = srt.with_suffix(".ass")
        out_mp4 = Path(args.out).resolve()

    print(f"🎬 Usando ASS ya generado: {ass}")
    ensure_ass_ready(ass)
    print("🎬 Generando MP4 (ffmpeg + libass)…")
    ffmpeg_burn(image, audio, ass, out_mp4, fps=args.fps, res=args.res)

    print(f"✅ Vídeo listo: {out_mp4}")

if __name__ == "__main__":
    main()