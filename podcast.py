# -*- coding: utf-8 -*-
"""
Generador de guion de podcast (solo texto).
Lee configuración desde config.json y usa guion.py.
"""

import os
import re
import sys
import json

from dotenv import load_dotenv
load_dotenv(".env")

from guion import generar_podcast, slugify

CONFIG_PATH = "config.json"


def _leer_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _extrae_path_si_es_msg(s: str) -> str:
    m = re.match(r"^Archivo guardado:\s*(.+)$", s.strip())
    return m.group(1).strip() if m else ""


def main():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("❌ Falta OPENAI_API_KEY en .env", file=sys.stderr)
        sys.exit(1)

    cfg = _leer_config()
    tema = cfg.get("tema", "El universo")
    slug = cfg.get("output_slug") or slugify(tema)
    basename = cfg.get("output_basename") or slug

    outdir = os.path.join("outputs", slug)
    os.makedirs(outdir, exist_ok=True)

    print(f"📝 Generando guion para: {tema}\n")

    try:
        resultado = generar_podcast(api_key)
    except Exception as e:
        print(f"❌ Error generando el guion: {e}", file=sys.stderr)
        sys.exit(1)

    saved_path = _extrae_path_si_es_msg(resultado)
    if saved_path:
        print(f"✅ Guion generado en: {os.path.dirname(saved_path)}")
    else:
        print(resultado)


if __name__ == "__main__":
    main()