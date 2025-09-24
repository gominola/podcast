# srt_to_ass.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import json
import argparse
from pathlib import Path
from difflib import SequenceMatcher
from typing import List, Tuple

# --------------------------
# Utilidades de I/O config
# --------------------------
def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def bgr_from_hex(hex_str: str) -> str:
    # ASS usa BGR en formato &HAABBGGRR (sin alpha para PrimaryColour usamos &H00BBGGRR)
    h = hex_str.strip().lstrip("#")
    if len(h) == 3:  # #RGB -> #RRGGBB
        h = "".join(ch*2 for ch in h)
    rr = h[0:2]; gg = h[2:4]; bb = h[4:6]
    return f"&H00{bb.upper()}{gg.upper()}{rr.upper()}"

def load_style_config(cfg: dict) -> dict:
    return {
        "font": cfg.get("subtitle_font", "Arial"),
        "fontsize": int(cfg.get("subtitle_fontsize", 64)),
        "margin_v": int(cfg.get("subtitle_margin_v", 40)),
        "margin_lr": int(cfg.get("subtitle_margin_lr", 140)),
        "outline": float(cfg.get("subtitle_outline", 2.0)),
        "shadow": float(cfg.get("subtitle_shadow", 1.0)),
        "color_hector": bgr_from_hex(cfg.get("color_hector", "#2EA8E6")),
        "color_aura":   bgr_from_hex(cfg.get("color_aura",   "#FFD23F")),
        "use_colors": bool(cfg.get("use_speaker_colors", True)),
        "wrap_chars": int(cfg.get("subtitle_wrap_chars", 42)),
    }

# --------------------------
# Lectura SRT
# --------------------------
SRT_BLOCK = re.compile(
    r"\s*(\d+)\s+(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s+([\s\S]*?)(?=\n\s*\d+\s+\d{2}|\Z)",
    re.MULTILINE
)

def srt_time_to_ass(t: str) -> str:
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{int(ms)//10:02d}"

def parse_srt(path: Path) -> List[dict]:
    txt = path.read_text(encoding="utf-8", errors="replace")
    items = []
    for m in SRT_BLOCK.finditer(txt):
        idx = int(m.group(1))
        start = srt_time_to_ass(m.group(2))
        end   = srt_time_to_ass(m.group(3))
        text_raw = "\n".join(l.strip() for l in m.group(4).strip().splitlines() if l.strip())
        # Normaliza comas iniciales y basura
        text_raw = re.sub(r"^[,;:\s]+", "", text_raw)
        text_raw = text_raw.replace("\u200b", "")
        items.append({"idx": idx, "start": start, "end": end, "text": text_raw})
    return items

# --------------------------
# Lectura y limpieza guion
# --------------------------
EMOJI_RE = re.compile(
    r"([\U0001F300-\U0001FAFF\u2600-\u27BF])"
)
TAG_RE = re.compile(r"\[[^\]]+\]")

def strip_meta(s: str) -> str:
    s = TAG_RE.sub("", s)
    s = EMOJI_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s

def read_transcript_txt(txt_path: Path) -> List[Tuple[str, str]]:
    lines = []
    for raw in txt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip(): continue
        m = re.match(r"^([^:]+):\s*(.+)$", raw.strip())
        if not m: 
            # cold open u otros
            lines.append(("Narrador", strip_meta(raw.strip())))
        else:
            speaker = m.group(1).strip()
            text = strip_meta(m.group(2).strip())
            lines.append((speaker, text))
    return lines

# --------------------------
# Alineación SRT <- TXT
# --------------------------
def best_speaker_for_caption(caption: str, speaker_buf: dict[str, str]) -> str:
    """
    Decide cuál speaker (Héctor/Aura) se parece más al caption,
    usando similitud de subsecuencia sobre el buffer acumulado.
    """
    scores = {}
    for spk, buf in speaker_buf.items():
        if not buf: 
            scores[spk] = 0.0
        else:
            ratio = SequenceMatcher(None, caption.lower(), buf.lower()).ratio()
            scores[spk] = ratio
    # elige mejor
    winner = max(scores, key=lambda k: scores[k])
    return winner

def build_speaker_buffers(transcript: List[Tuple[str,str]], hector: str, aura: str) -> dict[str, str]:
    # Buffers concatenados de cada speaker (texto lineal) para matching aproximado
    h_text = " ".join(t for s,t in transcript if s.lower().startswith(hector.lower()))
    a_text = " ".join(t for s,t in transcript if s.lower().startswith(aura.lower()))
    return {hector: h_text, aura: a_text}

def consume_from_buffer(buffer: str, used_fragment: str) -> str:
    # tras asignar un caption, recorta el buffer a partir del final del fragmento usado (greedy)
    try:
        pos = buffer.lower().find(used_fragment[:32].lower())
        if pos >= 0:
            return buffer[pos+len(used_fragment):]
    except Exception:
        pass
    # si no encontramos fragmento, adelanta por longitud aproximada
    skip = max(0, min(len(buffer), int(len(used_fragment)*0.8)))
    return buffer[skip:]

def assign_speakers_to_srt(srt_items: List[dict], transcript: List[Tuple[str,str]], hector="Héctor", aura="Aura") -> List[str]:
    """
    Devuelve una lista de estilos ('Hector','Aura','Base') por cada item SRT.
    """
    # Narrador en los 5 primeros segundos por defecto (cold open)
    narrator_until_sec = 5.0

    def ass_time_to_sec(ts: str) -> float:
        h,m,rest = ts.split(":")
        s,cs = rest.split(".")
        return int(h)*3600 + int(m)*60 + int(s) + int(cs)/100.0

    styles = []
    buffers = build_speaker_buffers(transcript, hector, aura)
    last_style = "Hector"  # alternancia por si no hay match

    for it in srt_items:
        txt_clean = strip_meta(it["text"])
        if ass_time_to_sec(it["start"]) < narrator_until_sec:
            styles.append("Base")
            continue

        # heurística: si caption contiene el nombre del otro (poco común), úsalo
        low = txt_clean.lower()
        if hector.lower() in low and aura.lower() not in low:
            sty = "Hector"
        elif aura.lower() in low and hector.lower() not in low:
            sty = "Aura"
        else:
            winner = best_speaker_for_caption(txt_clean, buffers)
            if winner.lower().startswith(hector.lower()):
                sty = "Hector"
                buffers[hector] = consume_from_buffer(buffers[hector], txt_clean)
            elif winner.lower().startswith(aura.lower()):
                sty = "Aura"
                buffers[aura] = consume_from_buffer(buffers[aura], txt_clean)
            else:
                # fallback: alterna
                sty = "Aura" if last_style == "Hector" else "Hector"

        styles.append(sty)
        last_style = sty
    return styles

# --------------------------
# Partir líneas largas
# --------------------------
def wrap_ass(text: str, max_chars_per_line: int) -> str:
    # Inserta \N para envolver por palabras
    words = text.split()
    out_lines = []
    cur = []
    cur_len = 0
    for w in words:
        add = (len(w) + (1 if cur else 0))
        if cur_len + add > max_chars_per_line and cur:
            out_lines.append(" ".join(cur))
            cur = [w]; cur_len = len(w)
        else:
            cur.append(w); cur_len += add
    if cur:
        out_lines.append(" ".join(cur))
    # máximo 3 líneas
    if len(out_lines) > 3:
        # compacta suavemente
        merged = " ".join(out_lines)
        return wrap_ass(merged, int(max_chars_per_line*1.2))
    return "\\N".join(out_lines)

# --------------------------
# Escribir ASS
# --------------------------
def write_ass(srt_items: List[dict], styles_by_item: List[str], cfg_styles: dict, out_ass: Path):
    f = []
    f.append("[Script Info]")
    f.append("ScriptType: v4.00+")
    f.append("WrapStyle: 2")
    f.append("ScaledBorderAndShadow: yes")
    f.append("PlayResX: 1920")
    f.append("PlayResY: 1080")
    f.append("")
    f.append("[V4+ Styles]")
    f.append("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
             "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
             "Alignment, MarginL, MarginR, MarginV, Encoding")
    base = f"Style: Base,{cfg_styles['font']},{cfg_styles['fontsize']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{cfg_styles['outline']},{cfg_styles['shadow']},2,{cfg_styles['margin_lr']},{cfg_styles['margin_lr']},{cfg_styles['margin_v']},1"
    hec  = f"Style: Hector,{cfg_styles['font']},{cfg_styles['fontsize']},{cfg_styles['color_hector']},{cfg_styles['color_hector']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{cfg_styles['outline']},{cfg_styles['shadow']},2,{cfg_styles['margin_lr']},{cfg_styles['margin_lr']},{cfg_styles['margin_v']},1"
    aur  = f"Style: Aura,{cfg_styles['font']},{cfg_styles['fontsize']},{cfg_styles['color_aura']},{cfg_styles['color_aura']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{cfg_styles['outline']},{cfg_styles['shadow']},2,{cfg_styles['margin_lr']},{cfg_styles['margin_lr']},{cfg_styles['margin_v']},1"
    f.append(base)
    f.append(hec)
    f.append(aur)
    f.append("")
    f.append("[Events]")
    f.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
    for it, sty in zip(srt_items, styles_by_item):
        txt = wrap_ass(it["text"], cfg_styles["wrap_chars"])
        # Seguridad: quitar comas iniciales otra vez
        txt = re.sub(r"^[,;:\s]+", "", txt)
        style_name = sty if cfg_styles["use_colors"] else "Base"
        f.append(f"Dialogue: 0,{it['start']},{it['end']},{style_name},,0,0,0,,{txt}")

    out_ass.write_text("\n".join(f) + "\n", encoding="utf-8")

# --------------------------
# CLI
# --------------------------
def main():
    ap = argparse.ArgumentParser(description="Convierte SRT -> ASS con colores por orador (alineando con TXT).")
    ap.add_argument("--srt", required=True, help="Ruta del SRT (Whisper)")
    ap.add_argument("--txt", required=True, help="Ruta del transcript TXT (guion)")
    ap.add_argument("--config", default="config.json", help="Config para estilos y colores")
    ap.add_argument("--out", required=True, help="Ruta del ASS de salida")
    args = ap.parse_args()

    srt_path = Path(args.srt).resolve()
    txt_path = Path(args.txt).resolve()
    cfg = read_json(Path(args.config))
    styles_cfg = load_style_config(cfg)

    if not srt_path.exists():
        raise SystemExit(f"❌ No existe SRT: {srt_path}")
    if not txt_path.exists():
        raise SystemExit(f"❌ No existe TXT: {txt_path}")

    hector = cfg.get("presentador", "Héctor")
    aura   = cfg.get("entrevistado", "Aura")

    srt_items = parse_srt(srt_path)
    transcript = read_transcript_txt(txt_path)
    styles = assign_speakers_to_srt(srt_items, transcript, hector=hector, aura=aura)

    out_ass = Path(args.out).resolve()
    write_ass(srt_items, styles, styles_cfg, out_ass)
    print(f"✅ ASS con colores: {out_ass}")

if __name__ == "__main__":
    main()