"""LKQ collision-parts search via LKQ open catalog API."""

from __future__ import annotations

import os
import logging
import urllib.parse
from typing import Any

from curl_cffi import requests as c_requests

logger = logging.getLogger("LKQSearch")

LKQ_ENDPOINT = "https://lkqonline.com/api/catalog/1/product"
DEFAULT_LATITUDE = float(os.environ.get("LKQ_LATITUDE", "38.742271"))
DEFAULT_LONGITUDE = float(os.environ.get("LKQ_LONGITUDE", "-97.262715"))
DEFAULT_TAKE = int(os.environ.get("LKQ_TAKE", "12"))

# Human-facing LKQ Online site base (used to build part/product links).
LKQ_SITE_BASE = os.environ.get("LKQ_SITE_BASE", "https://www.lkqonline.com").rstrip("/")

_AVAILABILITY_MAP = {
    "availablenational": "Available (National)",
    "availablelocal": "Available (Local)",
    "availablebytransfer": "Available by Transfer",
    "outofstock": "Out of Stock",
    "unavailable": "Unavailable",
}


def _price_to_str(value: Any, *, allow_zero: bool = False) -> str | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if allow_zero and number < 0:
        return None
    if not allow_zero and number <= 0:
        return None
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n", ""}:
            return False
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _part_number_key(value: Any) -> str:
    """Normalize formatting only; meaningful letters and digits remain significant."""
    return "".join(char for char in str(value or "").upper() if char.isalnum())


def _pick_first_image_url(images: Any) -> str:
    if not isinstance(images, list):
        return ""
    for image in images:
        if not isinstance(image, dict):
            continue
        url = str(image.get("url", "")).strip()
        if url:
            return url
    return ""


def _pricing_first(item: dict[str, Any]) -> dict[str, Any]:
    pricing = item.get("pricing")
    if isinstance(pricing, list) and pricing and isinstance(pricing[0], dict):
        return pricing[0]
    return {}


def _availability_message(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return "N/A"
    mapped = _AVAILABILITY_MAP.get(text.lower())
    if mapped:
        return mapped
    return text


def _sale_and_regular(price: str | None, list_price: str | None) -> str:
    if price and list_price and price != list_price:
        return f"${price} (was ${list_price})"
    if price:
        return f"${price}"
    if list_price:
        return f"${list_price}"
    return "N/A"


def _build_search_url(
    keyword: str,
    *,
    latitude: float,
    longitude: float,
    take: int,
) -> str:
    encoded_keyword = urllib.parse.quote_plus(keyword)
    return (
        "https://lkqonline.com/api/catalog/1/product?"
        f"catalogId=0&sort=closestFirst&keyword={encoded_keyword}"
        f"&skip=0&take={take}&latitude={latitude}&longitude={longitude}"
    )


def _build_site_search_url(keyword: str) -> str:
    """Human-facing LKQ Online search page for a keyword / part number."""
    return f"{LKQ_SITE_BASE}/search?keyword={urllib.parse.quote_plus(keyword)}"


def _build_product_url(item_id: str, number: str) -> str:
    """Human-facing LKQ Online product page for a part.

    LKQ product pages are keyed by the catalog ``id`` (e.g. ``001-AC1000130``).
    Falls back to a keyword search when the id is missing.
    """
    ident = str(item_id or "").strip()
    if ident:
        return f"{LKQ_SITE_BASE}/product/{urllib.parse.quote(ident, safe='-._~')}"
    return _build_site_search_url(number)


def _normalize_item(
    item: dict[str, Any],
    *,
    request_keyword: str,
    latitude: float,
    longitude: float,
    take: int,
) -> dict[str, Any]:
    pricing0 = _pricing_first(item)

    number = str(item.get("number") or request_keyword).strip() or request_keyword
    item_id = str(item.get("id") or "").strip()
    product_url = _build_product_url(item_id, number)
    title = str(item.get("descriptionRetail") or item.get("id") or number).strip()
    category = str(item.get("category") or "N/A").strip() or "N/A"
    condition = str(item.get("ftcDisplay") or "N/A").strip() or "N/A"
    unit_of_measure = str(item.get("unitOfMeasure") or item.get("unitOfMeasureCode") or "N/A").strip() or "N/A"

    price = _price_to_str(item.get("price")) or _price_to_str(pricing0.get("customerPrice"))
    list_price = _price_to_str(item.get("listPrice")) or _price_to_str(pricing0.get("listPrice"))
    core_price = _price_to_str(item.get("corePrice"))
    shipping = _price_to_str(pricing0.get("shipping"), allow_zero=True)

    free_shipping_flag = _to_bool(item.get("freeShippingEligible")) or _to_bool(pricing0.get("isFreeShipping"))
    if free_shipping_flag:
        delivery_message = "Free shipping eligible"
    elif shipping:
        delivery_message = f"${shipping} shipping"
    else:
        delivery_message = "N/A"

    return {
        "retailer": "LKQ",
        "retailer_code": "lkq",
        "part_number": number,
        "part_link": product_url,
        "product_title": title or "N/A",
        "price": price or "Price not available",
        "list_price": list_price or "N/A",
        "core_price": core_price or "N/A",
        "currency": "USD",
        "availability": _availability_message(item.get("availability")),
        "availability_message": _availability_message(item.get("availability")),
        "condition": condition,
        "category": category,
        "image_url": _pick_first_image_url(item.get("images")),
        "free_shipping": free_shipping_flag,
        "unit_of_measure": unit_of_measure,
        "shipping": shipping or "N/A",
        "delivery_message": delivery_message,
        "in_store_pickup": "N/A",
        "sale_and_regular_price": _sale_and_regular(price, list_price),
        "rrp_message": f"${list_price}" if list_price else "N/A",
        "rating": "N/A",
        "pack_size": "N/A",
        "search_url": _build_search_url(
            number,
            latitude=latitude,
            longitude=longitude,
            take=take,
        ),
        "source_url": _build_search_url(
            number,
            latitude=latitude,
            longitude=longitude,
            take=take,
        ),
        "found": True,
        "status": "success",
        "confidence": 0.95,
    }


def lkq_search_part(
    part_number: str,
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    take: int = DEFAULT_TAKE,
) -> tuple[list[dict[str, Any]], int]:
    """Search LKQ catalog by part number and return normalized rows + total count."""
    keyword = str(part_number or "").strip()
    if not keyword:
        return [], 0

    lat = float(DEFAULT_LATITUDE if latitude is None else latitude)
    lon = float(DEFAULT_LONGITUDE if longitude is None else longitude)
    safe_take = max(1, min(int(take or DEFAULT_TAKE), 50))

    params = {
        "catalogId": 0,
        "sort": "closestFirst",
        "keyword": keyword,
        "skip": 0,
        "take": safe_take,
        "latitude": lat,
        "longitude": lon,
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://lkqonline.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    response = c_requests.get(
        LKQ_ENDPOINT,
        params=params,
        headers=headers,
        impersonate="chrome",
        timeout=25,
    )
    if response.status_code != 200:
        raise Exception(f"LKQ API failed (HTTP {response.status_code}): {response.text[:300]}")

    payload = response.json()
    if not isinstance(payload, dict):
        raise Exception("LKQ API returned unexpected payload type.")

    raw_rows = payload.get("data") or []
    if not isinstance(raw_rows, list):
        raw_rows = []

    normalized: list[dict[str, Any]] = []
    requested_part_key = _part_number_key(keyword)
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        # LKQ can return related or suffix variants. A part such as FO123C is
        # different from FO123, so only formatting-insensitive exact matches
        # are valid for pricing.
        if _part_number_key(row.get("number")) != requested_part_key:
            continue
        try:
            normalized.append(
                _normalize_item(
                    row,
                    request_keyword=keyword,
                    latitude=lat,
                    longitude=lon,
                    take=safe_take,
                )
            )
        except Exception as e:  # noqa: PERF203
            logger.warning(f"[LKQ] Skipping malformed row: {e}")

    return normalized, len(normalized)
