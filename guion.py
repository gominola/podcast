# -*- coding: utf-8 -*-
"""
Generador de guiones de podcast a dos voces (estilo conversación realista).

- Lee config base + config.json + temas/<slug(tema)>.json (el tema sobrescribe).
- Usa preguntas_guia primero; después preguntas improvisadas según rango.
- Exporta en .md / .txt / .srt respetando 'formato_guardado'.
- Guarda SIEMPRE en outputs/<slug>/podcast_<slug>.<ext>.
- Limpia robotismos y muletillas.
- Mensajes de depuración para confirmar la configuración efectiva.

Actualizaciones:
- Guardado UTF-8 robusto (incluye newline="\n").
- Nuevo campo de config: 'formato_salida' (contrato de salida + few-shot para emociones/etiquetas/emojis).
- Post-proceso ligero 'enriquecer_dialogo' para añadir emojis/pausas si el modelo no los incluye.
"""

from __future__ import annotations

from dotenv import load_dotenv
load_dotenv(".env")

# SDK compat: intenta usar `from openai import OpenAI`, cae a openai.OpenAI si no existe.
try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    import openai  # type: ignore
    OpenAI = openai.OpenAI

import json
import os
import random
import re
from typing import List, Tuple
from datetime import datetime

from colorama import Fore, Style, init as colorama_init
colorama_init(autoreset=True)

# ---------------------------------------------------------------------
# Configuración base y utilidades
# ---------------------------------------------------------------------

CONFIG_PATH = "config.json"

DEFAULT_CONFIG = {
    "tema": "El universo",
    "tema_slug": None,
    "presentador": "Héctor",
    "entrevistado": "Aura",
    "idioma": "es",
    "tono_hector": "curioso, directo, incisivo; evita cumplidos y el nombre del invitado salvo lo imprescindible",
    "tono_aura": "clara, concreta; ejemplos breves y analogías sencillas; cero peloteo",
    "nivel_formalidad": "medio",            # baja | medio | alta
    "longitud_respuestas": "media",         # corta | media | larga
    "guardar_guion": True,
    "formato_guardado": "md",               # md | txt | srt
    "preguntas_guia": [],                   # si vacío, se generan 6–8
    "preguntas_improvisadas": [1, 2],       # [min, max] por bloque
    "modelo": "gpt-4o-mini",
    "temperatura": 0.85,
    "semilla": None,
    "max_turnos": 70,                       # nº de intervenciones del invitado aprox.
    "incluir_cold_open": True,
    "incluir_cierre_llamado": True,
    "humor_nivel": "bajo",                  # bajo|medio|medio-alto|alto
    "permitir_ironia": False,
    "referencias_pop": False,
    "muletillas_permitidas": [],
    "estilo_dialogo": [],                   # array de líneas (desde el JSON del tema)
    # NUEVO: contrato de salida para forzar etiquetas/emojis/pausas (array de líneas)
    "formato_salida": []
    ,
    "modo": "prod",
    "textos": {
        "bienvenida": "¡Hola a todos y bienvenidos a un nuevo episodio de 'chIArlando'! Hoy el tema es **{tema}**. Tenemos a {entrevistado} con nosotros. ¡Bienvenido, {entrevistado}!",
        "cierre_previo": "Ha sido una charla fantástica sobre **{tema}**. Antes de cerrar, {entrevistado}, ¿te gustaría dejar una última reflexión breve?",
        "cierre_final": "🎙️ Gracias por escucharnos. Si te ha gustado, compártelo y deja tu valoración. ¡Hasta la próxima!",
        "cta_cierre": " Síguenos y cuéntanos qué te gustaría escuchar la próxima vez."
    }
    ,
    "output_slug": None,
    "output_basename": None,
    "txt_utf8_bom": True
}

LONGITUD_MAP = {
    "corta": "1–2 frases",
    "media": "3–5 frases",
    "larga": "5–8 frases"
}

ROBOTISMO_BANLIST = [
    "como modelo de inteligencia artificial",
    "como IA",
    "no tengo acceso a",
    "no puedo acceder",
    "no puedo proporcionar",
    "mi entrenamiento",
    "datos de entrenamiento",
    "lenguaje de gran tamaño",
    "large language model",
    "soy un asistente",
    "como asistente",
]

MULETILLAS_INICIO = re.compile(
    r"^(gran pregunta|buena pregunta|excelente cuestión|me encanta que (me )?preguntes|"
    r"gracias por (la|tu) pregunta|como bien dices|efectivamente|sin duda|por supuesto|"
    r"queridos oyentes|estimados oyentes|hola a todos|hola a todas)\b[:,]?\s*",
    re.IGNORECASE
)

MULETILLAS_GENERICAS = [
    "impresionante", "fascinante", "increíble", "es muy interesante",
    "es súper interesante", "sin lugar a dudas", "debo decir que",
    "me gustaría decir que", "la verdad es que",
]

def slugify(texto: str) -> str:
    t = texto.lower()
    t = re.sub(r"[^a-z0-9áéíóúüñ\s-]", "", t)
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"-+", "-", t).strip("-")
    return t

def _ruta_tema(tema: str) -> str:
    base_dir = os.path.join(os.path.dirname(__file__), "temas")
    return os.path.join(base_dir, f"{slugify(tema)}.json")

def _cargar_config_tema(tema: str) -> dict:
    ruta = _ruta_tema(tema)
    try:
        if os.path.exists(ruta):
            print(f"{Fore.CYAN}[TEMA]{Style.RESET_ALL} Cargado tema desde: {ruta}")
            with open(ruta, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"{Fore.YELLOW}Aviso: no se pudo cargar el tema '{tema}': {e}{Style.RESET_ALL}")
    return {}

def cargar_configuracion() -> dict:
    """
    Carga DEFAULT_CONFIG + config.json + temas/<slug>.json.
    El tema sobrescribe claves de DEFAULT_CONFIG si están presentes.
    """
    cfg = DEFAULT_CONFIG.copy()

    # 1) config.json
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                incoming = json.load(f)
                if isinstance(incoming, dict):
                    cfg.update(incoming)
        except Exception as e:
            print(f"{Fore.YELLOW}Aviso: no se pudo cargar config.json ({e}). "
                  f"Se usan valores por defecto.{Style.RESET_ALL}")

    # 2) temas/<slug>.json (sobrescribe)
    tema_sel = cfg.get("tema", DEFAULT_CONFIG["tema"])
    tema_slug_sel = cfg.get("tema_slug", tema_sel)  # permite forzar archivo de tema independiente del título visible
    cfg_tema = _cargar_config_tema(tema_slug_sel)
    if cfg_tema:
        permitidas = set(DEFAULT_CONFIG.keys())
        for k, v in cfg_tema.items():
            if k in permitidas:
                cfg[k] = v

    # Guardar el tema_slug efectivo (solo para diagnóstico)
    cfg["tema_slug"] = tema_slug_sel

    # Normalizaciones
    # preguntas_improvisadas -> lista [min,max]
    pi = cfg.get("preguntas_improvisadas", [1, 2])
    if isinstance(pi, int):
        cfg["preguntas_improvisadas"] = [max(0, pi), max(0, pi)]
    elif isinstance(pi, (list, tuple)) and len(pi) == 2:
        cfg["preguntas_improvisadas"] = [max(0, int(pi[0])), max(0, int(pi[1]))]
    else:
        cfg["preguntas_improvisadas"] = [1, 2]

    # formato
    cfg["formato_guardado"] = str(cfg.get("formato_guardado", "md")).lower()
    if cfg["formato_guardado"] not in {"md", "txt", "srt"}:
        cfg["formato_guardado"] = "md"

    # NUEVO: formato_salida debe ser lista de líneas
    fs = cfg.get("formato_salida", [])
    if not isinstance(fs, list):
        fs = []
    cfg["formato_salida"] = fs

    # Asegurar textos
    tx = cfg.get("textos", {})
    if not isinstance(tx, dict):
        tx = {}
    cfg["textos"] = tx

    return cfg

config = cargar_configuracion()

# Extraer tema_slug para debug/config resumen
tema_slug_cfg       = config.get("tema_slug")

# Variables (ya fusionadas)
tema                = config["tema"]
presentador         = config["presentador"]
entrevistado        = config["entrevistado"]
idioma              = config["idioma"]
tono_hector         = config["tono_hector"]
tono_aura           = config["tono_aura"]
nivel_formalidad    = config["nivel_formalidad"]
longitud_respuestas = config["longitud_respuestas"]
guardar_guion_flag  = config["guardar_guion"]
formato_guardado    = config["formato_guardado"]
preguntas_guia      = list(config.get("preguntas_guia", []))
preguntas_improvisadas = config["preguntas_improvisadas"]
modelo              = config["modelo"]
temperatura         = float(config["temperatura"])
semilla             = config["semilla"]
max_turnos          = int(config["max_turnos"])
incluir_cold_open   = bool(config["incluir_cold_open"])
incluir_cierre_llamado = bool(config["incluir_cierre_llamado"])
humor_nivel         = config.get("humor_nivel", "bajo")
permitir_ironia     = bool(config.get("permitir_ironia", False))
referencias_pop     = bool(config.get("referencias_pop", False))
muletillas_permitidas = set(config.get("muletillas_permitidas", []))
estilo_dialogo_lines  = config.get("estilo_dialogo", [])
formato_salida_lines  = config.get("formato_salida", [])
modo                = str(config.get("modo", "prod")).lower()
textos              = config.get("textos", {})
output_slug         = config.get("output_slug")
output_basename     = config.get("output_basename")
if not isinstance(estilo_dialogo_lines, list):
    estilo_dialogo_lines = []
if not isinstance(formato_salida_lines, list):
    formato_salida_lines = []

if semilla is not None:
    random.seed(semilla)

# Debug: muestra la config efectiva (clave -> valor resumido)
def _dbg_resumen_config():
    resumen = {
        "tema": tema,
        "tema_visible": tema,
        "slug": slugify(tema),
        "tema_slug": slugify(tema_slug_cfg if tema_slug_cfg else tema),
        "formato_guardado": formato_guardado,
        "max_turnos": max_turnos,
        "preguntas_guia": len(preguntas_guia),
        "preguntas_improvisadas": preguntas_improvisadas,
        "modelo": modelo,
        "temperatura": temperatura,
        "formato_salida": len(formato_salida_lines),
        "modo": modo,
        "textos_keys": sorted(list(textos.keys())) if isinstance(textos, dict) else [],
        "output_slug": None,  # placeholder; will be set below
        "output_basename": None
    }
    # Completar con helpers que dependen de funciones
    try:
        resumen["output_slug"] = _get_output_slug()
        resumen["output_basename"] = _get_output_basename(resumen["output_slug"])
    except Exception:
        pass
    # Agregar campo legacy_slug_if_any
    resumen["legacy_slug_if_any"] = slugify(tema)
    print(f"{Fore.CYAN}[CONFIG EFECTIVA]{Style.RESET_ALL} {json.dumps(resumen, ensure_ascii=False)}")

# ---------------------------------------------------------------------
# Limpiezas / estilo
# ---------------------------------------------------------------------

def _limpia_robotismos(texto: str) -> str:
    t = texto.strip()
    for ban in ROBOTISMO_BANLIST:
        if ban.lower() in t.lower():
            t = re.sub(re.escape(ban), "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")
    # reduce paréntesis largos
    t = re.sub(r"\s*\((?:[^)]{0,80})\)", lambda m: "" if len(m.group(0)) > 40 else m.group(0), t)
    return t

def _contraparte(orador: str) -> str:
    return presentador if orador == entrevistado else entrevistado

def _limpia_muletillas(texto: str, orador: str) -> str:
    t = texto.strip()
    t = MULETILLAS_INICIO.sub("", t)
    otro = _contraparte(orador)
    t = re.sub(rf"^({re.escape(otro)})\s*,\s*", "", t)
    t = re.sub(rf"\b({re.escape(otro)})\s*,", "", t)
    for m in MULETILLAS_GENERICAS:
        t = re.sub(rf"^(?:{re.escape(m)})[, ]+\s*", "", t, flags=re.IGNORECASE)
    arranque = re.compile(r"^(oye|mira|bueno|pues|a ver)\s*,\s*", re.IGNORECASE)
    if not any(t.lower().startswith(m.lower()) for m in muletillas_permitidas):
        t = arranque.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")
    return t

def _recorta_preambulos_en_preguntas(t: str) -> str:
    t = re.sub(r"^¿\s+", "¿", t)
    t = re.sub(r"^¿\s*(podrías|puedes|serías capaz de|te parece si)\s+", "¿", t, flags=re.IGNORECASE)
    t = re.sub(r"^¿¿", "¿", t)
    return t

def _quita_prefijo_orador(texto: str, orador: str) -> str:
    pref = f"{orador}:"
    t = texto.strip()
    if t.lower().startswith(pref.lower()):
        t = t.split(":", 1)[1].strip()
    return t

# ---------------------------------------------------------------------
# Helpers para slug/basename de outputs
# ---------------------------------------------------------------------
def _get_output_slug() -> str:
    """Slug definitivo para carpeta de outputs: usa 'output_slug' si está, si no slugify(tema)."""
    if isinstance(output_slug, str) and output_slug.strip():
        return output_slug.strip()
    return slugify(tema)

def _get_output_basename(slug: str) -> str:
    """Nombre base de archivo en outputs: usa 'output_basename' si está, si no 'podcast_<slug>'."""
    if isinstance(output_basename, str) and output_basename.strip():
        return output_basename.strip()
    return f"podcast_{slug}"

# ---------------------------------------------------------------------
# Corrección de vocativos mal dirigidos (nuevo helper)
# ---------------------------------------------------------------------
def _fix_addressing(texto: str, orador: str) -> str:
    """
    Si el orador se dirige por nombre y usa su propio nombre (vocativo),
    corrige para que mencione al interlocutor (p. ej., 'Héctor,' dicho por Héctor -> 'Aura,').
    Solo tocamos usos vocativos (seguido de coma/pausa/puntuación), para no romper menciones narrativas.
    """
    yo = orador
    tu = _contraparte(orador)
    # Reemplazos de vocativo: "Héctor," / "Héctor:" / "Héctor?" / "Héctor!" / "Héctor …"
    patrones = [
        (rf"\b{re.escape(yo)}\s*([,，:;])", tu + r"\1"),
        (rf"([¿¡])\s*{re.escape(yo)}\s*([,，:;?!])", r"\1" + tu + r"\2"),
        # Caso al inicio de frase sin puntuación inmediata pero con espacio y minúscula/luego palabra
        (rf"^({re.escape(yo)})\s+(?=[a-záéíóúüñ])", tu + ", "),
    ]
    t = texto
    for pat, rep in patrones:
        t = re.sub(pat, rep, t)
    return t

# ---------------------------------------------------------------------
# Enriquecido emocional/pausas (post-proceso ligero)
# ---------------------------------------------------------------------

# --- Control estricto de emojis expresivos (para TTS) ---
ALLOWED_EMOJIS = {"😂","😍","😲","😏","😉","🙏","🔥"}  # solo caras/gestos que cambian prosodia

def _is_emoji_char(ch: str) -> bool:
    """Heurística simple: detecta la mayoría de emojis de los bloques U+1F300–U+1FAFF y símbolos misceláneos."""
    o = ord(ch)
    return (
        0x1F300 <= o <= 0x1FAFF or  # Misc Symbols & Pictographs / Supplemental Symbols & Pictographs
        0x2600 <= o <= 0x27BF or    # Misc symbols, Dingbats
        0x1F900 <= o <= 0x1F9FF     # Supplemental Symbols and Pictographs subset
    )

def _filtra_emojis(texto: str) -> str:
    """
    Elimina cualquier emoji que no esté en ALLOWED_EMOJIS.
    Mantiene texto normal y los emojis de la whitelist.
    """
    out_chars = []
    for ch in texto:
        if _is_emoji_char(ch) and ch not in ALLOWED_EMOJIS:
            # descarta emojis decorativos (🌍🌟🦖🦕, etc.)
            continue
        out_chars.append(ch)
    return "".join(out_chars)

def _limit_emoji_per_sentence(texto: str, max_per_sentence: int = 1) -> str:
    """
    Limita a 'max_per_sentence' emojis por frase.
    División heurística por . ! ? y nueva línea. Mantiene el primer emoji permitido y filtra el resto.
    """
    def _process_chunk(chunk: str) -> str:
        count = 0
        out = []
        for ch in chunk:
            if _is_emoji_char(ch):
                if ch in ALLOWED_EMOJIS and count < max_per_sentence:
                    out.append(ch)
                    count += 1
                else:
                    # drop emoji extra o no permitido
                    continue
            else:
                out.append(ch)
        return "".join(out)

    parts = re.split(r'([\.!?]\s+|\n+)', texto)  # conserva separadores
    processed = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            processed.append(_process_chunk(part))
        else:
            processed.append(part)  # separador
    return "".join(processed)

EMOJI_MAP = {
    r"\[riendo\]": " 😂 ",
    r"\[con entusiasmo\]": " 😍 ",
    r"\[sorprendido\]": " 😲 ",
    r"\[irónico\]": " 😏 ",
    r"\[con solemnidad\]": " 🙏 ",
    r"\[apasionado\]": " 🔥 ",
}

PALABRAS_CLAVE = {
    r"\b(increíbl[oa]s?|fascinant[ea]s?)\b": " 😍",
    r"\b(jajaj?a?|qué risa|me parto)\b": " 😂",
    r"\b(sorprendent[ea]s?|alucinant[ea]s?)\b": " 😲",
    r"\b(broma|chiste)\b": " 😉",
}

def _tiene_etiquetas_o_emojis(t: str) -> bool:
    return bool(re.search(r"\[[^\]]+\]", t) or re.search(r"[😂😍😲😏😉🙏🔥]", t))

def enriquecer_dialogo(texto: str) -> str:
    # 1) Si hay etiquetas, convertirlas en emojis (sin borrar el texto original)
    for patron, emoji in EMOJI_MAP.items():
        texto = re.sub(patron, lambda m: m.group(0) + emoji, texto, flags=re.IGNORECASE)

    # 2) Si NO hay etiquetas ni emojis, añade por palabras clave (ligero)
    if not _tiene_etiquetas_o_emojis(texto):
        def decora_linea(l):
            if any(e in l for e in ("😂","😍","😲","😏","😉","🙏","🔥")):
                return l
            for patron, emoji in PALABRAS_CLAVE.items():
                if re.search(patron, l, flags=re.IGNORECASE):
                    return re.sub(patron, lambda m: m.group(0) + emoji, l, count=1, flags=re.IGNORECASE)
            return l
        texto = "\n".join(decora_linea(l) for l in texto.splitlines())

    # 3) Pausas naturales tras interjecciones
    texto = re.sub(r"\b(eh|mmm|vale|ojo)\b(?=[,\.!\?]|\s|$)", r"\1…", texto, flags=re.IGNORECASE)
    # 4) Filtro de emojis: solo whitelist y 1 por frase
    texto = _filtra_emojis(texto)
    texto = _limit_emoji_per_sentence(texto, max_per_sentence=1)
    return texto

# ---------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------

def _sistema_global() -> str:
    formalidad = {
        "baja": "muy coloquial",
        "medio": "cercana y profesional",
        "alta": "cuidada y formal, pero sin rigidez"
    }.get(nivel_formalidad, "cercana y profesional")

    humor_line = {
        "bajo": "Humor muy sutil, ocasional. Evita ironía.",
        "medio": "Humor ligero y natural. Ironía muy medida.",
        "medio-alto": "Humor visible; chascarrillos puntuales. Ironía ligera permitida.",
        "alto": "Humor frecuente; ironía presente pero nunca ofensiva ni repetitiva."
    }.get(humor_nivel, "Humor ligero y natural. Ironía muy medida.")

    ironia_txt = "Permite ironía leve cuando aporte y no suene cruel." if permitir_ironia else "Evita ironía."
    refs_txt = "Puedes usar referencias pop/culturales cuando sumen." if referencias_pop else "Evita referencias pop salvo que sean imprescindibles."

    muletillas_txt = ""
    if muletillas_permitidas:
        muletillas_txt = f"Muletillas permitidas con mesura: {', '.join(sorted(muletillas_permitidas))}."

    estilo_extra = ""
    if estilo_dialogo_lines:
        estilo_extra = "\nDirectrices del tema:\n" + "\n".join(f"- {l}" for l in estilo_dialogo_lines)

    formato_extra = ""
    if formato_salida_lines:
        formato_extra = "\nContrato de salida (obligatorio):\n" + "\n".join(f"- {l}" for l in formato_salida_lines)
    else:
        # Contrato por defecto si no viene en config
        formato_extra = (
            "\nContrato de salida (obligatorio):\n"
            "- Cada intervención empieza implícitamente (sin prefijo) y NO debe llevar comillas.\n"
            "- Incluye etiquetas en corchetes cuando corresponda: [riendo], [con entusiasmo], [irónico], [con solemnidad], [susurrando].\n"
            "- Usa SOLO estos emojis expresivos (cambian la prosodia del TTS): 😂 😍 😲 😏 😉 🙏 🔥. Máximo 1 por frase. No uses otros (p. ej., 🌍🌟🦖🦕). \n"
            "- Frases cortas; usa '...' para pausas naturales.\n"
            "Ejemplos:\n"
            "Aura: [con entusiasmo] ¡Qué hallazgo! 😍 Imagina ver las huellas frescas marcadas en el barro...\n"
            "Héctor: [irónico, riendo] Vale… entonces el KFC es paleontología aplicada. 😂\n"
            "Aura: [con solemnidad] Más allá de las cifras… hay una historia de vida y extinción."
        )

    return (
        f"Guionista de un podcast a dos voces en español peninsular. "
        f"Participantes: {presentador} (presentador) y {entrevistado} (invitado).\n"
        f"Estilo: conversación {formalidad}, fluida, con personalidad y ritmo natural.\n"
        f"- {presentador}: {tono_hector}.\n"
        f"- {entrevistado}: {tono_aura}.\n"
        f"{humor_line} {ironia_txt} {refs_txt} {muletillas_txt}{estilo_extra}{formato_extra}\n"
        f"Realismo:\n"
        f"1) Frases con longitudes variadas; 2) Pausas [pausa]/[risas] muy ocasionales (≤10%); "
        f"3) Evita cerrar siempre con pregunta; 4) Cifras prudentes y marcadas como aproximadas; "
        f"5) Nada de disclaimers técnicos.\n"
        f"RESPONDE SOLO con el texto de la intervención, sin nombre ni comillas."
    )

def _longitud_objetivo() -> str:
    return LONGITUD_MAP.get(longitud_respuestas, LONGITUD_MAP["media"])

def _client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key)

def _llm_siguiente_linea(client: OpenAI, transcript: str, orador: str) -> str:
    instruccion = (
        f"Transcripción hasta ahora (formato 'Nombre: texto'):\n"
        f"{transcript}\n\n"
        f"Escribe SOLO la siguiente intervención de {orador} en {idioma}.\n"
        f"Longitud objetivo: {_longitud_objetivo()}.\n"
        f"Directrices: natural, específica, con etiquetas emocionales y emojis puntuales; "
        f"sin prefijos de nombre ni comillas. No repitas texto previo."
    )
    resp = client.chat.completions.create(
        model=modelo,
        temperature=temperatura,
        top_p=0.95,
        frequency_penalty=0.25,
        presence_penalty=0.0,
        messages=[
            {"role": "system", "content": _sistema_global()},
            {"role": "user", "content": instruccion}
        ]
    )
    texto = resp.choices[0].message.content.strip()
    texto = _quita_prefijo_orador(texto, orador)
    texto = _limpia_robotismos(texto)
    texto = _limpia_muletillas(texto, orador)
    # Corregir vocativos mal dirigidos (p. ej., Héctor diciéndose a sí mismo)
    texto = _fix_addressing(texto, orador)
    # NUEVO: enriquecer si faltan etiquetas/emojis/pausas
    texto = enriquecer_dialogo(texto)
    # Filtro final de seguridad (por si el modelo insistiera)
    texto = _limit_emoji_per_sentence(_filtra_emojis(texto), max_per_sentence=1)
    return texto


# ---------------------------------------------------------------------
# Helper: exportar segmentos JSON para TTS/subtítulos
# ---------------------------------------------------------------------
def _save_segments_json(outdir: str, basename: str, items: List[Tuple[str, str]]) -> str:
    """
    Exporta un JSON con segmentos para el nuevo pipeline TTS/subtítulos.
    Estructura: [{"speaker": "...", "text": "..."}...]
    - Mapea "COLD OPEN" -> "Narrator"
    - Aplica el filtro de emojis (solo los expresivos permitidos, máx. 1 por frase)
    """
    segs = []
    for who, txt in items:
        # Aseguramos el mismo postproceso anti-emoji decorativo que usamos al guardar
        clean = _limit_emoji_per_sentence(_filtra_emojis(txt), max_per_sentence=1)
        if who.upper() == "COLD OPEN":
            segs.append({"speaker": "Narrator", "text": clean})
        else:
            # Normaliza nombres por seguridad (acentos/variantes)
            wl = who.strip().lower()
            if wl.startswith("hec"):
                speaker = "Héctor"
            elif wl.startswith("aura"):
                speaker = "Aura"
            else:
                speaker = who.strip() or "Narrator"
            segs.append({"speaker": speaker, "text": clean})

    seg_path = os.path.join(outdir, f"{basename}_segments.json")
    with open(seg_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(segs, f, ensure_ascii=False, indent=2)
    return seg_path

# ---------------------------------------------------------------------
# Exportadores
# ---------------------------------------------------------------------

def _to_markdown(tema: str, items: List[Tuple[str, str]]) -> str:
    fecha = datetime.now().strftime("%Y-%m-%d")
    cabecera = f"# chIArlando — {tema}\n\n*Grabado: {fecha}*\n\n"
    cuerpo = "\n\n".join(f"**{orador}**: {texto}" for orador, texto in items)
    return cabecera + cuerpo + "\n"

def _to_txt(items: List[Tuple[str, str]]) -> str:
    return "\n".join(f"{orador}: {texto}" for orador, texto in items) + "\n"

def _to_srt(items: List[Tuple[str, str]]) -> str:
    """SRT aproximando tiempos por número de palabras (para fallback rápido)."""
    def fmt_ts(segundos: float) -> str:
        ms = int((segundos - int(segundos)) * 1000)
        s = int(segundos) % 60
        m = (int(segundos) // 60) % 60
        h = int(segundos) // 3600
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    srt = []
    t = 0.0
    idx = 1
    for (orador, texto) in items:
        palabras = max(1, len(re.findall(r"\w+", texto)))
        dur = max(2.0, palabras / 2.666)  # ≈160 wpm
        start = t
        end = t + dur
        bloque = f"{idx}\n{fmt_ts(start)} --> {fmt_ts(end)}\n{orador}: {texto}\n"
        srt.append(bloque)
        idx += 1
        t = end + 0.25
    return "\n".join(srt) + "\n"

def _ensure_outdir(slug: str) -> str:
    outdir = os.path.join("outputs", slug)
    os.makedirs(outdir, exist_ok=True)
    return outdir

def _guardar(tema: str, items: List[Tuple[str, str]], formato: str) -> str:
    # 0) NORMALIZADOR FINAL (garantiza que TODO lo que se guarda trae emojis/pausas si existen)
    items = _normalize_final_items(items)

    # Usar slug/basename configurables para no depender del valor visible de 'tema'
    slug = _get_output_slug()
    outdir = _ensure_outdir(slug)
    base = os.path.join(outdir, _get_output_basename(slug))

    # Diagnóstico: detectar carpeta legacy basada en slugify(tema) (sin crearla)
    legacy_slug = slugify(tema)
    legacy_outdir = os.path.join("outputs", legacy_slug)
    if legacy_slug != slug and os.path.isdir(legacy_outdir):
        print(f"{Fore.YELLOW}[AVISO]{Style.RESET_ALL} Existe carpeta legacy de outputs: '{legacy_outdir}'. Usando la configurada: '{outdir}'.")

    # 1) Render del contenido según formato
    if formato == "md":
        contenido = _to_markdown(tema, items)
        fname = base + ".md"
        encoding = "utf-8"        # MD: sin BOM
    elif formato == "srt":
        contenido = _to_srt(items)
        fname = base + ".srt"
        encoding = "utf-8"        # SRT: sin BOM
    else:
        contenido = _to_txt(items)
        fname = base + ".txt"
        # TXT: usa BOM por compat con visores quisquillosos (Windows Notepad clásico, etc.)
        use_bom = bool(config.get("txt_utf8_bom", True))
        encoding = "utf-8-sig" if use_bom else "utf-8"

    # 2) Escritura robusta (LF)
    with open(fname, "w", encoding=encoding, newline="\n") as f:
        f.write(contenido)

    # 2b) Exportar segmentos JSON para el pipeline (TTS/subs)
    try:
        _save_segments_json(outdir, os.path.basename(base), items)
    except Exception as e:
        print(f"{Fore.YELLOW}[WARN]{Style.RESET_ALL} No se pudo crear segments.json: {e}")

    # Debug: mostrar ruta y formato guardado
    print(f"{Fore.GREEN}[SAVE]{Style.RESET_ALL} Archivo guardado: {fname} (formato={formato}, slug={slug}, basename={os.path.basename(base)})")

    return fname

# ---------------------------------------------------------------------
# Conversación principal
# ---------------------------------------------------------------------

def _mensajes_base() -> dict:
    # Textos configurables con placeholders
    bienvenida_tpl   = textos.get("bienvenida", "¡Hola a todos y bienvenidos a un nuevo episodio de 'chIArlando'! Hoy el tema es **{tema}**. Tenemos a {entrevistado} con nosotros. ¡Bienvenido, {entrevistado}!")
    cierre_previo_tpl= textos.get("cierre_previo", "Ha sido una charla fantástica sobre **{tema}**. Antes de cerrar, {entrevistado}, ¿te gustaría dejar una última reflexión breve?")
    cierre_final_tpl = textos.get("cierre_final", "🎙️ Gracias por escucharnos. Si te ha gustado, compártelo y deja tu valoración. ¡Hasta la próxima!")
    # Render con variables actuales
    bienvenida = bienvenida_tpl.format(tema=tema, entrevistado=entrevistado, presentador=presentador)
    cierre_previo = cierre_previo_tpl.format(tema=tema, entrevistado=entrevistado, presentador=presentador)
    cierre_final = cierre_final_tpl.format(tema=tema, entrevistado=entrevistado, presentador=presentador)
    print(f"{Fore.CYAN}[TEXTOS]{Style.RESET_ALL} Plantillas activas: {list(textos.keys())}")
    return {
        "bienvenida": bienvenida,
        "cierre_previo": cierre_previo,
        "cierre_final": cierre_final
    }

def _generar_preguntas_si_faltan(client: OpenAI) -> List[str]:
    if preguntas_guia:
        return preguntas_guia

    prompt = (
        f"Propón 6–8 preguntas concretas y profundas sobre '{tema}' para una entrevista estilo 'The Wild Project'. "
        f"Mezcla ángulos: técnico, humano, práctica diaria, polémica respetuosa, futuro y ética. "
        f"Devuelve SOLO una lista, en {idioma}."
    )
    resp = client.chat.completions.create(
        model=modelo,
        temperature=0.8,
        top_p=0.95,
        frequency_penalty=0.25,
        presence_penalty=0.0,
        messages=[
            {"role": "system", "content": "Eres productor de podcasts: diseñas entrevistas potentes y memorables."},
            {"role": "user", "content": prompt}
        ]
    ).choices[0].message.content

    lineas = [l.strip(" -\t") for l in resp.splitlines() if l.strip()]
    candidatas = []
    for l in lineas:
        l = re.sub(r"^\d+[\).\s]+", "", l).strip()
        if len(l) > 8:
            candidatas.append(l)
    if not candidatas:
        candidatas = [
            f"¿Qué es lo más malentendido sobre {tema} y por qué?",
            f"Ponme un ejemplo real donde {tema} haya cambiado la vida o el negocio de alguien.",
            f"¿Qué riesgos ignoramos en {tema} y cómo los gestionas en la práctica?",
            f"Un consejo práctico y accionable para quien empieza en {tema}.",
            f"¿Cuál ha sido tu mayor cambio de opinión sobre {tema}?",
            f"¿Qué tendencia ves venir que casi nadie mira todavía?"
        ]
    return candidatas[:8]

def generar_podcast(api_key: str) -> str:
    if not api_key:
        raise ValueError("Falta OPENAI_API_KEY.")

    _dbg_resumen_config()  # Para verificar que sí está leyendo el formato, etc.

    dev_mode = modo.startswith("dev")
    if dev_mode:
        print(f"{Fore.YELLOW}[MODO DESARROLLO]{Style.RESET_ALL} Generación mínima para pruebas.")

    client = _client(api_key)
    base = _mensajes_base()
    guia = _generar_preguntas_si_faltan(client)
    if dev_mode:
        guia = guia[:1]  # solo la primera pregunta

    transcript: List[str] = []
    guion: List[Tuple[str, str]] = []

    # 1) Cold open
    if incluir_cold_open:
        instr = (
            f"Prepara un 'cold open' de 1–2 frases SOBRE el tema '{tema}'. "
            "Debe sonar intrigante y sugerente, pero concreto. "
            "Menciona explícitamente el tema y no cambies a otros ámbitos. "
            "No presentes a nadie aún. Evita clichés y evita cualquier referencia técnica a IA."
        )
        cold = client.chat.completions.create(
            model=modelo,
            temperature=0.9,
            top_p=0.95,
            frequency_penalty=0.25,
            presence_penalty=0.0,
            messages=[
                {"role": "system", "content": _sistema_global()},
                {"role": "user", "content": instr}
            ]
        ).choices[0].message.content.strip()
        cold = _limpia_robotismos(cold)
        cold = enriquecer_dialogo(cold)  # NUEVO
        if cold.endswith("?") and len(cold) > 120:
            cold = cold.rstrip(" ?") + "."
        print(f"\n{Fore.CYAN}[COLD OPEN]{Style.RESET_ALL} {cold}\n", flush=True)
        guion.append(("COLD OPEN", cold))

    # 2) Intro
    bienvenida = base["bienvenida"]
    print(f"\n{Fore.BLUE}{presentador}: {bienvenida}{Style.RESET_ALL}\n", flush=True)
    transcript.append(f"{presentador}: {bienvenida}")
    guion.append((presentador, bienvenida))

    # 3) Presentación invitado
    nota_intro = "\n\nNota: Es el primer turno del invitado. Preséntate brevemente y saluda a la audiencia."
    texto_aura = _llm_siguiente_linea(client, "\n".join(transcript) + nota_intro, entrevistado)
    print(f"\n{Fore.GREEN}{entrevistado}: {texto_aura}{Style.RESET_ALL}\n", flush=True)
    transcript.append(f"{entrevistado}: {texto_aura}")
    guion.append((entrevistado, texto_aura))

    # 4) Bloques principales: primero TODAS las preguntas_guia
    turnos_generados = 1  # ya respondió 1 vez (presentación)
    for pregunta in guia:
        if turnos_generados >= max_turnos:
            break

        # Héctor pregunta (de la guía)
        pregunta_directa = _recorta_preambulos_en_preguntas(pregunta.strip())
        if not pregunta_directa.endswith("?"):
            pregunta_directa = pregunta_directa.rstrip(".") + "?"
        pregunta_directa = enriquecer_dialogo(pregunta_directa)  # NUEVO (pausas sutiles)
        print(f"\n{Fore.BLUE}{presentador}: {pregunta_directa}{Style.RESET_ALL}\n", flush=True)
        transcript.append(f"{presentador}: {pregunta_directa}")
        guion.append((presentador, pregunta_directa))

        # Aura responde
        resp_aura = _llm_siguiente_linea(client, "\n".join(transcript), entrevistado)
        print(f"\n{Fore.GREEN}{entrevistado}: {resp_aura}{Style.RESET_ALL}\n", flush=True)
        transcript.append(f"{entrevistado}: {resp_aura}")
        guion.append((entrevistado, resp_aura))
        turnos_generados += 1

        # Seguimientos improvisados tras la respuesta de Aura
        seg_min, seg_max = preguntas_improvisadas
        if dev_mode:
            n_follow = 0
        else:
            n_follow = random.randint(seg_min, seg_max)
        for _ in range(n_follow):
            if turnos_generados >= max_turnos:
                break
            prompt_follow = (
                "\n".join(transcript)
                + "\n\nNota: formula UNA sola pregunta de seguimiento breve, incisiva y específica basada en la última respuesta."
            )
            follow = _llm_siguiente_linea(client, prompt_follow, presentador)
            follow = _recorta_preambulos_en_preguntas(follow)
            if not follow.strip().endswith("?"):
                follow = follow.rstrip(".") + "?"
            follow = enriquecer_dialogo(follow)  # NUEVO
            print(f"\n{Fore.BLUE}{presentador}: {follow}{Style.RESET_ALL}\n", flush=True)
            transcript.append(f"{presentador}: {follow}")
            guion.append((presentador, follow))

            # Respuesta de Aura
            resp_aura2 = _llm_siguiente_linea(client, "\n".join(transcript), entrevistado)
            print(f"\n{Fore.GREEN}{entrevistado}: {resp_aura2}{Style.RESET_ALL}\n", flush=True)
            transcript.append(f"{entrevistado}: {resp_aura2}")
            guion.append((entrevistado, resp_aura2))
            turnos_generados += 1

        if dev_mode:
            break  # solo un bloque principal en desarrollo

    # 5) Cierre
    cierre_previo = base["cierre_previo"]
    print(f"\n{Fore.MAGENTA}{presentador}: {cierre_previo}{Style.RESET_ALL}\n", flush=True)
    transcript.append(f"{presentador}: {cierre_previo}")
    guion.append((presentador, cierre_previo))

    reflexion = _llm_siguiente_linea(
        client,
        "\n".join(transcript) + f"\n\nNota: comparte una última reflexión sobre {tema}, cálida y breve.",
        entrevistado
    )
    print(f"\n{Fore.GREEN}{entrevistado}: {reflexion}{Style.RESET_ALL}\n", flush=True)
    transcript.append(f"{entrevistado}: {reflexion}")
    guion.append((entrevistado, reflexion))

    cierre_final = base["cierre_final"]
    if incluir_cierre_llamado:
        cta = textos.get("cta_cierre", "")
        if cta:
            cierre_final += cta.format(tema=tema, entrevistado=entrevistado, presentador=presentador)
    # Cierre estático ya trae emojis
    print(f"\n{Fore.MAGENTA}{presentador}: {cierre_final}{Style.RESET_ALL}\n", flush=True)
    transcript.append(f"{presentador}: {cierre_final}")
    guion.append((presentador, cierre_final))

    # 6) Guardado respetando formato_guardado (UTF-8)
    salida = ""
    if guardar_guion_flag:
        fname = _guardar(tema, guion, formato_guardado)
        print(f"\n{Fore.YELLOW}Guion guardado como {fname}{Style.RESET_ALL}")
        salida = fname

    return _to_txt(guion) if not salida else f"Archivo guardado: {salida}"


def _normalize_final_items(items: List[tuple]) -> List[tuple]:
    norm = []
    for (orador, texto) in items:
        t = enriquecer_dialogo(texto)  # último pase anti-planicie y pro-emoji
        t = _limit_emoji_per_sentence(_filtra_emojis(t), max_per_sentence=1)
        norm.append((orador, t))
    return norm