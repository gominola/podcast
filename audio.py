# -*- coding: utf-8 -*-
"""
audio.py
Genera audio TTS a partir del guion con dos voces diferenciadas (Héctor/Aura)
usando OpenAI TTS (gpt-4o-mini-tts). Devuelve un .wav concatenado listo para edición o publicación.

Requiere:
- openai>=1.0
- pydub
- ffmpeg en PATH
"""

from __future__ import annotations
import os
import re
import tempfile
from typing import List, Tuple

from pydub import AudioSegment

try:
    from openai import OpenAI
except Exception:
    import openai  # type: ignore
    OpenAI = openai.OpenAI


# ========= Config TTS =========
TTS_MODEL = "gpt-4o-mini-tts"

SUPPORTED_VOICES = {
    "alloy","echo","fable","onyx","nova","shimmer",
    "coral","verse","ballad","ash","sage","marin","cedar"
}

# Mapea oradores -> voces (elige las que más te gusten)
# Voces comunes: "alloy", "verse", "aria", "luna", "sage", "coral" (según disponibilidad)
VOICE_MAP = {
    "Héctor": "marin",
    "Aura": "shimmer",
}

# Pausas entre líneas (en milisegundos)
PAUSA_ENTRE_LINEAS_MS = 450
PAUSA_CAMBIO_LOCUTOR_MS = 700

# Formato de salida por fragmento
FRAGMENT_FORMAT = "mp3"  # o "wav" / "mp3". El master final lo exportamos a WAV

def _safe_voice(name: str) -> str:
    v = VOICE_MAP.get(name, "")
    if v not in SUPPORTED_VOICES:
        # fallback estable
        return "verse" if name.lower().startswith(("h","presentador")) else "nova"
    return v

def _parse_transcript_lines(guion_texto: str) -> List[Tuple[str, str]]:
    """
    Extrae [(speaker, text)] a partir de líneas tipo 'Nombre: texto'.
    Ignora líneas vacías y bloques COLD OPEN como speaker='COLD OPEN'.
    """
    items: List[Tuple[str, str]] = []
    for raw in guion_texto.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Formatos esperados: "Héctor: texto", "Aura: texto", "[COLD OPEN] ..." o "COLD OPEN: ..."
        m = re.match(r"^\[?COLD OPEN\]?\s*:?\s*(.+)$", line, flags=re.IGNORECASE)
        if m:
            items.append(("COLD OPEN", m.group(1).strip()))
            continue
        m2 = re.match(r"^([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)\s*:\s*(.+)$", line)
        if m2:
            speaker = m2.group(1).strip()
            text = m2.group(2).strip()
            items.append((speaker, text))
    return items


def _tts_fragment(client: OpenAI, text: str, voice: str, out_path: str) -> None:
    """
    Genera un fragmento TTS y lo guarda en disco (streaming).
    """
    try:
        with client.audio.speech.with_streaming_response.create(
            model=TTS_MODEL,
            voice=voice,
            input=text,
            response_format=FRAGMENT_FORMAT  # "mp3" o "wav"
        ) as resp:
            resp.stream_to_file(out_path)
    except TypeError:
        # Fallback para clientes que no soporten with_streaming_response
        audio = client.audio.speech.create(
            model=TTS_MODEL,
            voice=voice,
            input=text,
            response_format=FRAGMENT_FORMAT
        )
        try:
            audio.write_to_file(out_path)
        except Exception:
            data = getattr(audio, "audio", None) or getattr(audio, "content", None) or bytes()
            with open(out_path, "wb") as f:
                f.write(data)

def _concat_segments(temp_files: List[Tuple[str, str]], master_path: str) -> str:
    """
    Concatena los fragmentos generados respetando pausas entre líneas
    y una pausa mayor en cambio de locutor. Exporta WAV master.
    """
    if not temp_files:
        raise ValueError("No hay fragmentos TTS para concatenar.")

    master = AudioSegment.silent(duration=250)
    prev_speaker = None

    for speaker, path in temp_files:
        frag = AudioSegment.from_file(path)
        # Pausa diferente si cambia de locutor
        if prev_speaker and speaker != prev_speaker:
            master += AudioSegment.silent(duration=PAUSA_CAMBIO_LOCUTOR_MS)
        else:
            master += AudioSegment.silent(duration=PAUSA_ENTRE_LINEAS_MS)
        master += frag
        prev_speaker = speaker

    # Normaliza a -1.0 dBFS ligero
    target_gain = -1.0 - master.max_dBFS
    master = master.apply_gain(target_gain)

    # Exporta WAV final
    if not master_path.lower().endswith(".wav"):
        master_path += ".wav"
    master.export(master_path, format="wav")
    return master_path


def texto_a_audio(guion_texto: str, api_key: str, out_path: str = "podcast_final.wav") -> str:
    """
    Entrada:
      - guion_texto: transcript plano (como devuelve tu guion.py si no guarda a archivo).
      - api_key: OPENAI_API_KEY.
      - out_path: ruta del WAV final.
    Salida:
      - Ruta del WAV exportado.
    """
    if not api_key:
        raise ValueError("Falta OPENAI_API_KEY para TTS.")
    client = OpenAI(api_key=api_key)

    lines = _parse_transcript_lines(guion_texto)
    if not lines:
        raise ValueError("No se pudo parsear el guion (¿formato 'Nombre: texto'?).")

    tmpdir = tempfile.mkdtemp(prefix="tts_frag_")
    temp_files: List[Tuple[str, str]] = []

    for speaker, text in lines:
        # Omite etiquetas no habladas
        if speaker.upper() in {"COLD OPEN"}:
            # Puedes asignar COLD OPEN a una voz (ej. Héctor) si quieres que se lea
            speaker_for_cold = "Héctor"
            voice = VOICE_MAP.get(speaker_for_cold, "verse")
            fn = os.path.join(tmpdir, f"{speaker_for_cold}_{len(temp_files):04d}.{FRAGMENT_FORMAT}")
            _tts_fragment(client, text, voice, fn)
            temp_files.append((speaker_for_cold, fn))
            continue

        # Selecciona voz
        voice = _safe_voice(speaker)

        fn = os.path.join(tmpdir, f"{speaker}_{len(temp_files):04d}.{FRAGMENT_FORMAT}")
        _tts_fragment(client, text, voice, fn)
        temp_files.append((speaker, fn))

    final = _concat_segments(temp_files, out_path)
    return final


def reproducir_podcast(path: str) -> None:
    """
    Reproducción simple (opcional): usa ffplay si existe.
    """
    try:
        os.system(f'ffplay -nodisp -autoexit "{path}"')
    except Exception:
        print(f"Reproduce manualmente el archivo: {path}")