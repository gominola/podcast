# video.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

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

def ensure_ass_from_srt(srt: Path, txt: Path, cfg_json: Path, ass_out: Path) -> None:
    """
    Llama al CLI srt_to_ass.py para colorear por orador y envolver líneas.
    """
    if not srt.exists():
        raise SystemExit(f"❌ No existe SRT: {srt}")
    if not txt.exists():
        raise SystemExit(f"❌ No existe TXT (guion): {txt}")

    # Ejecuta el script como CLI
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("srt_to_ass.py")),
        "--srt", str(srt),
        "--txt", str(txt),
        "--config", str(cfg_json),
        "--out", str(ass_out),
    ]
    run(cmd)
    if not ass_out.exists() or ass_out.stat().st_size == 0:
        raise SystemExit("❌ srt_to_ass no generó el ASS.")

def ffmpeg_burn(image: Path, audio: Path, ass: Path, out_mp4: Path, fps: int = 30, res: str = "1920x1080") -> None:
    if not image.exists():
        raise SystemExit(f"❌ No existe imagen: {image}")
    if not audio.exists():
        raise SystemExit(f"❌ No existe audio: {audio}")
    if not ass.exists():
        raise SystemExit(f"❌ No existe ASS: {ass}")

    # ffmpeg + libass: subtítulos desde ASS + imagen estática + audio
    # Nota: quotes cuidadosos para rutas con espacios
    vf = f"subtitles={ass.as_posix()}"
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
    ap.add_argument("--out", default=None)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--res", default="1920x1080")
    args = ap.parse_args()

    cfg_json = Path(args.config).resolve()

    if args.tema_from_config:
        paths = build_paths_from_config(cfg_json)
        image = Path(args.image).resolve()
        audio = Path(args.audio).resolve() if args.audio else paths["wav"]
        srt   = Path(args.srt).resolve() if args.srt else paths["srt"]
        txt   = paths["txt"]
        ass   = paths["ass"]
        out_mp4 = Path(args.out).resolve() if args.out else paths["mp4"]
    else:
        # modo manual
        if not (args.audio and args.srt and args.out):
            raise SystemExit("❌ En modo manual indica --audio --srt --out (y opcionalmente --image).")
        image = Path(args.image).resolve()
        audio = Path(args.audio).resolve()
        srt   = Path(args.srt).resolve()
        # asumimos .txt al lado de .srt con el mismo basename
        txt   = srt.with_suffix(".txt")
        ass   = srt.with_suffix(".ass")
        out_mp4 = Path(args.out).resolve()

    print("🎬 Preparando ASS coloreado a partir de SRT + TXT…")
    ensure_ass_from_srt(srt, txt, cfg_json, ass)

    print("🎬 Generando MP4 (ffmpeg + libass)…")
    ffmpeg_burn(image, audio, ass, out_mp4, fps=args.fps, res=args.res)
    print(f"✅ Vídeo listo: {out_mp4}")

if __name__ == "__main__":
    main()