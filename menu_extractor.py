"""
menu_extractor.py — Extraction catalogue par Gemini (gratuit) ou Claude Vision
Priorité : GEMINI_API_KEY → ANTHROPIC_API_KEY
"""

from __future__ import annotations
import os, json, base64, re, time

_PROMPT = """Tu es un expert en extraction de cartes de restaurant et de bar.
Analyse cette image et extrais TOUS les produits/articles visibles.

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

═══ RÈGLES GÉNÉRALES ═══

- prix = nombre décimal en euros (ex: 12.50). null si non indiqué ou illisible.
- confiance = "haute" / "moyenne" / "basse" selon la lisibilité globale de la carte.
- Regroupe par catégories logiques (entrées, plats, desserts, boissons, formules…).
- Les points de suspension "......." et tirets "-----" entre un nom et un prix sont des séparateurs visuels : ignore-les.
- Ne mets jamais de numéro de référence dans le nom ("12. Poulet rôti" → nom = "Poulet rôti").
- Ne mets jamais les codes allergènes dans le nom ("Pizza (G)(L)" → nom = "Pizza", description = "Contient gluten, lactose").
- Réponds UNIQUEMENT en JSON valide, sans texte avant ni après, sans markdown.

═══ VARIANTES ET SAVEURS ═══

Si une ligne liste plusieurs variantes/saveurs d'un même produit séparées par des virgules ou "/"
avec un seul prix commun (ex: "Mochis chocolat, noix de coco, mangue passion ..... 4€"),
crée UN produit SÉPARÉ par variante avec le même prix.
→ {"nom": "Mochi chocolat", "prix": 4.0, "description": null}
→ {"nom": "Mochi noix de coco", "prix": 4.0, "description": null}
→ {"nom": "Mochi mangue passion", "prix": 4.0, "description": null}

═══ TAILLES ET FORMATS ═══

Si un produit existe en plusieurs tailles ou formats avec des prix différents
(ex: "Café 2€ / 2.50€" ou "S 2€ · M 3€ · L 4€"),
crée un produit par taille.
→ {"nom": "Café petit", "prix": 2.0}
→ {"nom": "Café grand", "prix": 2.5}

Pour les boissons au verre / pichet / bouteille
(ex: "Vin rouge verre 4€ · pichet 11€ · bouteille 18€"),
crée 3 produits séparés.
→ {"nom": "Vin rouge (verre)", "prix": 4.0}
→ {"nom": "Vin rouge (pichet)", "prix": 11.0}
→ {"nom": "Vin rouge (bouteille)", "prix": 18.0}

═══ PRIX ═══

- S'il y a un prix barré et un prix actuel, prends UNIQUEMENT le prix actuel (le plus bas ou le non-barré).
- "à partir de 14€" → prix = 14.0, description = "à partir de 14€".
- "de 8€ à 15€" → prix = null, description = "de 8€ à 15€".
- Les suppléments ("+1€ fromage", "+0.50€ sauce") → crée un produit "Supplément fromage" avec prix = 1.0.
  Ne les fusionne jamais avec le produit principal.

═══ FORMULES ET MENUS COMPOSÉS ═══

Si une formule propose des composants AU CHOIX (entrée + plat + dessert, boisson + plat, etc.),
crée UN produit avec un champ "composants" qui référence les produits déjà extraits dans le menu.

Format attendu :
{
  "nom": "Menu midi",
  "prix": 15.0,
  "description": "Entrée + plat + dessert au choix",
  "composants": [
    {"label": "Votre entrée",   "obligatoire": true, "choix": ["Salade César", "Soupe du jour", "Terrine"]},
    {"label": "Votre plat",     "obligatoire": true, "choix": ["Steak frites", "Poulet rôti", "Risotto"]},
    {"label": "Votre dessert",  "obligatoire": true, "choix": ["Crème brûlée", "Tarte maison"]}
  ]
}

Règles formules :
- Peuple les "choix" avec les noms EXACTS des produits que tu as extraits dans les catégories correspondantes.
- Si la formule mentionne "boisson incluse" ou "boisson au choix", ajoute un composant "Votre boisson".
- Si un nombre est précisé ("2 plats au choix"), indique-le dans le label : "Votre plat (2 au choix)".
- obligatoire = true sauf si la formule indique explicitement que le composant est optionnel.
- Un produit SANS champ "composants" = produit standard sans choix, ne mets pas le champ du tout.
- Ne JAMAIS découper une formule en produits séparés.

═══ PRODUITS SPÉCIAUX ═══

- "Plat du jour" sans prix → {"nom": "Plat du jour", "prix": null, "description": "Selon arrivage"}
- Produits rayés/barrés sur la carte (épuisés) → NE PAS inclure.
- Texte de bas de page (mentions légales, TVA, service, allergènes génériques) → ignorer complètement.

═══ AVERTISSEMENTS ═══

Ajoute un avertissement si :
- Une zone de la carte est illisible ou floue
- Un prix semble anormalement élevé (>80€ pour un plat standard)
- Tu as dû deviner une information incertaine
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


def _extract_mistral(image_bytes: bytes, mime: str) -> dict:
    import urllib.request
    b64 = base64.standard_b64encode(image_bytes).decode()
    payload = json.dumps({
        "model": "pixtral-12b-2409",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 4096,
    }).encode()
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                "https://api.mistral.ai/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                text = json.loads(r.read())["choices"][0]["message"]["content"]
            return _validate(_parse(text))
        except json.JSONDecodeError as e:
            if attempt == 0:
                time.sleep(1)
                continue
            return {"erreur": "JSON invalide retourné par l'IA", "detail": str(e)}
        except Exception as e:
            if attempt == 0:
                time.sleep(1)
                continue
            return {"erreur": "Extraction échouée", "detail": str(e)}
    return {"erreur": "Extraction échouée après 2 tentatives"}


def _extract_gemini(image_bytes: bytes, mime: str) -> dict:
    import google.generativeai as genai
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-1.5-flash")
    for attempt in range(2):
        try:
            resp = model.generate_content([
                {"mime_type": mime, "data": image_bytes},
                _PROMPT,
            ])
            return _validate(_parse(resp.text))
        except json.JSONDecodeError as e:
            if attempt == 0:
                time.sleep(1)
                continue
            return {"erreur": "JSON invalide retourné par l'IA", "detail": str(e)}
        except Exception as e:
            if attempt == 0:
                time.sleep(1)
                continue
            return {"erreur": "Extraction échouée", "detail": str(e)}
    return {"erreur": "Extraction échouée après 2 tentatives"}


def _extract_claude(image_bytes: bytes, mime: str) -> dict:
    from anthropic import Anthropic
    client = Anthropic()
    b64 = base64.standard_b64encode(image_bytes).decode()
    for attempt in range(2):
        try:
            resp = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=4096,
                system=(
                    "Tu es un expert en extraction de menus. "
                    "Tu réponds UNIQUEMENT en JSON valide, sans texte avant ni après."
                ),
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


def extract_from_image(image_bytes: bytes, filename: str) -> dict:
    ext  = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpeg"
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")

    if os.environ.get("MISTRAL_API_KEY"):
        return _extract_mistral(image_bytes, mime)
    if os.environ.get("GEMINI_API_KEY"):
        return _extract_gemini(image_bytes, mime)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _extract_claude(image_bytes, mime)
    return {"erreur": "Aucune clé IA configurée", "detail": "Ajoutez MISTRAL_API_KEY, GEMINI_API_KEY ou ANTHROPIC_API_KEY"}


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
            "nom_commerce":  place.get("name", query),
            "adresse":       place.get("formatted_address", ""),
            "categories":    [],
            "total_produits": 0,
            "avertissements": [
                "Commerce créé via Google Places — uploadez une photo de menu pour ajouter les produits."
            ],
            "confiance": "basse",
            "source":    "google_places",
        }
    except Exception:
        return None
