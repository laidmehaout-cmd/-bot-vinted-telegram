import json
import os
import sys
import time
from http.cookiejar import CookieJar
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


STATE_FILE = Path("seen.json")
MAX_SEEN = 300
MAX_AI_ITEMS = 5
OPENAI_MODEL = "gpt-5.6-terra"


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"La variable {name} manque.")
    return value


def request_json(url, headers=None, data=None, timeout=45):
    request = Request(
        url,
        data=data,
        headers=headers or {},
        method="POST" if data is not None else "GET",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def api_url(search_url):
    parsed = urlparse(search_url)

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc.endswith("vinted.fr")
    ):
        raise RuntimeError(
            "VINTED_SEARCH_URL doit être un lien de recherche vinted.fr."
        )

    query = dict(
        parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
    )
    query["order"] = "newest_first"
    query["page"] = "1"
    query["per_page"] = "20"

    return (
        "https://www.vinted.fr/api/v2/catalog/items?"
        f"{urlencode(query)}"
    )


def vinted_session():
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 "
            "(iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 "
            "Version/18.0 Mobile/15E148 Safari/604.1"
        ),
    }

    opener = build_opener(
        HTTPCookieProcessor(CookieJar())
    )

    homepage_request = Request(
        "https://www.vinted.fr/",
        headers=headers,
    )

    with opener.open(
        homepage_request,
        timeout=25,
    ) as response:
        response.read(1)

    return opener, headers


def fetch_items(search_url):
    opener, headers = vinted_session()

    request = Request(
        api_url(search_url),
        headers=headers,
    )

    with opener.open(request, timeout=25) as response:
        payload = json.loads(
            response.read().decode("utf-8")
        )

    return payload.get("items", []), opener, headers


def fetch_item_details(item, opener, headers):
    item_id = item.get("id")

    if not item_id:
        return item

    try:
        request = Request(
            f"https://www.vinted.fr/api/v2/items/{item_id}",
            headers=headers,
        )

        with opener.open(request, timeout=25) as response:
            detailed = json.loads(
                response.read().decode("utf-8")
            ).get("item", {})

        merged = dict(item)
        merged.update(detailed)
        return merged

    except Exception as exc:
        print(
            f"Détails Vinted indisponibles "
            f"pour {item_id}: {exc}"
        )
        return item


def load_seen():
    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_seen(ids):
    STATE_FILE.write_text(
        json.dumps(
            ids[-MAX_SEEN:],
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def item_price(item):
    price = item.get("price")

    if isinstance(price, dict):
        amount = (
            price.get("amount")
            or price.get("value")
            or "?"
        )
        currency = (
            price.get("currency_code")
            or "EUR"
        )
        return f"{amount} {currency}"

    return str(price or "Prix inconnu")


def photo_urls(item):
    photos = item.get("photos") or []

    if not photos and item.get("photo"):
        photos = [item["photo"]]

    urls = []

    for photo in photos:
        if not isinstance(photo, dict):
            continue

        high_resolution = (
            photo.get("high_resolution")
            or {}
        )

        candidates = [
            high_resolution.get("url")
            if isinstance(high_resolution, dict)
            else None,
            photo.get("full_size_url"),
            photo.get("url"),
        ]

        url = next(
            (
                value
                for value in candidates
                if value
            ),
            None,
        )

        if url and url not in urls:
            urls.append(url)

    return urls[:4]


def extract_openai_text(payload):
    if isinstance(
        payload.get("output_text"),
        str,
    ):
        return payload["output_text"].strip()

    texts = []

    for output in payload.get("output", []):
        for content in output.get("content", []):
            if (
                content.get("type") == "output_text"
                and content.get("text")
            ):
                texts.append(content["text"])

    return "\n".join(texts).strip()


def analyze_item(openai_key, item):
    title = str(
        item.get("title")
        or "Non indiqué"
    )
    brand = str(
        item.get("brand_title")
        or "Non indiquée"
    )
    size = str(
        item.get("size_title")
        or "Non indiquée"
    )
    condition = str(
        item.get("status")
        or item.get("status_title")
        or "Non indiqué"
    )
    description = str(
        item.get("description")
        or "Non disponible"
    )[:1800]
    price = item_price(item)

    prompt = f"""
Tu es un expert prudent en achat-revente
de vêtements d'occasion en France.

Analyse cette annonce Vinted
pour une revente rapide.

Données :
- Titre : {title}
- Prix vendeur : {price}
- Marque : {brand}
- Taille : {size}
- État : {condition}
- Description : {description}

Réponds en français, en 7 lignes maximum,
exactement avec ces rubriques :

🎯 VERDICT : ACHÈTE / À VÉRIFIER / LAISSE
🛡️ AUTHENTICITÉ : risque faible / moyen / élevé / impossible à juger
💶 REVENTE RAPIDE : fourchette en euros
💰 BÉNÉFICE NET : estimation après prix d'achat, protection Vinted,
livraison vers l'acheteur-revendeur et marge de négociation, hors impôts
⚡ VITESSE : rapide / moyenne / lente
✅ POURQUOI : raison principale
🔍 AVANT D'ACHETER : vérification précise à demander

Règles absolues :
- Le bas de la fourchette du bénéfice net estimé
  doit atteindre au moins 30 €.
  Sinon, réponds LAISSE.
- Si la revente n'est pas rapide,
  réponds LAISSE.
- Une photo insuffisante, une incohérence
  ou un doute de contrefaçon interdit
  le verdict ACHÈTE.
- Ne prétends jamais garantir l'authenticité.
""".strip()

    content = [
        {
            "type": "input_text",
            "text": prompt,
        }
    ]

    for url in photo_urls(item):
        content.append(
            {
                "type": "input_image",
                "image_url": url,
                "detail": "high",
            }
        )

    body = json.dumps(
        {
            "model": OPENAI_MODEL,
            "reasoning": {
                "effort": "low",
            },
            "max_output_tokens": 500,
            "input": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
        }
    ).encode("utf-8")

    result = request_json(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization":
            f"Bearer {openai_key}",
            "Content-Type":
            "application/json",
        },
        data=body,
        timeout=60,
    )

    text = extract_openai_text(result)

    if not text:
        raise RuntimeError(
            "OpenAI n'a renvoyé aucune analyse."
        )

    return text[:2200]


def telegram_send(
    token,
    chat_id,
    item,
    analysis,
):
    title = escape(
        str(
            item.get("title")
            or "Nouvelle annonce Vinted"
        )
    )
    brand = escape(
        str(
            item.get("brand_title")
            or "Marque non indiquée"
        )
    )
    size = escape(
        str(
            item.get("size_title")
            or "Taille non indiquée"
        )
    )
    price = escape(item_price(item))

    url = (
        item.get("url")
        or f"https://www.vinted.fr/items/{item['id']}"
    )

    message = (
        f"🆕 <b>{title}</b>\n"
        f"💶 {price}\n"
        f"🏷 {brand}\n"
        f"📏 {size}\n\n"
        f"🤖 <b>AVIS DE L'IA</b>\n"
        f"{escape(analysis)}\n\n"
        f'<a href="{escape(url, quote=True)}">'
        f"Voir l’annonce</a>"
    )

    body = urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")

    result = request_json(
        (
            "https://api.telegram.org/"
            f"bot{token}/sendMessage"
        ),
        headers={
            "Content-Type":
            "application/x-www-form-urlencoded"
        },
        data=body,
    )

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram a refusé le message: {result}"
        )


def main():
    token = required_env(
        "TELEGRAM_BOT_TOKEN"
    )
    chat_id = required_env(
        "TELEGRAM_CHAT_ID"
    )
    search_url = required_env(
        "VINTED_SEARCH_URL"
    )
    openai_key = required_env(
        "OPENAI_API_KEY"
    )

    items, opener, headers = fetch_items(
        search_url
    )

    seen = load_seen()
    seen_set = {
        str(value)
        for value in seen
    }

    fresh = [
        item
        for item in items
        if str(item.get("id")) not in seen_set
    ]

    sent_ids = []

    for item in reversed(
        fresh[:MAX_AI_ITEMS]
    ):
        detailed_item = fetch_item_details(
            item,
            opener,
            headers,
        )

        try:
            analysis = analyze_item(
                openai_key,
                detailed_item,
            )

        except Exception as exc:
            print(
                "Analyse IA indisponible pour "
                f"{item.get('id')}: {exc}"
            )
            analysis = (
                "⚠️ Analyse indisponible. "
                "N'achète pas sans vérifier toi-même "
                "l'authenticité, l'état et le prix "
                "de revente."
            )

        first_line = (
            analysis.splitlines()[0].upper()
            if analysis
            else ""
        )

        if "LAISSE" in first_line:
            print(
                f"Annonce {item.get('id')} ignorée : "
                "bénéfice net ou vitesse insuffisante."
            )
            continue

        telegram_send(
            token,
            chat_id,
            detailed_item,
            analysis,
        )

        sent_ids.append(
            str(item["id"])
        )
        time.sleep(1)

    current_ids = [
        str(item["id"])
        for item in items
        if item.get("id")
    ]

    save_seen(
        (
            seen
            + sent_ids
            + current_ids
        )[-MAX_SEEN:]
    )

    print(
        f"{len(items)} annonces trouvées, "
        f"{len(sent_ids)} bonnes affaires envoyées."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"ERREUR: {exc}",
            file=sys.stderr,
        )
        raise
