"""
menu_extractor.py — Extraction catalogue par Claude Vision ou Google Places
"""

from __future__ import annotations
import os, json, base64, re, time
from anthropic import Anthropic

client = Anthropic()

_SYSTEM = (
    "Tu es un expert en extraction de menus de restaurants et catalogues de produits. "
    "Tu réponds UNIQUEMENT en JSON valide, sans texte avant ni après, sans bloc markdown."
)

_PROMPT = """Analyse cette image et extrais tous les produits/articles visibles.

Retourne exactement ce JSON (pas de ```json, juste le JSON brut) :
{
  "nom_commerce": "Nom si visible, sinon null",
  "categories": [
    {
      "nom_categorie": "Catégorie",
      "produits": [
        {"nom": "Produit", "prix": 12.50, "description": "Description courte ou null"}
      ]
    }
  ],
  "avertissements": [],
  "confiance": "haute"
}

Règles :
- prix = nombre décimal en euros (12.50), null si invisible
- confiance = haute / moyenne / basse selon lisibilité
- Regroupe par catégories logiques (entrées, plats, desserts, boissons, formules…)
- Si texte illisible → avertissement explicite dans le tableau
"""


def _parse(raw: str) -> dict:
    raw = raw.strip()
    if "```" in raw:
        raw = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    return json.loads(raw)


def _validate(data: dict) -> dict:
    warns = list(data.get("avertissements") or [])
    for cat in data.get("categories") or []:
        for prod in cat.get("produits") or []:
            px = prod.get("prix")
            if px is not None:
                if px < 0:
                    prod["prix"] = None
                    warns.append(f"Prix négatif ignoré : {prod['nom']}")
                elif px > 500:
                    warns.append(f"Prix suspect (>{500}€) : {prod['nom']}")
    data["total_produits"] = sum(
        len(c.get("produits") or []) for c in (data.get("categories") or [])
    )
    data["avertissements"] = warns
    return data


def extract_from_image(image_bytes: bytes, filename: str) -> dict:
    ext = (filename.rsplit(".", 1)[-1].lower()) if "." in filename else "jpeg"
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    b64 = base64.standard_b64encode(image_bytes).decode()

    for attempt in range(2):
        try:
            resp = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=4096,
                system=_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                        {"type": "text", "text": _PROMPT},
                    ],
                }],
            )
            return _validate(_parse(resp.content[0].text))
        except json.JSONDecodeError as e:
            if attempt == 0:
                time.sleep(1.5)
                continue
            return {"erreur": "JSON invalide retourné par l'IA", "detail": str(e)}
        except Exception as e:
            if attempt == 0:
                time.sleep(1.5)
                continue
            return {"erreur": "Extraction échouée", "detail": str(e)}

    return {"erreur": "Extraction échouée après 2 tentatives"}


def extract_from_google_places(query: str) -> dict | None:
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return None
    try:
        import urllib.request, urllib.parse
        url = (
            "https://maps.googleapis.com/maps/api/place/textsearch/json"
            f"?query={urllib.parse.quote(query)}&key={api_key}"
        )
        with urllib.request.urlopen(url, timeout=5) as r:
            results = json.loads(r.read()).get("results", [])
        if not results:
            return None
        place = results[0]
        return {
            "nom_commerce": place.get("name", query),
            "adresse": place.get("formatted_address", ""),
            "categories": [],
            "total_produits": 0,
            "avertissements": [
                "Commerce créé via Google Places — aucun menu automatique disponible. "
                "Ajoutez les produits manuellement ou uploadez une photo de menu."
            ],
            "confiance": "basse",
            "source": "google_places",
        }
    except Exception:
        return None
