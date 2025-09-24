# audio.py
# -*- coding: utf-8 -*-
"""
Texto -> Audio (TTS) con voces diferenciadas por orador.
Interfaz compatible con podcast.py:
  - texto_a_audio(transcript_text, api_key, out_path) -> str (ruta WAV)
  - reproducir_podcast(audio_path) -> None

Características:
- Presentador (Héctor) y Entrevistado (Aura) con voces fijas.
- 'COLD OPEN' se locuta con voz de narrador (tercera voz).
- Filtra etiquetas [..] y emojis decorativos para evitar lecturas raras.
- Concatena segmentos a un único WAV con ffmpeg (sin reencode perceptible).
- Config desde config.json (opcional) para voces/modelo/parámetros TTS.

config.json (claves usadas):
{
  "presentador": "Héctor",
  "entrevistado": "Aura",

  "tts_model": "gpt-4o-mini-tts",
  "tts_voice_narrator": "alloy",
  "tts_voice_hector": "onyx",
  "tts_voice_aura": "sage",
  "tts_format": "wav",
  "tts_sample_rate": 24000,

  "tts_allow_emojis": false,
  "tts_whitelist_emojis": ["😂","😍","😲","😏","😉","🙏","🔥"],

  "OPENAI_API_KEY": "... (opcional; si no, usa el parámetro api_key)"
}
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv(".env")

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional

# ---------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------

def _read_config(path: Path = Path("config.json")) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _which(bin_name: str) -> Optional[str]:
    from shutil import which as _which
    return _which(bin_name)

def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------
# Cliente OpenAI
# ---------------------------------------------------------------------

def _load_openai_client(api_key: Optional[str] = None):
    """
    Carga el cliente oficial de OpenAI (SDK >= 1.0).
    Soporta streaming a fichero si está disponible.
    """
    try:
        from openai import OpenAI
        key = api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("Falta OPENAI_API_KEY (parámetro o variable de entorno).")
        return OpenAI(api_key=key)
    except Exception as e:
        raise RuntimeError(f"No se pudo cargar OpenAI SDK: {e}")

# ---------------------------------------------------------------------
# Filtrado de texto para TTS (evitar leer etiquetas/emojis “de adorno”)
# ---------------------------------------------------------------------

EMOJI_RANGES = [
    (0x1F300, 0x1FAFF),  # Symbols & Pictographs
    (0x2600,  0x27BF),   # Misc symbols + Dingbats
    (0x1F900, 0x1F9FF),  # Supplemental symbols
]

def _is_emoji_char(ch: str) -> bool:
    o = ord(ch)
    for a, b in EMOJI_RANGES:
        if a <= o <= b:
            return True
    return False

def _filter_emojis(text: str, allow: bool, whitelist: List[str], max_per_sentence: int = 1) -> str:
    if not allow:
        # Quita todos los emojis
        return "".join(ch for ch in text if not _is_emoji_char(ch))

    # Mantener solo whitelist y máx. 1 por frase
    def process_chunk(chunk: str) -> str:
        out = []
        count = 0
        for ch in chunk:
            if _is_emoji_char(ch):
                if ch in whitelist and count < max_per_sentence:
                    out.append(ch)
                    count += 1
                else:
                    continue
            else:
                out.append(ch)
        return "".join(out)

    parts = re.split(r'([\.!?]\s+|\n+)', text)
    buff = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            buff.append(process_chunk(part))
        else:
            buff.append(part)
    return "".join(buff)

_TAG_BRACKETS = re.compile(r"\[[^\]]+\]")

def _clean_for_tts(text: str, allow_emojis: bool, emoji_whitelist: List[str]) -> str:
    """
    - Elimina etiquetas [riendo], [con entusiasmo], etc.
    - Normaliza espacios y puntuación.
    - Filtra emojis (todos, o 1/whitelist por frase).
    """
    t = text.strip()
    t = _TAG_BRACKETS.sub("", t)
    t = _filter_emojis(t, allow_emojis, emoji_whitelist, max_per_sentence=1)
    # Espacios / puntuación
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([,;:\.\?!])", r"\1", t)  # sin espacio antes de signos
    t = re.sub(r"([\(¿¡])\s+", r"\1", t)
    t = t.strip(" ,;:")
    return t

# ---------------------------------------------------------------------
# Parse del transcript (string)
# ---------------------------------------------------------------------

def _parse_transcript_from_text(transcript_text: str, presenter: str, guest: str) -> List[Tuple[str, str]]:
    """
    Recibe el guion en texto plano y devuelve lista de (role, text),
    donde role ∈ {"NARRATOR","HECTOR","AURA"}.
    - Detecta 'COLD OPEN' como narrador.
    - Acepta 'Hector' sin tilde.
    - Junta líneas consecutivas del mismo role para evitar microcortes.
    """
    raw = transcript_text or ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    def who(speaker: str) -> Optional[str]:
        s = speaker.strip().lower().replace("é", "e")
        p = presenter.strip().lower().replace("é", "e")
        g = guest.strip().lower()
        if s in ("cold open", "coldopen", "intro", "narrador", "narration"):
            return "NARRATOR"
        if s == p:
            return "HECTOR"
        if s == g:
            return "AURA"
        if s == "hector":
            return "HECTOR"
        if s == "aura":
            return "AURA"
        return None

    pairs: List[Tuple[str, str]] = []
    for l in lines:
        m = re.match(r"^([^:]{1,40}):\s*(.*)$", l)  # "Orador: texto"
        if not m:
            # fallback: narrador (suele ocurrir con COLD OPEN sin prefijo)
            pairs.append(("NARRATOR", l))
            continue
        spk, text = m.group(1).strip(), m.group(2).strip()
        role = who(spk) or "NARRATOR"
        pairs.append((role, text))

    # Unir consecutivos del mismo role
    merged: List[Tuple[str, str]] = []
    for role, text in pairs:
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + " " + text)
        else:
            merged.append((role, text))
    return merged

# ---------------------------------------------------------------------
# Síntesis TTS y concatenación
# ---------------------------------------------------------------------

def _tts_to_file(client, model: str, voice: str, text: str, out_path: Path, fmt: str = "wav", sample_rate: int = 24000):
    """
    Intenta usar streaming a fichero; si no está disponible, usa método estándar.
    """
    _ensure_parent(out_path)
    # Intento: streaming
    try:
        with client.audio.speech.with_streaming_response.create(
            model=model, voice=voice, input=text, response_format=fmt
        ) as resp:
            resp.stream_to_file(out_path.as_posix())
            return
    except Exception:
        pass
    # Fallback no-streaming
    try:
        result = client.audio.speech.create(
            model=model, voice=voice, input=text, response_format=fmt
        )
        audio_bytes = None
        if hasattr(result, "read"):
            audio_bytes = result.read()
        if audio_bytes is None:
            audio_bytes = getattr(result, "content", None)
        if audio_bytes is None:
            audio_bytes = getattr(result, "audio", None)
        if audio_bytes is None:
            raise RuntimeError("Respuesta TTS sin audio.")
        out_path.write_bytes(audio_bytes)
    except Exception as e:
        raise RuntimeError(f"Fallo TTS ({voice}): {e}")

def _concat_wav_ffmpeg(chunk_paths: List[Path], out_wav: Path):
    """
    Concatena WAVs con ffmpeg 'concat demuxer' (sin reencode).
    """
    if _which("ffmpeg") is None:
        raise SystemExit("❌ No se encuentra ffmpeg en PATH.")
    _ensure_parent(out_wav)
    lst = out_wav.parent / "concat.txt"
    lst.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in chunk_paths) + "\n",
        encoding="utf-8"
    )
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst.as_posix(),
        "-c", "copy", out_wav.as_posix()
    ]
    subprocess.run(cmd, check=True)
    lst.unlink(missing_ok=True)

# ---------------------------------------------------------------------
# API pública (compat con podcast.py)
# ---------------------------------------------------------------------

def texto_a_audio(transcript_text: str, api_key: str, out_path: str) -> str:
    """
    Genera audio WAV con voces diferenciadas a partir de un transcript en texto.
    Retorna la ruta al WAV final (out_path).
    """
    cfg = _read_config(Path("config.json"))
    presenter = cfg.get("presentador", "Héctor")
    guest     = cfg.get("entrevistado", "Aura")

    # TTS config
    model        = cfg.get("tts_model", "gpt-4o-mini-tts")
    voice_narr   = cfg.get("tts_voice_narrator", "alloy")
    voice_hector = cfg.get("tts_voice_hector", "onyx")
    voice_aura   = cfg.get("tts_voice_aura", "sage")
    fmt          = cfg.get("tts_format", "wav")
    sample_rate  = int(cfg.get("tts_sample_rate", 24000))

    allow_emojis = bool(cfg.get("tts_allow_emojis", False))
    emoji_wh     = cfg.get("tts_whitelist_emojis", ["😂","😍","😲","😏","😉","🙏","🔥"])

    # Cliente
    client = _load_openai_client(api_key or cfg.get("OPENAI_API_KEY"))

    # Parse guion desde string
    pairs = _parse_transcript_from_text(transcript_text, presenter=presenter, guest=guest)

    print("🔊 Convirtiendo texto a audio (voces diferenciadas)...\n")
    out_wav = Path(out_path).with_suffix(".wav")
    chunks_dir = out_wav.parent / "chunks_tts"
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    # Genera un WAV por bloque
    chunk_paths: List[Path] = []
    for i, (role, raw_text) in enumerate(pairs, start=1):
        if not raw_text.strip():
            continue
        if role == "NARRATOR":
            voice = voice_narr
        elif role == "HECTOR":
            voice = voice_hector
        elif role == "AURA":
            voice = voice_aura
        else:
            voice = voice_narr  # fallback seguro

        tts_text = _clean_for_tts(raw_text, allow_emojis=allow_emojis, emoji_whitelist=emoji_wh)
        if not tts_text:
            print(f"  • {role:<8} → texto filtrado vacío, se omite.")
            continue

        out_chunk = chunks_dir / f"{i:03d}_{role.lower()}.{fmt}"
        print(f"  • {role:<8} → {voice:<8}  [{len(tts_text)} chars]")
        _tts_to_file(client, model=model, voice=voice, text=tts_text, out_path=out_chunk, fmt=fmt, sample_rate=sample_rate)
        chunk_paths.append(out_chunk)

    if not chunk_paths:
        raise SystemExit("❌ No se generaron chunks de audio (guion vacío tras filtrado).")

    if len(chunk_paths) == 1:
        # Solo un chunk, copiar directamente a out_wav
        print("⚠️ Solo un bloque de audio generado, copiando directamente al archivo final.")
        shutil.copy(chunk_paths[0], out_wav)
    else:
        print(f"🔗 Concatenando {len(chunk_paths)} bloques de audio con ffmpeg...")
        _concat_wav_ffmpeg(chunk_paths, out_wav)

    # Verificar que el archivo final existe y no está vacío
    if not out_wav.exists() or out_wav.stat().st_size == 0:
        raise RuntimeError(f"❌ El archivo de salida no se generó correctamente: {out_wav}")

    print(f"\n✅ Audio generado correctamente: {out_wav}")
    return out_wav.as_posix()

def reproducir_podcast(audio_path: str) -> None:
    """
    Reproduce el WAV con ffplay si está disponible.
    """
    if not Path(audio_path).exists():
        print(f"❌ No existe el audio: {audio_path}", file=sys.stderr)
        return
    if _which("ffplay"):
        try:
            subprocess.run(["ffplay", "-autoexit", "-nodisp", audio_path], check=True)
            return
        except Exception as e:
            print(f"⚠️ ffplay falló: {e}", file=sys.stderr)
    print(f"ℹ️ Reproduce el archivo manualmente: {audio_path}")

if __name__ == "__main__":
    import argparse

    def slugify(text: str) -> str:
        text = text.lower()
        text = re.sub(r"[^\w\s-]", "", text)
        text = re.sub(r"[\s_-]+", "-", text)
        text = re.sub(r"^-+|-+$", "", text)
        return text

    parser = argparse.ArgumentParser(description="Generar audio TTS desde texto de podcast con voces diferenciadas.")
    parser.add_argument("--tema-from-config", action="store_true", help="Obtener tema y rutas desde config.json")
    parser.add_argument("--config", default="config.json", help="Archivo de configuración JSON (default: config.json)")
    parser.add_argument("--txt", help="Archivo de texto con el guion (sobrescribe tema-from-config)")
    parser.add_argument("--out", help="Archivo de salida WAV (sobrescribe tema-from-config)")

    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = _read_config(config_path)

    slug = None
    basename = None
    txt_path = None
    out_path = None

    if args.tema_from_config:
        tema = cfg.get("tema", "")
        output_slug = cfg.get("output_slug")
        output_basename = cfg.get("output_basename")
        if not output_slug and tema:
            output_slug = slugify(tema)
        if not output_basename and output_slug:
            output_basename = output_slug
        if output_slug and output_basename:
            base_dir = Path("outputs") / output_slug
            txt_path = base_dir / f"{output_basename}.txt"
            out_path = base_dir / f"{output_basename}.wav"
        else:
            print("❌ No se pudo determinar output_slug o output_basename desde config para --tema-from-config.", file=sys.stderr)
            sys.exit(1)

    if args.txt:
        txt_path = Path(args.txt)
    if args.out:
        out_path = Path(args.out)

    if not txt_path or not txt_path.exists():
        print(f"❌ Archivo de texto con guion no encontrado: {txt_path}", file=sys.stderr)
        sys.exit(1)

    if not out_path:
        print("❌ Ruta de salida para audio no especificada.", file=sys.stderr)
        sys.exit(1)

    transcript_text = txt_path.read_text(encoding="utf-8")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("❌ No se encontró OPENAI_API_KEY en variables de entorno.", file=sys.stderr)
        sys.exit(1)

    try:
        wav_path = texto_a_audio(transcript_text, api_key, str(out_path))
    except Exception as e:
        print(f"❌ Error al generar audio: {e}", file=sys.stderr)
        sys.exit(1)

    if not Path(wav_path).exists() or Path(wav_path).stat().st_size == 0:
        print(f"❌ El archivo de audio generado está vacío o no existe: {wav_path}", file=sys.stderr)
        sys.exit(1)

    print(f"✅ Audio: {wav_path}")