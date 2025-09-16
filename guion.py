# -*- coding: utf-8 -*-
"""
Generador de guiones de podcast a dos voces (estilo conversación realista).

Características clave:
- Config base + fusión con archivo de tema: /temas/<slug(tema)>.json
- Preguntas guía por tema; si faltan, se generan 6–8 automáticamente
- Cold open coherente con el tema
- Anti-robotismos y anti-muletillas (adiós a “gran pregunta”, “impresionante, Aura”, etc.)
- Seguimientos improvisados directos (recorte de “¿podrías/puedes…?”)
- Comentarios de transición del presentador conectados a la siguiente pregunta (si no conectan, se omiten)
- Exportación en .md/.txt/.srt (con tiempos aproximados)
- Colores en terminal (colorama)
- OpenAI SDK con fallback de import
"""

from __future__ import annotations

# SDK compat: intenta usar `from openai import OpenAI`, cae a `openai.OpenAI` si no existe.
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
from colorama import Fore, Style, init

# Inicializa colorama para colores en la consola
init(autoreset=True)

# -------------------------
# Configuración y utilidades
# -------------------------

CONFIG_PATH = "config.json"

DEFAULT_CONFIG = {
    "tema": "El universo",
    "presentador": "Héctor",
    "entrevistado": "Aura",
    "idioma": "es",
    "tono_hector": "curioso, directo, incisivo; evita cumplidos y el nombre del invitado salvo lo imprescindible",
    "tono_aura": "clara, concreta; ejemplos breves y analogías sencillas; cero peloteo",
    "nivel_formalidad": "medio",                # baja | medio | alta
    "longitud_respuestas": "media",             # corta | media | larga
    "guardar_guion": True,
    "formato_guardado": "md",                   # md | txt | srt
    "preguntas_guia": [],                       # si vacío, se generan 6–8
    "preguntas_improvisadas": [1, 2],           # rango [min, max]
    "modelo": "gpt-4o-mini",
    "temperatura": 0.85,
    "semilla": None,
    "max_turnos": 12,                           # respuestas de invitado (aprox. duración)
    "incluir_cold_open": True,                  # breve gancho antes de la intro
    "incluir_cierre_llamado": True,             # CTA final breve
    # “knobs” de estilo ampliados
    "humor_nivel": "bajo",                      # bajo|medio|medio-alto|alto
    "permitir_ironia": False,
    "referencias_pop": False,
    "muletillas_permitidas": [],                # e.g., ["vale","ojo","tío"]
    # NUEVO: el estilo de diálogo se define en el JSON del tema
    "estilo_dialogo": []                        # array de líneas; se concatena en _sistema_global()
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

# Frases vacías o peloteo que suenan artificiales
MULETILLAS_INICIO = re.compile(
    r"^(gran pregunta|buena pregunta|excelente cuestión|me encanta que (me )?preguntes|"
    r"gracias por (la|tu) pregunta|como bien dices|efectivamente|sin duda|por supuesto|"
    r"queridos oyentes|estimados oyentes|hola a todos|hola a todas)\b[:,]?\s*",
    re.IGNORECASE
)

MULETILLAS_GENERICAS = [
    "impresionante",
    "fascinante",
    "increíble",
    "es muy interesante",
    "es súper interesante",
    "sin lugar a dudas",
    "debo decir que",
    "me gustaría decir que",
    "la verdad es que",
]

def slugify(texto: str) -> str:
    t = texto.lower()
    t = re.sub(r"[^a-z0-9áéíóúüñ\s-]", "", t)
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"-+", "-", t).strip("-")
    return t

def _limpia_robotismos(texto: str) -> str:
    t = texto.strip()
    for ban in ROBOTISMO_BANLIST:
        if ban.lower() in t.lower():
            t = re.sub(re.escape(ban), "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")
    # reduce uso excesivo de paréntesis largos
    t = re.sub(r"\s*\((?:[^)]{0,80})\)", lambda m: "" if len(m.group(0)) > 40 else m.group(0), t)
    return t

def _contraparte(orador: str) -> str:
    return presentador if orador == entrevistado else entrevistado

def _limpia_muletillas(texto: str, orador: str) -> str:
    t = texto.strip()
    # 1) Quita “gran pregunta”, “me encanta que me preguntes”, etc.
    t = MULETILLAS_INICIO.sub("", t)
    # 2) Quita nombre al inicio 'Héctor,' / 'Aura,'
    otro = _contraparte(orador)
    t = re.sub(rf"^({re.escape(otro)})\s*,\s*", "", t)
    # 3) Quita nombre intercalado '..., Héctor,' para evitar peloteo
    t = re.sub(rf"\b({re.escape(otro)})\s*,", "", t)
    # 4) Elimina elogios vacíos si abren la frase
    for m in MULETILLAS_GENERICAS:
        t = re.sub(rf"^(?:{re.escape(m)})[, ]+\s*", "", t, flags=re.IGNORECASE)
    # 5) Reduce muletillas coloquiales en arranque (respeta las permitidas)
    arranque = re.compile(r"^(oye|mira|bueno|pues|a ver)\s*,\s*", re.IGNORECASE)
    if not any(t.lower().startswith(m.lower()) for m in muletillas_permitidas):
        t = arranque.sub("", t)
    # 6) Limpieza general
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")
    return t

def _recorta_preambulos_en_preguntas(t: str) -> str:
    """En preguntas del presentador, elimina '¿podrías/puedes/te parece si...' para sonar más directo."""
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

def _ruta_tema(tema: str) -> str:
    base_dir = os.path.join(os.path.dirname(__file__), "temas")
    return os.path.join(base_dir, f"{slugify(tema)}.json")

def _cargar_config_tema(tema: str) -> dict:
    """Carga la configuración completa del tema si existe; si no, {}."""
    ruta = _ruta_tema(tema)
    try:
        if os.path.exists(ruta):
            with open(ruta, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"{Fore.YELLOW}Aviso: no se pudo cargar el tema '{tema}': {e}{Style.RESET_ALL}")
    return {}

def cargar_configuracion() -> dict:
    """Carga config base + config.json + tema/<slug>.json (tema sobrescribe)."""
    cfg = DEFAULT_CONFIG.copy()
    # Carga config.json
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as file:
                incoming = json.load(file)
                if isinstance(incoming, dict):
                    cfg.update(incoming)
        except Exception as e:
            print(f"{Fore.YELLOW}Aviso: no se pudo cargar config.json ({e}). Usando valores por defecto.{Style.RESET_ALL}")

    # Carga tema y fusiona
    tema_sel = cfg.get("tema", DEFAULT_CONFIG["tema"])
    cfg_tema = _cargar_config_tema(tema_sel)
    if cfg_tema:
        # Claves permitidas para sobrescribir
        permitidas = set(DEFAULT_CONFIG.keys()) | {
            "humor_nivel", "permitir_ironia", "referencias_pop",
            "muletillas_permitidas", "preguntas_guia"
        }
        for k, v in cfg_tema.items():
            if k in permitidas:
                cfg[k] = v

    # Normaliza preguntas_improvisadas
    pi = cfg.get("preguntas_improvisadas", [1, 2])
    if isinstance(pi, int):
        cfg["preguntas_improvisadas"] = [max(0, pi), max(0, pi)]
    elif isinstance(pi, (list, tuple)) and len(pi) == 2:
        cfg["preguntas_improvisadas"] = [max(0, int(pi[0])), max(0, int(pi[1]))]
    else:
        cfg["preguntas_improvisadas"] = [1, 2]

    # Normaliza formato
    cfg["formato_guardado"] = str(cfg.get("formato_guardado", "md")).lower()
    return cfg

config = cargar_configuracion()

# Variables principales (después de fusionar tema)
tema = config.get("tema")
presentador = config.get("presentador")
entrevistado = config.get("entrevistado")
idioma = config.get("idioma")
tono_hector = config.get("tono_hector")
tono_aura = config.get("tono_aura")
nivel_formalidad = config.get("nivel_formalidad")
longitud_respuestas = config.get("longitud_respuestas")
guardar_guion_flag = config.get("guardar_guion")
formato_guardado = config.get("formato_guardado")
preguntas_guia = list(config.get("preguntas_guia"))
preguntas_improvisadas = config.get("preguntas_improvisadas")
modelo = config.get("modelo")
temperatura = float(config.get("temperatura"))
semilla = config.get("semilla")
max_turnos = int(config.get("max_turnos"))
incluir_cold_open = bool(config.get("incluir_cold_open"))
incluir_cierre_llamado = bool(config.get("incluir_cierre_llamado"))

# “knobs” extra
humor_nivel = config.get("humor_nivel", "bajo")
permitir_ironia = bool(config.get("permitir_ironia", False))
referencias_pop = bool(config.get("referencias_pop", False))
muletillas_permitidas = set(config.get("muletillas_permitidas", []))
# NUEVO: leemos estilo_dialogo como lista de líneas (del JSON del tema)
estilo_dialogo_lines = config.get("estilo_dialogo", [])
if not isinstance(estilo_dialogo_lines, list):
    estilo_dialogo_lines = []

if semilla is not None:
    random.seed(semilla)

# -------------------------
# Prompting y generación
# -------------------------

def _sistema_global() -> str:
    """Reglas de estilo para conversación realista, adaptadas al tema."""
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

    return (
        f"Guionista de un podcast a dos voces en español peninsular. "
        f"Participantes: {presentador} (presentador) y {entrevistado} (invitado).\n"
        f"Estilo: conversación {formalidad}, fluida, con personalidad y ritmo natural.\n"
        f"- {presentador}: {tono_hector}.\n"
        f"- {entrevistado}: {tono_aura}.\n"
        f"{humor_line} {ironia_txt} {refs_txt} {muletillas_txt}{estilo_extra}\n"
        f"Realismo:\n"
        f"1) Frases con longitudes variadas; 2) Pausas [pausa]/[risas] muy ocasionales (≤10%); "
        f"3) Evita cerrar siempre con pregunta; 4) Cifras prudentes y marcadas como aproximadas; "
        f"5) Nada de disclaimers técnicos.\n"
        f"\nProhibido: arrancar con 'gran pregunta', 'me encanta que me preguntes', 'como bien dices', "
        f"o repetir el nombre del interlocutor salvo lo imprescindible. Evita halagos directos y fórmulas de relleno. "
        f"Prefiere frases declarativas y ejemplos concretos frente a discursos grandilocuentes.\n"
        f"RESPONDE SOLO con el texto de la intervención, sin nombre ni comillas."
    )

def _longitud_objetivo() -> str:
    return LONGITUD_MAP.get(longitud_respuestas, LONGITUD_MAP["media"])

def _client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key)

def _llm_siguiente_linea(client: OpenAI, transcript: str, orador: str) -> str:
    """Pide al modelo SOLO la siguiente línea de un orador concreto y la limpia."""
    instruccion = (
        f"Transcripción hasta ahora (formato 'Nombre: texto'):\n"
        f"{transcript}\n\n"
        f"Escribe SOLO la siguiente intervención de {orador} en {idioma}.\n"
        f"Longitud objetivo: {_longitud_objetivo()}.\n"
        f"Directrices: natural, específica, sin relleno, sin prefijos de nombre ni comillas. "
        f"No repitas texto previo."
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
    return texto

# --- Helpers de validación de transición (para comentarios puente de Héctor) ---

_STOPWORDS = {
    "el","la","los","las","un","una","unos","unas","de","del","al","a","y","o","u",
    "que","como","con","por","para","en","es","son","se","su","sus","lo","suelo",
    "si","no","ya","más","mas","muy","esto","esta","estas","estos","ese","esa",
    "sobre","entre","hasta","desde","cuando","donde","qué","cuál","cuáles",
    "cuanto","cuánta","cuántos","cuántas","porque","porqué"
}

def _keywords(text: str) -> set:
    t = re.sub(r"[^a-záéíóúüñ0-9\s]", " ", text.lower())
    toks = [w for w in t.split() if len(w) >= 3 and w not in _STOPWORDS]
    return set(toks)

def _overlap(a: str, b: str) -> float:
    ka, kb = _keywords(a), _keywords(b)
    if not ka or not kb:
        return 0.0
    inter = len(ka & kb)
    union = len(ka | kb)
    return inter / union

def _limita_a_dos_frases(t: str) -> str:
    partes = re.split(r"(?<=[.!?])\s+", t.strip())
    return " ".join(partes[:2]).strip()

def _asegura_declarativa(t: str) -> str:
    t = t.strip()
    if t.endswith("?"):
        t = t.rstrip(" ?") + "."
    return t

# -------------------------
# Exportación
# -------------------------

def _to_markdown(items: List[Tuple[str, str]]) -> str:
    fecha = datetime.now().strftime("%Y-%m-%d")
    cabecera = f"# chIArlando — {tema}\n\n*Grabado: {fecha}*\n\n"
    cuerpo = "\n\n".join(f"**{orador}**: {texto}" for orador, texto in items)
    return cabecera + cuerpo + "\n"

def _to_txt(items: List[Tuple[str, str]]) -> str:
    return "\n".join(f"{orador}: {texto}" for orador, texto in items) + "\n"

def _to_srt(items: List[Tuple[str, str]]) -> str:
    """Convierte [(orador, texto)] a SRT aproximando tiempos por número de palabras."""
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
        dur = max(2.0, palabras / 2.666)  # segundos (≈160 wpm)
        start = t
        end = t + dur
        bloque = f"{idx}\n{fmt_ts(start)} --> {fmt_ts(end)}\n{orador}: {texto}\n"
        srt.append(bloque)
        idx += 1
        t = end + 0.25  # pequeña separación
    return "\n".join(srt) + "\n"

def _guardar(tema: str, items: List[Tuple[str, str]], formato: str) -> str:
    base = f"podcast_{slugify(tema)}"
    if formato == "md":
        contenido = _to_markdown(items)
        fname = base + ".md"
    elif formato == "srt":
        contenido = _to_srt(items)
        fname = base + ".srt"
    else:
        contenido = _to_txt(items)
        fname = base + ".txt"

    with open(fname, "w", encoding="utf-8") as f:
        f.write(contenido)
    return fname

# -------------------------
# Conversación principal
# -------------------------

def _mensajes_base() -> dict:
    return {
        "bienvenida": (
            f"¡Hola a todos y bienvenidos a un nuevo episodio de 'chIArlando'! "
            f"Hoy el tema es **{tema}**. Tenemos a {entrevistado} con nosotros. ¡Bienvenido, {entrevistado}!"
        ),
        "cierre_previo": (
            f"Ha sido una charla fantástica sobre **{tema}**. "
            f"Antes de cerrar, {entrevistado}, ¿te gustaría dejar una última reflexión breve?"
        ),
        "cierre_final": "🎙️ Gracias por escucharnos. Si te ha gustado, compártelo y deja tu valoración. ¡Hasta la próxima!"
    }

def _generar_preguntas_si_faltan(client: OpenAI) -> List[str]:
    """
    Prioridad:
    1) preguntas_guia del tema (ya fusionadas en `config`)
    2) generación automática (6–8)
    """
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
    """Genera el guion del podcast con turnos alternos y estilo realista."""
    if not api_key:
        raise ValueError("API Key de OpenAI no encontrada. Pásala a generar_podcast(api_key).")

    client = _client(api_key)
    base = _mensajes_base()

    # 1) Plan si faltan preguntas
    guia = _generar_preguntas_si_faltan(client)

    transcript: List[str] = []
    guion: List[Tuple[str, str]] = []

    # 2) Cold open (gancho breve)
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
        cold = cold.strip()
        if cold.endswith("?") and len(cold) > 120:
            cold = cold.rstrip(" ?") + "."
        print(f"\n{Fore.CYAN}[COLD OPEN]{Style.RESET_ALL} {cold}\n", flush=True)
        guion.append(("COLD OPEN", cold))

    # 3) Introducción del presentador
    bienvenida = base["bienvenida"]
    print(f"\n{Fore.BLUE}{presentador}: {bienvenida}{Style.RESET_ALL}\n", flush=True)
    transcript.append(f"{presentador}: {bienvenida}")
    guion.append((presentador, bienvenida))

    # 4) Presentación del invitado
    nota_intro = (
        f"\n\nNota: Es el primer turno de {entrevistado}. "
        f"Preséntate brevemente y saluda a la audiencia."
    )
    texto_aura = _llm_siguiente_linea(client, "\n".join(transcript) + nota_intro, entrevistado)
    print(f"\n{Fore.GREEN}{entrevistado}: {texto_aura}{Style.RESET_ALL}\n", flush=True)
    transcript.append(f"{entrevistado}: {texto_aura}")
    guion.append((entrevistado, texto_aura))

    # 5) Bloques principales
    turnos_generados = 0
    for pregunta in guia:
        if turnos_generados >= max_turnos:
            break

        # Héctor pregunta
        print(f"\n{Fore.BLUE}{presentador}: {pregunta}{Style.RESET_ALL}\n", flush=True)
        transcript.append(f"{presentador}: {pregunta}")
        guion.append((presentador, pregunta))

        # Aura responde
        resp_aura = _llm_siguiente_linea(client, "\n".join(transcript), entrevistado)
        print(f"\n{Fore.GREEN}{entrevistado}: {resp_aura}{Style.RESET_ALL}\n", flush=True)
        transcript.append(f"{entrevistado}: {resp_aura}")
        guion.append((entrevistado, resp_aura))
        turnos_generados += 1

        # Seguimientos improvisados
        seg_min, seg_max = preguntas_improvisadas
        n_follow = random.randint(seg_min, seg_max)
        for _ in range(n_follow):
            if turnos_generados >= max_turnos:
                break
            prompt_follow = (
                "\n".join(transcript)
                + "\n\nNota: formula UNA sola pregunta de seguimiento breve, incisiva y específica basada en la última respuesta."
            )
            follow = _llm_siguiente_linea(client, prompt_follow, presentador)
            if not follow.strip().endswith(("?", "¿")):
                follow = follow.rstrip(".") + "?"
            follow = _limpia_muletillas(follow, presentador)
            follow = _recorta_preambulos_en_preguntas(follow)
            print(f"\n{Fore.BLUE}{presentador}: {follow}{Style.RESET_ALL}\n", flush=True)
            transcript.append(f"{presentador}: {follow}")
            guion.append((presentador, follow))

            # Respuesta de Aura
            resp_aura2 = _llm_siguiente_linea(client, "\n".join(transcript), entrevistado)
            print(f"\n{Fore.GREEN}{entrevistado}: {resp_aura2}{Style.RESET_ALL}\n", flush=True)
            transcript.append(f"{entrevistado}: {resp_aura2}")
            guion.append((entrevistado, resp_aura2))
            turnos_generados += 1

        # Comentario breve del presentador (≈50% prob), conectado a la SIGUIENTE pregunta
        if random.random() < 0.5 and turnos_generados < max_turnos:
            # Averigua la siguiente pregunta del guion, si la hay
            try:
                idx_actual = guia.index(pregunta)
            except ValueError:
                idx_actual = -1
            prox_pregunta = guia[idx_actual + 1] if 0 <= idx_actual < len(guia) - 1 else ""

            if prox_pregunta:
                prompt_puente = (
                    "\n".join(transcript)
                    + "\n\nNota: Escribe UN comentario de transición (1 frase, máx 2) "
                      "que conecte naturalmente lo que se acaba de decir con ESTA próxima pregunta, "
                      "sin formular preguntas, sin repetir la pregunta ni adelantar su contenido textual. "
                      "Debe sonar orgánico, breve y declarativo. Próxima pregunta: «"
                      + prox_pregunta + "»"
                )
                comentario = _llm_siguiente_linea(client, prompt_puente, presentador)
                comentario = _limpia_muletillas(comentario, presentador)
                comentario = _asegura_declarativa(comentario)
                comentario = _limita_a_dos_frases(comentario)

                # Valida que el comentario realmente conecte con la próxima pregunta (solapamiento mínimo)
                if _overlap(comentario, prox_pregunta) >= 0.15:
                    print(f"\n{Fore.YELLOW}{presentador}: {comentario}{Style.RESET_ALL}\n", flush=True)
                    transcript.append(f"{presentador}: {comentario}")
                    guion.append((presentador, comentario))
                # Si no conecta, lo omitimos en silencio.

    # 6) Cierre
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
        cierre_final += " Síguenos y cuéntanos qué te gustaría escuchar la próxima vez."
    print(f"\n{Fore.MAGENTA}{presentador}: {cierre_final}{Style.RESET_ALL}\n", flush=True)
    transcript.append(f"{presentador}: {cierre_final}")
    guion.append((presentador, cierre_final))

    # 7) Guardado
    salida = ""
    if guardar_guion_flag:
        fname = _guardar(tema, guion, formato_guardado)
        print(f"\n{Fore.YELLOW}Guion guardado como {fname}{Style.RESET_ALL}")
        salida = fname

    # Devuelve el transcript en texto plano además de guardar
    return _to_txt(guion) if not salida else f"Archivo guardado: {salida}"m