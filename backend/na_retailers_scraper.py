# --- na_retailers_scraper.py ---
# Core scraper for North American auto parts retailers using SerpApi-first live search.

import os
import re
import json
import time
import logging
import threading
import urllib.parse
from datetime import datetime
from seleniumbase import Driver

from curl_cffi import requests as c_requests
from dotenv import load_dotenv

from serpapi_search import serpapi_scrape_retailer

load_dotenv()

# Optional non-LLM browser fallback for retailers that need site-specific handling.
ALWAYS_HTML_FALLBACK_RETAILERS = {
    name.strip()
    for name in os.environ.get("SERPAPI_ALWAYS_HTML_FALLBACK_RETAILERS", "CarParts").split(",")
    if name.strip()
}

SUMMIT_BRAND_CODES = {
    # Summit product URLs use a manufacturer line code, e.g. /parts/ran-rs55047a.
    # Keep this as a brand mapping, not a product-specific special case.
    "rancho": "ran",
}
SUMMIT_BROWSER_WAIT_SECONDS = float(os.environ.get("SUMMIT_BROWSER_WAIT_SECONDS", "6"))
SUMMIT_BROWSER_SEMAPHORE = threading.Semaphore(1)
CARPARTS_BROWSER_WAIT_SECONDS = float(os.environ.get("CARPARTS_BROWSER_WAIT_SECONDS", "6"))
CARPARTS_BROWSER_SEMAPHORE = threading.Semaphore(1)

# --- Configuration ---
PROJECT  = "ford-5ae865a4a2f14ba62fcc8c2d"
LOCATION = "us-central1"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Retailer Registry ---
RETAILER_CONFIG = {
    "AutoZone": {
        "code": "autozone",
        "base_url": "https://www.autozone.com",
        "language": "English",
        "currency": "USD",
        "path": "B", # HTML Fetch
        "search_path": "/searchresult",
        "search_param": "searchText",
        "search_format": "part_only",
    },
    "O'Reilly": {
        "code": "oreilly",
        "base_url": "https://www.oreillyauto.com",
        "language": "English",
        "currency": "USD",
        "path": "C",
        "search_path": "/search",
        "search_param": "q",
        "search_format": "part_only",
    },
    "Advance Auto": {
        "code": "advanceauto",
        "base_url": "https://shop.advanceautoparts.com",
        "language": "English",
        "currency": "USD",
        "path": "C",
        "search_path": "/web/SearchResults",
        "search_param": "searchTerm",
        "search_format": "part_only",
    },
    "NAPA": {
        "code": "napa",
        "base_url": "https://www.napaonline.com",
        "language": "English",
        "currency": "USD",
        "path": "D",
        "search_path": "/en/search",
        "search_param": "text",
        "search_format": "brand_part",
    },
    "Amazon": {
        "code": "amazon",
        "base_url": "https://www.amazon.com",
        "language": "English",
        "currency": "USD",
        "path": "A",
        "search_path": "/s",
        "search_param": "k",
        "search_format": "brand_part",
    },
    "Summit Racing": {
        "code": "summitracing",
        "base_url": "https://www.summitracing.com",
        "language": "English",
        "currency": "USD",
        "path": "A",
        "search_path": "/search",
        "search_param": "keyword",
        "search_format": "part_only",
    },
    "Walmart": {
        "code": "walmart",
        "base_url": "https://www.walmart.com",
        "language": "English",
        "currency": "USD",
        "path": "D",
        "search_path": "/search",
        "search_param": "q",
        "search_format": "part_auto",
    },
    "CarParts": {
        "code": "carparts",
        "base_url": "https://www.carparts.com",
        "language": "English",
        "currency": "USD",
        "path": "B",
        "search_path": "/search",
        "search_param": "q",
        "search_format": "brand_part",
    },
}

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _build_search_url(retailer_name, part_number, brand_name):
    cfg = RETAILER_CONFIG[retailer_name]
    base_url = cfg["base_url"]
    search_path = cfg["search_path"]
    param = cfg["search_param"]
    fmt = cfg["search_format"]

    if fmt == "part_auto":
        query = f"{part_number} auto parts"
    elif fmt == "brand_part":
        query = f"{brand_name} {part_number}"
    else:
        query = part_number

    encoded_q = urllib.parse.quote_plus(query)
    return f"{base_url}{search_path}?{param}={encoded_q}"


def _normalise_part_for_match(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _price_to_str(value, allow_zero=False):
    if value in (None, ""):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if allow_zero:
        if num < 0:
            return None
    elif num <= 0:
        return None
    text = f"{num:.2f}"
    return text.rstrip("0").rstrip(".")


def _has_usable_price(value):
    if value in (None, "", 0, 0.0, "0", "0.0"):
        return False
    text = str(value).strip().lower()
    return text not in {
        "price not available",
        "part not found",
        "error",
        "website can't be bypassed",
    }


def _schema_availability_to_label(raw):
    text = str(raw or "").strip()
    if not text:
        return "Availability not shown", False

    key = text.split("/")[-1].lower()
    mapping = {
        "instock": ("In stock", True),
        "outofstock": ("Out of stock", False),
        "preorder": ("Preorder", False),
        "limitedavailability": ("Limited availability", True),
        "onlineonly": ("Online only", True),
    }
    if key in mapping:
        return mapping[key]
    return text, "instock" in key


def _should_use_fallback_result(current: dict, fallback: dict) -> bool:
    """
    Decide whether fallback output is better than the current parsed result.
    We prefer:
    1) Any found fallback when current did not find a product.
    2) Found + priced fallback when current found product but price is missing.
    """
    current_found = bool(current.get("found"))
    fallback_found = bool(fallback.get("found"))
    if not current_found:
        return fallback_found or fallback.get("status") == "blocked"

    current_price_ok = _has_usable_price(current.get("price"))
    fallback_price_ok = _has_usable_price(fallback.get("price"))
    return (not current_price_ok) and fallback_found and fallback_price_ok


def _extract_carparts_next_data(raw_html):
    match = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        raw_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    payload = match.group(1).strip()
    if not payload:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


def _scrape_carparts_browser_fallback(part_number, brand_name, product_url):
    """
    CarParts pages are often Akamai-protected for raw HTTP clients.
    Use a real browser fallback and parse Next.js hydration JSON deterministically
    (without Gemini) to extract price/availability/list-price.
    """
    driver = None
    try:
        with CARPARTS_BROWSER_SEMAPHORE:
            logging.info(f"[CarParts] Browser fallback loading {product_url}")
            driver = Driver(uc=True, headless2=True)
            driver.get(product_url)
            time.sleep(CARPARTS_BROWSER_WAIT_SECONDS)
            raw_html = driver.page_source or ""

        if len(raw_html) < 5000 or "powered and protected by" in raw_html.lower():
            logging.warning("[CarParts] Browser fallback appears blocked or incomplete.")
            return {"found": False, "status": "blocked", "confidence": 0.0}

        next_data = _extract_carparts_next_data(raw_html)
        if not isinstance(next_data, dict):
            logging.warning("[CarParts] __NEXT_DATA__ payload missing; cannot parse deterministically.")
            return None

        page_props = (
            (next_data.get("props") or {}).get("pageProps") or {}
            if isinstance(next_data.get("props"), dict) else {}
        )
        if not isinstance(page_props, dict):
            return None

        current_product = page_props.get("currentProduct") or {}
        product_details = current_product.get("productDetails") or {} if isinstance(current_product, dict) else {}
        pricing = product_details.get("pricing") or {} if isinstance(product_details, dict) else {}

        # Offer availability/price can also be exposed in structuredData/productJsonLd.
        offer = None
        structured = current_product.get("structuredData") if isinstance(current_product, dict) else None
        if isinstance(structured, list):
            for item in structured:
                if not isinstance(item, dict):
                    continue
                offers = item.get("offers")
                if isinstance(offers, dict) and (offers.get("price") is not None or offers.get("availability")):
                    offer = offers
                    break
        if offer is None:
            seo_offer = (
                ((page_props.get("productPageSeoData") or {}).get("productJsonLd") or {}).get("offers")
                if isinstance(page_props.get("productPageSeoData"), dict) else None
            )
            if isinstance(seo_offer, dict):
                offer = seo_offer

        price = _price_to_str(pricing.get("regularPrice")) or _price_to_str((offer or {}).get("price"))
        list_price = _price_to_str(pricing.get("listPrice"))
        shipping = _price_to_str(pricing.get("shipping"), allow_zero=True)
        core_price = _price_to_str(pricing.get("corePrice"), allow_zero=True)

        availability_label, in_stock = _schema_availability_to_label((offer or {}).get("availability"))
        sku_title = str(product_details.get("skuTitle") or "").strip()
        title = sku_title or str(product_details.get("description") or "").strip() or f"{brand_name} {part_number}"
        mfr_number = str(product_details.get("mfrNumber") or "").strip().lstrip("#")
        resolved_part = mfr_number or part_number

        if price and list_price and float(list_price) > float(price):
            sale_regular = f"${price} (was ${list_price})"
        elif price:
            sale_regular = f"${price}"
        elif list_price:
            sale_regular = f"${list_price}"
        else:
            sale_regular = "N/A"

        if shipping is None:
            delivery_message = "N/A"
        elif float(shipping) == 0:
            delivery_message = "Free delivery"
        else:
            delivery_message = f"${shipping} delivery"

        return {
            "part_number": resolved_part,
            "product_title": title,
            "retailer": "CarParts",
            "price": price or "Price not available",
            "currency": "USD",
            "pack_size": "1",
            "rating": "N/A",
            "in_stock": in_stock,
            "availability": availability_label,
            "availability_message": availability_label,
            "delivery_message": delivery_message,
            "in_store_pickup": "N/A",
            "sale_and_regular_price": sale_regular,
            "rrp_message": f"${list_price}" if list_price else "N/A",
            "core_price": core_price or "N/A",
            "source_url": product_url,
            "confidence": 0.95 if _has_usable_price(price) else 0.75,
            "found": True if title else False,
        }
    except Exception as e:
        logging.warning(f"[CarParts] Browser fallback failed: {e}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# PATH A: LIVE WEB SEARCH — now powered by SerpApi (Google Search API)
# ---------------------------------------------------------------------------

def _scrape_path_a_serpapi(part_number, brand_name, retailer):
    """Live search via SerpApi (Google Shopping + Google organic)."""
    return serpapi_scrape_retailer(part_number, brand_name, retailer)

def _scrape_summit_browser_fallback(part_number, brand_name):
    """
    Summit blocks server-side HTTP with Incapsula, while a real browser can load
    the product page. Use this only as a fallback after SerpApi proves the part
    exists but cannot provide a reliable live price.
    """
    brand_code = SUMMIT_BRAND_CODES.get(str(brand_name or "").strip().lower())
    if not brand_code:
        return None

    product_url = f"https://www.summitracing.com/parts/{brand_code}-{str(part_number).strip().lower()}"
    driver = None
    try:
        logging.info(f"[Summit Racing] Browser fallback loading {product_url}")
        with SUMMIT_BROWSER_SEMAPHORE:
            driver = Driver(uc=True, headless2=True)
            driver.get(product_url)

            raw_html = ""
            compact = ""
            deadline = time.time() + SUMMIT_BROWSER_WAIT_SECONDS
            price_match = None

            while time.time() < deadline:
                raw_html = driver.page_source or ""
                compact = re.sub(r"\s+", " ", raw_html)
                price_match = re.search(r'"CurrentFormatted"\s*:\s*"\$([\d,]+(?:\.\d{2})?)"', compact)
                if not price_match:
                    price_match = re.search(
                        r'id="[^"]*currentPrice"[^>]*>.*?<p class="price">\s*<span>\$([\d,]+(?:\.\d{2})?)</span>',
                        compact,
                        flags=re.IGNORECASE,
                    )
                if (
                    _normalise_part_for_match(part_number) in _normalise_part_for_match(compact)
                    and price_match
                ):
                    break
                if "pardon our interruption" in compact.lower():
                    break
                time.sleep(0.4)

        if _normalise_part_for_match(part_number) not in _normalise_part_for_match(compact):
            logging.warning("[Summit Racing] Browser fallback loaded page without exact part number.")
            return None
        if "pardon our interruption" in compact.lower():
            logging.warning("[Summit Racing] Browser fallback was blocked by Summit.")
            return None

        title_match = re.search(r"<title>(.*?)</title>", raw_html, flags=re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else f"{brand_name} {part_number}"

        if not price_match:
            return None

        price = price_match.group(1).replace(",", "")
        return {
            "part_number": part_number,
            "product_title": title.replace(" | Summit Racing", ""),
            "retailer": "Summit Racing",
            "price": price,
            "currency": "USD",
            "pack_size": "1",
            "rating": "N/A",
            "in_stock": "Add To Cart" in compact or "In Stock" in compact,
            "source_url": product_url,
            "confidence": 0.95,
            "found": True,
        }
    except Exception as e:
        logging.warning(f"[Summit Racing] Browser fallback failed: {e}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

# ---------------------------------------------------------------------------
# ORCHESTRATION
# ---------------------------------------------------------------------------

def scrape_na_retailer(part_number, brand_name, retailer, save_screenshot=True):
    """
    Dual-path orchestrator. No Selenium required.
    Returns the legacy dict format for na_parts_main.py compatibility.
    """
    if retailer == "My Auto Parts":
        try:
            from map_scraper import scrape_myautoparts_live
            return scrape_myautoparts_live(part_number, brand_name, save_screenshot)
        except ImportError:
            pass

    if retailer not in RETAILER_CONFIG:
        return {
            'retailer': retailer, 'retailer_code': '??',
            'status': 'error', 'product_title': 'N/A', 'price': 'Error',
            'search_url': ''
        }

    r_config = RETAILER_CONFIG[retailer]
    search_url = _build_search_url(retailer, part_number, brand_name)

    parsed = {}

    # ── Primary live search: SerpApi (Google Search API) for ALL retailers ──
    try:
        logging.info(f"[{retailer}] Live search via SerpApi for {part_number}...")
        parsed = _scrape_path_a_serpapi(part_number, brand_name, retailer)
        if isinstance(parsed, list):
            parsed = parsed[0] if parsed else {"found": False}
        parsed = {k.lower(): v for k, v in parsed.items()}
    except Exception as e:
        logging.error(f"[{retailer}] SerpApi live search failed: {e}")
        parsed = {"found": False, "status": "error"}

    if retailer == "Summit Racing" and parsed.get("found") and not _has_usable_price(parsed.get("price")):
        summit_live = _scrape_summit_browser_fallback(part_number, brand_name)
        if summit_live and summit_live.get("found"):
            parsed = {k.lower(): v for k, v in summit_live.items()}

    # ── Optional non-LLM browser fallback for specific retailers ──
    should_try_browser_fallback = retailer in ALWAYS_HTML_FALLBACK_RETAILERS
    needs_price_enrichment = parsed.get("found") and not _has_usable_price(parsed.get("price"))
    should_run_fallback = should_try_browser_fallback and (not parsed.get("found") or needs_price_enrichment)
    if should_run_fallback:
        try:
            fallback_url = str(parsed.get("source_url") or parsed.get("search_url") or search_url).strip() or search_url
            if retailer == "CarParts":
                fallback = _scrape_carparts_browser_fallback(part_number, brand_name, fallback_url)
            else:
                fallback = None

            if fallback:
                if isinstance(fallback, list):
                    fallback = fallback[0] if fallback else {"found": False}
                fallback = {k.lower(): v for k, v in fallback.items()}
                if _should_use_fallback_result(parsed, fallback):
                    parsed = fallback
        except Exception as e:
            logging.error(f"[{retailer}] browser fallback error: {e}")

    # Map the Strict JSON Schema to the expected legacy format for `na_parts_main.py`
    found = parsed.get("found", False)
    is_blocked = parsed.get("status") == "blocked"

    # Determine the appropriate price/status message
    if found:
        raw_price = parsed.get('price')
        if not _has_usable_price(raw_price):
            price_display = "Price not available"
        else:
            price_display = str(raw_price)
        status_display = 'success'
        title_display = str(parsed.get('product_title', 'N/A'))
        rating_display = str(parsed.get('rating', 'N/A'))
        raw_avail_msg = str(parsed.get('availability_message', '')).strip()
        if not raw_avail_msg or raw_avail_msg.lower() == 'n/a':
            avail_msg_display = 'In Stock' if parsed.get('in_stock') else 'Availability not shown'
        else:
            avail_msg_display = raw_avail_msg
        avail_display = avail_msg_display
        pack_display = str(parsed.get('pack_size', '1'))
        delivery_display = str(parsed.get('delivery_message', 'N/A') or 'N/A')
        pickup_display = str(parsed.get('in_store_pickup', 'N/A') or 'N/A')
        sale_regular_display = str(parsed.get('sale_and_regular_price', 'N/A') or 'N/A')
    elif is_blocked:
        price_display = "Website can't be bypassed"
        status_display = 'blocked'
        title_display = 'N/A'
        rating_display = 'N/A'
        avail_display = 'N/A'
        avail_msg_display = 'N/A'
        pack_display = 'N/A'
        delivery_display = 'N/A'
        pickup_display = 'N/A'
        sale_regular_display = 'N/A'
    else:
        price_display = 'Part not found'
        status_display = 'error'
        title_display = 'N/A'
        rating_display = 'N/A'
        avail_display = 'N/A'
        avail_msg_display = 'N/A'
        pack_display = 'N/A'
        delivery_display = 'N/A'
        pickup_display = 'N/A'
        sale_regular_display = 'N/A'

    return {
        'retailer': retailer,
        'retailer_code': r_config["code"],
        'currency': parsed.get('currency', r_config["currency"]),
        'search_url': parsed.get('source_url', search_url),
        'screenshot_url': '',
        'status': status_display,
        'product_title': title_display,
        'price': price_display,
        'rrp_message': parsed.get('rrp_message', 'N/A'),
        'rating': rating_display,
        'availability': avail_display,
        'availability_message': avail_msg_display,
        'delivery_message': delivery_display,
        'in_store_pickup': pickup_display,
        'sale_and_regular_price': sale_regular_display,
        'pack_size': pack_display,
        'confidence': parsed.get('confidence', 0.0),
        'found': found
    }
