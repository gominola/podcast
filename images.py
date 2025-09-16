# -*- coding: utf-8 -*-
"""
Descarga imágenes libres a outputs/<slug>/imagenes
- Wikimedia Commons
- Wikipedia (ES/EN) fallback
- Priorización de diagramas/ilustraciones para temas científicos
"""
from __future__ import annotations
import os
import re
import json
import argparse
import unicodedata
from typing import List

import requests

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIPEDIA_API_ES = "https://es.wikipedia.org/w/api.php"
WIKIPEDIA_API_EN = "https://en.wikipedia.org/w/api.php"
TIMEOUT = 25

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "podcast-generator/1.0 (https://github.com/tu-usuario; contacto@example.com)"
})

def slugify(text: str) -> str:
    t = text.lower()
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9\s-]", "", t)
    t = re.sub(r"\s+", "-", t).strip("-")
    t = re.sub(r"-+", "-", t)
    return t

def tema_from_config(path: str = "config.json") -> str:
    if not os.path.exists(path):
        return "El universo"
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("tema", "El universo")

def _commons_query(q: str, limit: int) -> List[str]:
    params = {
        "action": "query", "format": "json",
        "generator": "search", "gsrsearch": q, "gsrnamespace": 6, "gsrlimit": min(limit*2, 50),
        "prop": "imageinfo", "iiprop": "url|mime|size", "iiurlwidth": 1920, "origin": "*"
    }
    r = SESSION.get(COMMONS_API, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    pages = data.get("query", {}).get("pages", {})
    out = []
    for _, p in pages.items():
        ii = p.get("imageinfo", [])
        if not ii:
            continue
        info = ii[0]
        url = info.get("thumburl") or info.get("url")
        mime = info.get("mime", "")
        if url and mime.startswith("image/"):
            out.append(url)
    return out

def _wiki_thumbs(api_url: str, q: str, limit: int) -> List[str]:
    params = {
        "action": "query", "format": "json",
        "generator": "search", "gsrsearch": q, "gsrlimit": min(limit, 40),
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 1920, "origin": "*"
    }
    r = SESSION.get(api_url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    pages = data.get("query", {}).get("pages", {})
    out = []
    for _, p in pages.items():
        th = p.get("thumbnail", {})
        src = th.get("source")
        if src:
            out.append(src)
    return out[:limit]

def search_images_multi(tema: str, limit: int) -> List[str]:
    tema_low = tema.lower()
    base_queries = [tema]

    if any(k in tema_low for k in ["cuant", "quantum", "física", "fisica"]):
        base_queries += [
            "mecánica cuántica", "física cuántica", "doble rendija",
            "patrón de interferencia", "diagrama átomo", "diagrama feynman",
            "quantum mechanics", "double slit experiment", "interference pattern",
            "atom diagram", "bohr model", "photon", "electron diffraction", "wave function"
        ]
    else:
        base_queries += [f"{tema} diagram", f"{tema} illustration", f"{tema} science"]

    urls: List[str] = []
    # Commons
    for q in base_queries:
        try:
            found = _commons_query(q, limit)
        except requests.HTTPError:
            found = []
        for u in found:
            if u not in urls:
                urls.append(u)
        if len(urls) >= limit:
            break

    # Wikipedia ES -> EN
    if len(urls) < limit:
        for api in (WIKIPEDIA_API_ES, WIKIPEDIA_API_EN):
            for q in base_queries:
                try:
                    found = _wiki_thumbs(api, q, limit - len(urls))
                except requests.HTTPError:
                    found = []
                for u in found:
                    if u not in urls:
                        urls.append(u)
                if len(urls) >= limit:
                    break
            if len(urls) >= limit:
                break

    # Prioriza ilustraciones/diagramas sobre fotos genéricas
    prefer = ("diagram", "illustration", "interference", "slit", "atom", "quantum", "feynman", "diffraction")
    urls.sort(key=lambda u: any(k in u.lower() for k in prefer), reverse=True)
    return urls[:limit]

def download_images(urls: List[str], outdir: str) -> int:
    os.makedirs(outdir, exist_ok=True)
    saved = 0
    for i, u in enumerate(urls, 1):
        ext = ".jpg"
        m = re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", u, flags=re.IGNORECASE)
        if m:
            ext = "." + m.group(1).lower()
        path = os.path.join(outdir, f"img_{i:03d}{ext}")
        try:
            resp = SESSION.get(u, timeout=TIMEOUT)
            resp.raise_for_status()
            with open(path, "wb") as f:
                f.write(resp.content)
            print(f"  ✓ {path}")
            saved += 1
        except Exception as e:
            print(f"  ⚠️  Error con {u}: {e}")
    return saved

def main():
    ap = argparse.ArgumentParser(description="Descarga imágenes libres para un tema.")
    ap.add_argument("--tema", type=str, help="Tema a buscar.")
    ap.add_argument("--tema-from-config", action="store_true", help="Leer tema desde config.json.")
    ap.add_argument("--out", type=str, help="Carpeta de salida. Por defecto: outputs/<slug>/imagenes.")
    ap.add_argument("--n", type=int, default=20, help="Número de imágenes.")
    args = ap.parse_args()

    tema = args.tema or (tema_from_config() if args.tema_from_config else "El universo")
    slug = slugify(tema)
    outdir = args.out or os.path.join("outputs", slug, "imagenes")
    os.makedirs(outdir, exist_ok=True)

    print(f"🔎 Buscando imágenes para: {tema}")
    urls = search_images_multi(tema, args.n)

    if not urls:
        print("❌ No se han podido obtener imágenes.")
        sys.exit(2)

    print(f"⬇️ Descargando {len(urls)} imágenes a {outdir} ...")
    saved = download_images(urls, outdir)
    if saved == 0:
        print("❌ No se descargó ninguna imagen.")
        sys.exit(2)
    print(f"✅ Descargadas {saved} imágenes en {outdir}")

if __name__ == "__main__":
    main()