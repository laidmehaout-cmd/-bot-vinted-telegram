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


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"La variable {name} manque.")
    return value


def request_json(url, headers=None, data=None):
    request = Request(
        url,
        data=data,
        headers=headers or {},
        method="POST" if data is not None else "GET",
    )
    with urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def api_url(search_url):
    parsed = urlparse(search_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc.endswith("vinted.fr"):
        raise RuntimeError("VINTED_SEARCH_URL doit être un lien de recherche vinted.fr.")

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["order"] = "newest_first"
    query["page"] = "1"
    query["per_page"] = "20"
    return f"https://www.vinted.fr/api/v2/catalog/items?{urlencode(query)}"


def fetch_items(search_url):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
        ),
    }

    cookie_jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))

    homepage_request = Request("https://www.vinted.fr/", headers=headers)
    with opener.open(homepage_request, timeout=25) as response:
        response.read(1)

    catalog_request = Request(api_url(search_url), headers=headers)
    with opener.open(catalog_request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))

    return payload.get("items", [])


def load_seen():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_seen(ids):
    STATE_FILE.write_text(
        json.dumps(ids[-MAX_SEEN:], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def item_price(item):
    price = item.get("price")
    if isinstance(price, dict):
        amount = price.get("amount") or price.get("value") or "?"
        currency = price.get("currency_code") or "EUR"
        return f"{amount} {currency}"

    return str(price or "Prix inconnu")


def telegram_send(token, chat_id, item):
    title = escape(str(item.get("title") or "Nouvelle annonce Vinted"))
    brand = escape(str(item.get("brand_title") or "Marque non indiquée"))
    size = escape(str(item.get("size_title") or "Taille non indiquée"))
    price = escape(item_price(item))
    url = item.get("url") or f"https://www.vinted.fr/items/{item['id']}"

    message = (
        f"🆕 <b>{title}</b>\n"
        f"💶 {price}\n"
        f"🏷 {brand}\n"
        f"📏 {size}\n\n"
        f'<a href="{escape(url, quote=True)}">Voir l’annonce</a>'
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
        f"https://api.telegram.org/bot{token}/sendMessage",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )

    if not result.get("ok"):
        raise RuntimeError(f"Telegram a refusé le message: {result}")


def main():
    token = required_env("TELEGRAM_BOT_TOKEN")
    chat_id = required_env("TELEGRAM_CHAT_ID")
    search_url = required_env("VINTED_SEARCH_URL")

    items = fetch_items(search_url)
    seen = load_seen()
    seen_set = set(str(value) for value in seen)
    fresh = [item for item in items if str(item.get("id")) not in seen_set]

    first_run = not STATE_FILE.exists()
    if first_run:
        fresh = fresh[:3]

    sent_ids = []

    for item in reversed(fresh[:10]):
        telegram_send(token, chat_id, item)
        sent_ids.append(str(item["id"]))
        time.sleep(1)

    current_ids = [str(item["id"]) for item in items if item.get("id")]
    save_seen((seen + sent_ids + current_ids)[-MAX_SEEN:])

    print(f"{len(items)} annonces trouvées, {len(sent_ids)} envoyées.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        raise
