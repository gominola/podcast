# timeline_to_subs.py
# Genera SRT y ASS desde TXT/JSON:
# - Si los segmentos no traen start/end, sintetiza tiempos a partir de la longitud del texto (pipeline "desde TXT").
# - Estilos por orador, margenes y particionado de textos largos.

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, Any, List, Tuple

CONFIG_PATH = "config.json"

# -------------------------
# Utilidades de E/S
# -------------------------
def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))

def load_cfg() -> Dict[str, Any]:
    return read_json(Path(CONFIG_PATH)) if Path(CONFIG_PATH).exists() else {}

def slugify(texto: str) -> str:
    t = (texto or "").lower()
    t = re.sub(r"[^a-z0-9áéíóúüñ\s-]", "", t)
    t = re.sub(r"\s+", "-", t)
    return re.sub(r"-+", "-", t).strip("-")

def from_config_paths(cfg: Dict[str, Any]):
    slug = cfg.get("output_slug") or slugify(cfg.get("tema", "podcast"))
    base = cfg.get("output_basename") or slug
    outdir = Path("outputs") / slug
    # Acepta dos nombres de entrada: <base>.timeline.json o <base>_segments.json (solo speaker/text)
    timeline_path = outdir / f"{base}.timeline.json"
    if not timeline_path.exists():
        alt_path = outdir / f"{base}_segments.json"
        if alt_path.exists():
            timeline_path = alt_path
    return timeline_path, outdir / f"{base}.srt", outdir / f"{base}.ass"

# -------------------------
# Formatos de tiempo
# -------------------------
def fmt_srt_ts(sec: float) -> str:
    if sec < 0: sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02}:{m:02}:{s:06.3f}".replace(".", ",")

def fmt_ass_ts(sec: float) -> str:
    if sec < 0: sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        cs = 0; s += 1
        if s == 60:
            s = 0; m += 1
            if m == 60:
                m = 0; h += 1
    return f"{h}:{m:02}:{s:02}.{cs:02}"

# -------------------------
# Colores y estilos
# -------------------------
def hex_to_ass_color(hexstr: str) -> str:
    hexstr = (hexstr or "").strip().lstrip("#")
    if len(hexstr) != 6:
        return "&H00FFFFFF"
    rr = int(hexstr[0:2], 16)
    gg = int(hexstr[2:4], 16)
    bb = int(hexstr[4:6], 16)
    return f"&H00{bb:02X}{gg:02X}{rr:02X}"  # BGR + alpha 00

_emoji_re = re.compile(r"[\U00010000-\U0010FFFF]")

def strip_emojis(text: str) -> str:
    try:
        return _emoji_re.sub("", text)
    except re.error:
        return "".join(ch for ch in text if ord(ch) <= 0xFFFF)

# -------------------------
# Texto y normalización
# -------------------------
def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def norm_speaker(raw: str) -> str:
    if not raw:
        return "NARRATOR"
    r = strip_accents(str(raw)).upper().strip()
    r = r.rstrip(":").strip()
    if r.startswith("HECTOR") or r == "HÉCTOR":
        return "HECTOR"
    if r.startswith("AURA"):
        return "AURA"
    if r in {"NARRATOR","NARRADOR","COLD OPEN","COLD_OPEN","COLDOPEN","NARR","NARRATION"}:
        return "NARRATOR"
    return "NARRATOR"

def clean_text(raw_text: str) -> str:
    t = (raw_text or "")
    t = t.replace("**", "")  # quitar markdown
    t = re.sub(r"\s+", " ", t).strip(" ,")
    return t.strip()

def wrap_text_for_srt(text: str, max_len: int = 48, max_lines: int = 3) -> str:
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        add_len = len(w) + (1 if cur else 0)
        if len(cur) + add_len > max_len:
            if cur:
                lines.append(cur)
            cur = w
        else:
            cur = (cur + (" " if cur else "") + w)
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        # aumentar ancho si quedó a 4+ líneas
        return wrap_text_for_srt(" ".join(lines), max_len=max_len + 8, max_lines=max_lines)
    return "\n".join(lines)

# -------------------------
# Particionado de segmentos largos
# -------------------------
def split_segment(text: str, start: float, end: float, max_chars: int = 160) -> List[Tuple[str, float, float]]:
    text = text.strip()
    dur = max(0.1, end - start)
    if len(text) <= max_chars:
        return [(text, start, end)]

    # Dividir por puntuación fuerte cada ~40+ chars
    parts: List[str] = []
    buf: List[str] = []
    for ch in text:
        buf.append(ch)
        if ch in ".!?;:…" and len(buf) >= 40:
            parts.append("".join(buf).strip()); buf = []
    if buf:
        parts.append("".join(buf).strip())
    if len(parts) == 1:  # fallback por longitud fija
        step = max_chars
        parts = [text[i:i+step] for i in range(0, len(text), step)]

    total = sum(len(p) for p in parts) or 1
    acc = start
    out: List[Tuple[str, float, float]] = []
    for i, p in enumerate(parts):
        frac = len(p) / total
        sub_dur = max(0.6, dur * frac)
        st = acc
        en = start + dur if i == len(parts) - 1 else st + sub_dur
        out.append((p.strip(), st, en))
        acc = en
    return out

# -------------------------
# Síntesis de tiempos (pipeline desde TXT)
# -------------------------
def synthesize_times(segments: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Asigna start/end sintéticos secuenciales cuando no existen.
    Basado en velocidad de lectura (chars/seg) + pausas por puntuación.
    """
    cps = float(cfg.get("subtitle_chars_per_second", 12.0))  # ~180 wpm ~ 12 cps
    min_dur = float(cfg.get("subtitle_min_duration", 1.2))
    pause_strong = float(cfg.get("subtitle_pause_strong", 0.35))  # . ? ! …
    pause_soft = float(cfg.get("subtitle_pause_soft", 0.20))      # , ; :
    # Boost para preguntas/exclamaciones largas
    q_excl_boost = float(cfg.get("subtitle_q_excl_boost", 0.15))

    t = 0.0
    out = []
    for s in segments:
        text = clean_text(s.get("text",""))
        if not text:
            continue
        base = max(min_dur, len(text) / max(1.0, cps))

        # pausas internas (no afectan al habla real, pero dan aire visual)
        strongs = len(re.findall(r"[\.!\?…]", text))
        softs   = len(re.findall(r"[,;:]", text))
        dur = base + strongs * pause_strong + softs * pause_soft

        # si es claramente pregunta/exclamación larga, añade un poco
        if ("?" in text or "!" in text) and len(text) > 80:
            dur += q_excl_boost

        st = t
        en = st + dur
        s_out = dict(s)
        s_out["start"] = round(st, 3)
        s_out["end"]   = round(en, 3)
        out.append(s_out)
        t = en
    return out

# -------------------------
# Construcción SRT/ASS
# -------------------------
def build_from_timeline(timeline_or_segments: Path, srt: Path, ass: Path, cfg: Dict[str, Any]) -> None:
    raw = read_json(timeline_or_segments)

    # Acepta:
    #  a) {"segments":[{speaker,text,start,end},...]}
    #  b) [{speaker,text}, ...]  -> se sintetizan tiempos
    if isinstance(raw, dict):
        segs = raw.get("segments", [])
    else:
        segs = raw

    # Si no hay tiempos válidos, sintetizarlos (pipeline desde TXT)
    needs_synth = False
    checked = []
    for s in segs:
        st = s.get("start"); en = s.get("end")
        if not (isinstance(st,(int,float)) and isinstance(en,(int,float)) and en>st):
            needs_synth = True
        checked.append({"speaker": s.get("speaker",""), "text": s.get("text",""), "start": s.get("start"), "end": s.get("end")})
    if needs_synth:
        segs = synthesize_times(segs, cfg)

    # Estilos desde config
    font = cfg.get("subtitle_font", "Arial")
    fontsize = int(cfg.get("subtitle_fontsize", 64))
    margin_v = int(cfg.get("subtitle_margin_v", 70))
    margin_lr = int(cfg.get("subtitle_margin_lr", 200))
    outline = float(cfg.get("subtitle_outline", 2.0))
    shadow = int(cfg.get("subtitle_shadow", 1))

    use_colors = bool(cfg.get("use_speaker_colors", True))
    color_hector = hex_to_ass_color(cfg.get("color_hector", "#2EA8E6")) if use_colors else "&H00FFFFFF"
    color_aura   = hex_to_ass_color(cfg.get("color_aura",   "#FFD23F")) if use_colors else "&H00FFFFFF"
    color_base   = "&H00FFFFFF"

    max_line_chars = int(cfg.get("subtitle_max_chars_per_line", 48))
    max_seg_chars  = int(cfg.get("subtitle_max_chars_per_segment", 160))

    # Header ASS
    ass_header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,"
        " MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Base,{font},{fontsize},{color_base},{color_base},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{outline},{shadow},2,{margin_lr},{margin_lr},{margin_v},1\n"
        f"Style: Hector,{font},{fontsize},{color_hector},{color_hector},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{outline},{shadow},2,{margin_lr},{margin_lr},{margin_v},1\n"
        f"Style: Aura,{font},{fontsize},{color_aura},{color_aura},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{outline},{shadow},2,{margin_lr},{margin_lr},{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    srt_blocks: List[str] = []
    ass_events: List[str] = []
    idx = 1

    for s in segs:
        speaker = norm_speaker(s.get("speaker", "NARRATOR"))
        text = clean_text(s.get("text", ""))
        if not text:
            continue

        start = s.get("start"); end = s.get("end")
        if not (isinstance(start,(int,float)) and isinstance(end,(int,float)) and end>start):
            # En teoría no pasará porque sintetizamos, pero por si acaso
            continue

        # Particionar si es largo para mejorar sincronía y lectura
        subparts = split_segment(text, start, end, max_chars=max_seg_chars)

        for sub_text, sub_start, sub_end in subparts:
            srt_text = wrap_text_for_srt(sub_text, max_len=max_line_chars)
            srt_blocks.append(f"{idx}\n{fmt_srt_ts(sub_start)} --> {fmt_srt_ts(sub_end)}\n{srt_text}\n")

            style = "Base"
            if speaker == "HECTOR":
                style = "Hector"
            elif speaker == "AURA":
                style = "Aura"

            ass_safe = strip_emojis(srt_text)
            ass_text = ass_safe.replace("\n", "\\N")
            ass_events.append(
                f"Dialogue: 0,{fmt_ass_ts(sub_start)},{fmt_ass_ts(sub_end)},{style},,0,0,0,,{ass_text}"
            )
            idx += 1

    if not srt_blocks:
        print(f"⚠️  No se generaron eventos de subtítulos desde '{timeline_or_segments}'. "
              f"Verifica que haya texto en los segmentos.")
    srt.parent.mkdir(parents=True, exist_ok=True)
    srt.write_text("\n".join(srt_blocks), encoding="utf-8")
    ass.write_text(ass_header + "\n".join(ass_events), encoding="utf-8")

# -------------------------
# Main CLI
# -------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Genera SRT/ASS desde TXT/JSON (pipeline desde TXT)")
    ap.add_argument("--tema-from-config", action="store_true")
    ap.add_argument("--timeline", default=None, help="Ruta a .timeline.json o *_segments.json")
    ap.add_argument("--srt", default=None)
    ap.add_argument("--ass", default=None)
    args = ap.parse_args()

    cfg = load_cfg()
    if args.tema_from_config:
        timeline, srt, ass = from_config_paths(cfg)
    else:
        if not all([args.timeline, args.srt, args.ass]):
            raise SystemExit("❌ Debes indicar --timeline, --srt y --ass si no usas --tema-from-config")
        timeline, srt, ass = Path(args.timeline), Path(args.srt), Path(args.ass)

    build_from_timeline(timeline, srt, ass, cfg)
    print(f"✅ Subtítulos generados: {srt} + {ass}")