# --- serpapi_search.py ---
# Live auto-parts price search powered by SerpApi (Google Search + Google Shopping).
#
# This replaces the previous "Gemini agent live web search" (Path A grounding).
# Instead of asking an LLM to browse the web, we query Google directly through
# SerpApi and parse the structured JSON it returns (organic_results,
# shopping_results, immersive_products, etc. — exactly the schema documented in
# the sample payload the user provided).
#
# Config / secrets (resolved by app_config):
#   SerpApi key       — fetched from GCP Secret Manager (SERPAPI_SECRET_NAME in
#                       config.yaml); SERPAPI_API_KEY env var is an optional fallback.
#   SERPAPI_LOCATION  — optional default location (env > config.yaml > built-in default)

import os
import re
import time
import json
import logging
import threading
import urllib.parse
from contextlib import contextmanager

import requests
from curl_cffi import requests as c_requests
from dotenv import load_dotenv

import app_config

load_dotenv()

logger = logging.getLogger("SerpApi")

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
DEFAULT_LOCATION = app_config.get_location()

# ---------------------------------------------------------------------------
# Retailer matching — how to recognise a result as belonging to a retailer.
# `domain`  → matched against any URL field in the result.
# `tokens`  → matched (lower-cased, substring) against the merchant "source" name.
# ---------------------------------------------------------------------------
RETAILER_MATCH = {
    "AutoZone":      {"domain": "autozone.com",          "tokens": ["autozone"]},
    "O'Reilly":      {"domain": "oreillyauto.com",       "tokens": ["o'reilly", "oreilly", "o reilly"]},
    "Advance Auto":  {"domain": "advanceautoparts.com",  "tokens": ["advance auto"]},
    "NAPA":          {"domain": "napaonline.com",        "tokens": ["napa"]},
    "Amazon":        {"domain": "amazon.com",            "tokens": ["amazon"]},
    "Summit Racing": {"domain": "summitracing.com",      "tokens": ["summit racing", "summit"]},
    "Walmart":       {"domain": "walmart.com",           "tokens": ["walmart"]},
    "CarParts":      {"domain": "carparts.com",          "tokens": ["carparts", "car parts", "carparts.com"], "strict_domain_only": True},
}

RETAILER_CARD_META = {
    "AutoZone": {"code": "autozone", "currency": "USD"},
    "O'Reilly": {"code": "oreilly", "currency": "USD"},
    "Advance Auto": {"code": "advanceauto", "currency": "USD"},
    "NAPA": {"code": "napa", "currency": "USD"},
    "Amazon": {"code": "amazon", "currency": "USD"},
    "Summit Racing": {"code": "summitracing", "currency": "USD"},
    "Walmart": {"code": "walmart", "currency": "USD"},
    "CarParts": {"code": "carparts", "currency": "USD"},
}

# Simple in-process cache so scraping several retailers for the SAME part
# re-uses a single SerpApi response (saves credits + latency).
_CACHE = {}
_CACHE_TTL = int(os.environ.get("SERPAPI_CACHE_TTL_SECONDS", "900"))
_CACHE_LOCK = threading.Lock()
_RAW_CAPTURE = threading.local()


@contextmanager
def capture_raw_responses(sink):
    """
    Capture raw SerpApi payloads in the current thread.

    The caller supplies a mutable list (sink) that receives one dict per
    SerpApi request, including cache hits.
    """
    previous_sink = getattr(_RAW_CAPTURE, "sink", None)
    _RAW_CAPTURE.sink = sink
    try:
        yield sink
    finally:
        _RAW_CAPTURE.sink = previous_sink


def _record_raw_response(query_params, data):
    sink = getattr(_RAW_CAPTURE, "sink", None)
    if sink is None:
        return
    sink.append(
        {
            "engine": query_params.get("engine"),
            "query": query_params.get("q"),
            "location": query_params.get("location"),
            "data": data,
        }
    )


# ---------------------------------------------------------------------------
# Low-level SerpApi request
# ---------------------------------------------------------------------------

def _get_api_key(api_key=None):
    key = (api_key or "").strip()
    if not key:
        key = (app_config.get_serpapi_key() or "").strip()
    if not key:
        raise ValueError(
            "SerpApi key not found. Ensure SERPAPI_SECRET_NAME in config.yaml points to a "
            "readable GCP Secret Manager secret and your gcloud auth is valid "
            "(run `gcloud auth login`). SERPAPI_API_KEY in the environment is an optional fallback."
        )
    return key


def _should_retry_direct(error: Exception) -> bool:
    """
    Whether a first-attempt failure should be retried on a direct (no-proxy)
    connection. Covers proxy errors as well as timeouts/connection errors that
    happen when an inherited HTTP(S)_PROXY env var points at a dead/stale proxy.
    """
    if isinstance(
        error,
        (
            requests.exceptions.ProxyError,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    ):
        return True

    text = str(error).lower()
    retry_markers = (
        "proxyerror",
        "cannot connect to proxy",
        "tunnel connection failed",
        "proxy",
        "timed out",
        "timeout",
        "connection aborted",
        "connection reset",
        "failed to establish a new connection",
    )
    return any(marker in text for marker in retry_markers)


# Whether SerpApi requests should honor proxy configuration (HTTP(S)_PROXY env
# vars AND OS/system proxy settings). Default is DIRECT (no proxy) because
# SerpApi is a public API and a stale local/system proxy causes read timeouts.
# In a proxy-required deployment (e.g. Ford Cloud Run), set SERPAPI_TRUST_ENV=1.
_SERPAPI_TRUST_ENV = os.environ.get("SERPAPI_TRUST_ENV", "0").strip().lower() not in ("0", "false", "no", "")

# Read timeout (seconds) for SerpApi calls. The attempt-2 domain-restricted
# retry query is much slower on Google (~20-24s), so it needs a longer timeout
# than the default single-query call.
_SERPAPI_RETRY_TIMEOUT = int(os.environ.get("SERPAPI_RETRY_TIMEOUT", "45"))

# Number of Google result pages to fetch per query (pagination via the `start`
# param: page 1 = start 0, page 2 = start 10, ...). Merging several pages widens
# retailer coverage without changing the response shape.
_SERPAPI_PAGES = int(os.environ.get("SERPAPI_PAGES", "3"))


def _serpapi_get(query_params: dict, timeout, trust_env: bool):
    session = requests.Session()
    session.trust_env = trust_env
    return session.get(SERPAPI_ENDPOINT, params=query_params, timeout=timeout)


def _serpapi_get_with_proxy_fallback(query_params: dict, timeout):
    primary_trust_env = _SERPAPI_TRUST_ENV
    try:
        return _serpapi_get(query_params, timeout, primary_trust_env)
    except requests.exceptions.RequestException as first_error:
        if not _should_retry_direct(first_error):
            raise Exception(f"SerpApi request failed: {first_error}") from first_error

        # Retry once with the opposite proxy mode. This makes the request work
        # whether the failure was a dead proxy (retry direct) or a network that
        # requires a proxy (retry via proxy).
        fallback_trust_env = not primary_trust_env
        logger.warning(
            "SerpApi request failed (%s) with trust_env=%s; retrying with trust_env=%s.",
            type(first_error).__name__,
            primary_trust_env,
            fallback_trust_env,
        )
        try:
            return _serpapi_get(query_params, timeout, fallback_trust_env)
        except requests.exceptions.RequestException as fallback_error:
            raise Exception(
                "SerpApi request failed on both direct and proxy connections. "
                f"primary_error={first_error}; fallback_error={fallback_error}"
            ) from fallback_error


def serpapi_request(params, api_key=None, use_cache=True, timeout=15):
    """
    Performs a raw SerpApi request and returns the parsed JSON dict.
    `params` must include at least `engine` and `q`.
    """
    key = _get_api_key(api_key)
    query_params = {
        "api_key": key,
        "google_domain": "google.com",
        "gl": "us",
        "hl": "en",
        **params,
    }

    cache_key = None
    if use_cache:
        cache_key = "|".join(
            f"{k}={v}" for k, v in sorted(query_params.items()) if k != "api_key"
        )
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached and (time.time() - cached[0] < _CACHE_TTL):
            _record_raw_response(query_params, cached[1])
            return cached[1]

    resp = _serpapi_get_with_proxy_fallback(query_params, timeout)
    try:
        data = resp.json()
    except ValueError:
        raise Exception(f"SerpApi returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]}")

    if resp.status_code != 200:
        err = data.get("error") if isinstance(data, dict) else resp.text[:300]
        raise Exception(f"SerpApi error (HTTP {resp.status_code}): {err}")
    if isinstance(data, dict) and data.get("error"):
        raise Exception(f"SerpApi error: {data['error']}")

    if use_cache and cache_key:
        with _CACHE_LOCK:
            _CACHE[cache_key] = (time.time(), data)
            # cheap eviction so the cache never grows unbounded
            if len(_CACHE) > 256:
                oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
                _CACHE.pop(oldest, None)

    _record_raw_response(query_params, data)
    return data


# Result blocks that accumulate across pages; every other key (product_result,
# search_metadata, ...) is taken from the first page so the merged response keeps
# the same shape as a single-page response.
_PAGINATED_LIST_KEYS = (
    "organic_results",
    "shopping_results",
    "immersive_products",
    "inline_shopping_results",
)


def _merge_serpapi_pages(pages):
    """Merge several SerpApi page responses into one response dict.

    List result blocks are concatenated across pages (de-duplicated by link);
    all other keys are inherited from the first page, so downstream extraction
    and the persisted JSON see the same structure as before — just with more
    organic_results.

    Each merged result item is tagged with `page_number` (1-based) recording
    the Google result page it came from, so the persisted JSON keeps a per-item
    reference to its source page. De-duplicated items keep the page number of
    the first page they appeared on.
    """
    pages = [p for p in pages if isinstance(p, dict)]
    if not pages:
        return {}

    merged = dict(pages[0])
    for key in _PAGINATED_LIST_KEYS:
        combined = []
        seen = set()
        for page_index, page in enumerate(pages):
            page_number = page_index + 1
            for item in (page.get(key) or []):
                if isinstance(item, dict):
                    dedupe = item.get("link") or item.get("product_link") or item.get("title")
                    if dedupe and dedupe in seen:
                        continue
                    if dedupe:
                        seen.add(dedupe)
                    # Shallow copy so the source page number is recorded without
                    # mutating cached/raw-captured page objects.
                    item = {**item, "page_number": page_number}
                combined.append(item)
        if combined:
            merged[key] = combined
    return merged


def _serpapi_request_paginated(params, *, api_key=None, timeout, pages=None):
    """Fetch up to `pages` Google result pages (start=0,10,20,...) and merge them.

    Page 1 errors propagate (same behavior as a single request). Later pages that
    legitimately have no more results stop pagination without failing the query.
    """
    page_count = pages if pages is not None else _SERPAPI_PAGES
    collected = []
    for page_index in range(max(1, page_count)):
        page_params = dict(params)
        page_params["start"] = page_index * 10
        try:
            data = serpapi_request(page_params, api_key=api_key, timeout=timeout)
        except Exception as e:
            if page_index == 0:
                raise
            if "hasn't returned any results" in str(e).lower():
                break
            logger.warning("SerpApi pagination stopped at page %s: %s", page_index + 1, e)
            break
        collected.append(data)
        # No organic results on this page means there are no further pages.
        if not (data.get("organic_results") or []):
            break
    return _merge_serpapi_pages(collected)


# ---------------------------------------------------------------------------
# Matching / parsing helpers
# ---------------------------------------------------------------------------

def _url_matches_domain(url, domain):
    if not url or not domain:
        return False

    text = str(url).strip().lower()
    domain = str(domain).strip().lower()
    if not text or not domain:
        return False

    try:
        parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
        host = parsed.netloc.split("@")[-1].split(":")[0]
        if host == domain or host.endswith(f".{domain}"):
            return True
    except Exception:
        pass

    # Fallback for displayed_link style strings that are not valid URLs.
    return re.search(rf"(?:^|[^a-z0-9.])(?:[a-z0-9-]+\.)*{re.escape(domain)}(?:[^a-z0-9.]|$)", text) is not None


def _source_matches_token(source, token):
    source_norm = re.sub(r"[^a-z0-9]+", " ", str(source or "").lower()).strip()
    token_norm = re.sub(r"[^a-z0-9]+", " ", str(token or "").lower()).strip()
    if not source_norm or not token_norm:
        return False
    return f" {token_norm} " in f" {source_norm} "


def _matches_retailer(item, retailer):
    """True if a SerpApi result item appears to belong to the given retailer."""
    match = RETAILER_MATCH.get(retailer, {})
    domain = match.get("domain", "")
    tokens = match.get("tokens", [])
    strict_domain_only = bool(match.get("strict_domain_only", False))

    # 1. URL-based match (most reliable)
    for field in ("link", "product_link", "redirect_link", "source_link", "displayed_link"):
        url = item.get(field, "")
        if _url_matches_domain(url, domain):
            return True

    if strict_domain_only:
        return False

    # 2. Merchant/source name match (field names vary by SerpApi block)
    for field in ("source", "seller", "merchant", "merchant_name", "store_name"):
        source = str(item.get(field, "")).lower()
        if not source:
            continue
        for tok in tokens:
            if _source_matches_token(source, tok):
                return True

    return False


def _retailer_query_hint(retailer):
    """Human-readable retailer token for Google queries (CarParts -> Car Parts)."""
    hint = str(retailer or "").strip()
    if not hint:
        return hint
    hint = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", hint)
    return hint


def _normalise_part(value):
    """Normalize part numbers for tolerant exact matching (RS-55047A == RS55047A)."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _part_match_needles(part_number):
    """
    Build high-signal normalized needles for part matching.

    Why:
    Users sometimes type "<brand> <part>" in the part field (for example
    "KYB 3440040"). A strict contiguous match on "kyb3440040" fails against
    titles like "KYB Gas Shock 3440040". We keep strictness by only adding
    strong token needles (digit-bearing tokens, length >= 4).
    """
    raw = str(part_number or "").strip()
    full = _normalise_part(raw)
    needles = [full] if full else []

    token_candidates = [
        _normalise_part(tok)
        for tok in re.split(r"[^A-Za-z0-9]+", raw)
        if tok and _normalise_part(tok)
    ]
    for tok in token_candidates:
        if tok == full:
            continue
        if len(tok) < 4:
            continue
        has_digit = any(ch.isdigit() for ch in tok)
        has_alpha = any(ch.isalpha() for ch in tok)
        # Keep meaningful part-like tokens such as 3440040, RS55047A.
        if has_digit and (len(tok) >= 5 or has_alpha):
            needles.append(tok)

    # De-duplicate while preserving order
    unique = []
    for needle in needles:
        if needle and needle not in unique:
            unique.append(needle)
    return unique


def _item_text(item, include_urls=False):
    """Join searchable SerpApi fields without letting the query URL create false matches."""
    fields = [
        "title",
        "snippet",
        "description",
        "part_number",
        "product_id",
        "source",
    ]
    if include_urls:
        fields.extend(["link", "product_link", "redirect_link", "source_link", "displayed_link"])

    values = [str(item.get(field, "")) for field in fields]
    extensions = item.get("extensions")
    if isinstance(extensions, list):
        values.extend(str(ext) for ext in extensions)
    return " ".join(values)


def _matches_part_number(item, part_number, include_urls=False):
    """
    True when the exact part number appears in product text. URLs are only used
    for organic results; Shopping can return Google URLs containing the query,
    which would otherwise make nearby products look like exact matches.
    """
    needles = _part_match_needles(part_number)
    if not needles:
        return True
    haystack = _normalise_part(_item_text(item, include_urls=include_urls))
    return any(needle in haystack for needle in needles)


def _matches_part_title(item, part_number):
    """Strict-enough check: title must contain a high-signal part needle."""
    needles = _part_match_needles(part_number)
    if not needles:
        return True
    title_norm = _normalise_part(item.get("title", ""))
    return any(needle in title_norm for needle in needles)


def _matches_requested_brand_title(item, brand_name):
    """Require the leading requested brand token in a generic shopping title."""
    brand_tokens = re.findall(r"[A-Za-z0-9]+", str(brand_name or ""))
    if not brand_tokens:
        return False
    brand_token = _normalise_part(brand_tokens[0])
    if len(brand_token) < 3:
        return False
    return brand_token in _normalise_part(item.get("title", ""))


def _shopping_title_has_conflicting_part(item, part_number):
    """Reject generic shopping cards that explicitly name another part code."""
    requested = _normalise_part(part_number)
    for raw_token in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9-]{2,}\b", str(item.get("title", ""))):
        token = _normalise_part(raw_token)
        if not token or token == requested:
            continue
        has_digit = any(char.isdigit() for char in token)
        has_alpha = any(char.isalpha() for char in token)
        if has_digit and has_alpha:
            return True
        if requested.isdigit() and token.isdigit() and len(token) >= 4:
            return True
    return False


def _title_has_conflicting_part_number(item, part_number):
    """Reject organic results whose title clearly names a different RS part."""
    needle = _normalise_part(part_number)
    title = str(item.get("title", ""))
    # Rancho product lines such as RS5000X are not part numbers; real part
    # numbers in this feed are longer, e.g. RS55047A / RS999047A.
    for match in re.findall(r"\bRS[\s-]?\d{5,6}[A-Z]?\b", title, flags=re.IGNORECASE):
        if _normalise_part(match) != needle:
            return True
    return False


def _contextual_snippet_price(item, part_number):
    """
    Extract a price only when it appears close to the exact part number in the
    Google snippet. This handles category/search snippets such as:
    "... Rancho RS5000X Shock Absorbers RS55047A ... $75.99 ..."
    without trusting unrelated prices elsewhere on the page.
    """
    snippet = str(item.get("snippet", ""))
    if not snippet:
        return None

    needles = _part_match_needles(part_number)
    compact = _normalise_part(snippet)
    present_needles = [needle for needle in needles if needle in compact]
    if not present_needles:
        return None

    # Prefer the longest matching needle as the part anchor.
    anchor = sorted(present_needles, key=len, reverse=True)[0]
    part_pattern = r"\b" + r"[\s\-_/]*".join(re.escape(ch) for ch in anchor) + r"\b"
    part_match = re.search(part_pattern, snippet, flags=re.IGNORECASE)
    if not part_match:
        return None

    prices = list(re.finditer(r"\$\s?([\d,]+(?:\.\d{2})?)", snippet))
    if not prices:
        return None

    part_start = part_match.start()
    part_end = part_match.end()
    best_price = None
    best_distance = None
    for price_match in prices:
        # In Google snippets for product rows, the product/part appears before
        # the price. Prices before the part are commonly unrelated filters,
        # shipping thresholds, or page chrome (for example "Free Shipping $125").
        if price_match.start() < part_end:
            continue
        distance = price_match.start() - part_end

        # Keep prices that are in the same result-row sentence/fragment.
        if distance > 180:
            continue

        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_price = price_match.group(1).replace(",", "")

    return best_price


def _price_to_str(value):
    """Normalise an extracted price into a plain numeric string ('13.86')."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    s = str(value)
    m = re.search(r"[\d,]+(?:\.\d+)?", s.replace(" ", ""))
    if not m:
        return None
    return m.group(0).replace(",", "")


def _has_usable_price(value):
    if isinstance(value, (int, float)):
        return float(value) > 0
    text = str(value or "").strip()
    if not text:
        return False
    if text.lower() in {"n/a", "na", "none", "null", "price not available", "part not found", "error"}:
        return False
    parsed = _price_to_str(text)
    if parsed is None:
        return False
    try:
        return float(parsed) > 0
    except ValueError:
        return False


def _extracted_price(item):
    """Pull a numeric price out of a shopping/immersive result item."""
    for k in ("extracted_price", "price"):
        if item.get(k) is not None:
            p = _price_to_str(item.get(k))
            if p:
                return p
    return None


def _format_money(value):
    price = _price_to_str(value)
    return f"${price}" if price else None


def _extract_msrp(item, current_price=None):
    """Extract MSRP/list/original price when SerpApi exposes it generically."""
    for key in (
        "original_price",
        "extracted_original_price",
        "list_price",
        "extracted_list_price",
        "retail_price",
        "extracted_retail_price",
        "msrp",
        "extracted_msrp",
    ):
        if item.get(key) is None:
            continue
        msrp = _format_money(item.get(key))
        if msrp:
            return msrp

    rich = item.get("rich_snippet", {}) or {}
    for section in ("top", "bottom"):
        det = (rich.get(section, {}) or {}).get("detected_extensions", {}) or {}
        for key in ("original_price", "list_price", "retail_price", "msrp"):
            if det.get(key) is not None:
                msrp = _format_money(det.get(key))
                if msrp:
                    return msrp

    text = _item_text(item, include_urls=False)
    match = re.search(
        r"\b(?:msrp|list price|original(?: price)?|retail price|was)\s*:?\s*\$?\s*([\d,]+(?:\.\d{2})?)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return f"${match.group(1).replace(',', '')}"

    return "N/A"


def _extract_rating(item):
    """Extract rating from structured fields, rich snippets, extensions, or text."""
    value = item.get("rating")
    if value not in (None, ""):
        return str(value)

    rich = item.get("rich_snippet", {}) or {}
    for section in ("top", "bottom"):
        det = (rich.get(section, {}) or {}).get("detected_extensions", {}) or {}
        for key in ("rating", "stars"):
            if det.get(key) not in (None, ""):
                return str(det.get(key))

    text = _item_text(item, include_urls=False)
    patterns = (
        r"\b([0-5](?:\.\d+)?)\s*(?:out of|/)\s*5\b",
        r"\b([0-5](?:\.\d+)?)\s*stars?\b",
        r"\brating\s*:?\s*([0-5](?:\.\d+)?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    return "N/A"


def _extract_pack_size(item):
    """
    Detect multi-pack quantities from product text.

    Examples:
      "4 PCS NEW", "4 pieces", "pack of 4", "4-pack", "4 count", "4 ct",
      "2 pk", "set of 6"
    """
    text = _item_text(item, include_urls=False)
    if not text:
        return "1"

    patterns = (
        r"\b(\d{1,3})\s*[- ]?\s*(?:pc|pcs|piece|pieces)\b",
        r"\b(?:pack|package|set|box|case)\s+of\s+(\d{1,3})\b",
        r"\b(\d{1,3})\s*[- ]?\s*(?:pack|pk|count|ct)\b",
        r"\bqty\.?\s*:?\s*(\d{1,3})\b",
        r"\bquantity\s*:?\s*(\d{1,3})\b",
        r"\bx\s*(\d{1,3})\b",
    )

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            qty = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 1 < qty <= 100:
            return str(qty)

    return "1"


def _stringify_field(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value).strip()
    return text or None


def _extensions_texts(item):
    values = []
    extensions = item.get("extensions")
    if isinstance(extensions, list):
        values.extend(str(ext).strip() for ext in extensions if str(ext).strip())
    rich = item.get("rich_snippet", {}) or {}
    for section in ("top", "bottom"):
        ext_list = (rich.get(section, {}) or {}).get("extensions")
        if isinstance(ext_list, list):
            values.extend(str(ext).strip() for ext in ext_list if str(ext).strip())
    return values


def _extract_delivery_message(item):
    for key in (
        "delivery",
        "delivery_message",
        "shipping",
        "shipping_message",
        "shipping_info",
        "shipping_time",
        "delivery_info",
    ):
        text = _stringify_field(item.get(key))
        if text:
            return text

    rich = item.get("rich_snippet", {}) or {}
    for section in ("top", "bottom"):
        det = (rich.get(section, {}) or {}).get("detected_extensions", {}) or {}
        for key in ("delivery", "shipping", "shipping_message"):
            text = _stringify_field(det.get(key))
            if text:
                return text

    for ext in _extensions_texts(item):
        if re.search(r"\b(deliver|delivery|ship|shipping|arrive|arrival)\b", ext, flags=re.IGNORECASE):
            return ext

    return "N/A"


def _extract_in_store_pickup(item):
    for key in (
        "in_store_pickup",
        "pickup",
        "pickup_message",
        "store_pickup",
        "curbside_pickup",
    ):
        text = _stringify_field(item.get(key))
        if text:
            return text

    for ext in _extensions_texts(item):
        if re.search(r"\b(pickup|pick up|in[- ]store)\b", ext, flags=re.IGNORECASE):
            return ext

    return "N/A"


def _extract_availability_message(item, in_stock_default=False):
    for key in (
        "availability_message",
        "availability",
        "stock_status",
        "stock",
        "condition",
    ):
        text = _stringify_field(item.get(key))
        if text:
            return text

    rich = item.get("rich_snippet", {}) or {}
    for section in ("top", "bottom"):
        det = (rich.get(section, {}) or {}).get("detected_extensions", {}) or {}
        for key in ("availability", "stock", "stock_status"):
            text = _stringify_field(det.get(key))
            if text:
                return text

    for ext in _extensions_texts(item):
        if re.search(r"\b(in stock|out of stock|backorder|backordered|unavailable|limited)\b", ext, flags=re.IGNORECASE):
            return ext

    return "In Stock" if in_stock_default else "Availability not shown"


def _extract_sale_and_regular_price(item, current_price=None):
    sale = _price_to_str(current_price)
    if not sale:
        sale = _extracted_price(item)

    regular = None
    for key in (
        "old_price",
        "extracted_old_price",
        "original_price",
        "extracted_original_price",
        "list_price",
        "extracted_list_price",
        "retail_price",
        "extracted_retail_price",
        "msrp",
        "extracted_msrp",
    ):
        if item.get(key) is None:
            continue
        regular = _price_to_str(item.get(key))
        if regular:
            break

    if sale and regular and sale != regular:
        return f"${sale} (was ${regular})"
    if sale:
        return f"${sale}"
    if regular:
        return f"${regular}"
    return "N/A"


def _build_result(retailer, *, part_number, title, price, url,
                  rating=None, in_stock=True, pack_size="1",
                  rrp_message="N/A", confidence=0.9, found=True, currency="USD",
                  delivery_message="N/A", in_store_pickup="N/A",
                  availability_message="Availability not shown",
                  sale_and_regular_price="N/A"):
    return {
        "part_number":   part_number,
        "product_title": title or "N/A",
        "retailer":      retailer,
        "price":         price if price is not None else 0.0,
        "currency":      currency,
        "rrp_message":   rrp_message or "N/A",
        "pack_size":     str(pack_size) if pack_size else "1",
        "rating":        str(rating) if rating not in (None, "") else "N/A",
        "in_stock":      bool(in_stock),
        "delivery_message": str(delivery_message or "N/A"),
        "in_store_pickup": str(in_store_pickup or "N/A"),
        "availability_message": str(availability_message or "Availability not shown"),
        "sale_and_regular_price": str(sale_and_regular_price or "N/A"),
        "source_url":    url or "",
        "confidence":    confidence,
        "found":         found,
    }


def _not_found(retailer, part_number):
    return {
        "part_number":   part_number,
        "product_title": "N/A",
        "retailer":      retailer,
        "price":         0.0,
        "currency":      "USD",
        "rrp_message":   "N/A",
        "pack_size":     "N/A",
        "rating":        "N/A",
        "in_stock":      False,
        "delivery_message": "N/A",
        "in_store_pickup": "N/A",
        "availability_message": "N/A",
        "sale_and_regular_price": "N/A",
        "source_url":    "",
        "confidence":    1.0,
        "found":         False,
    }


# ---------------------------------------------------------------------------
# Extraction from the different SerpApi result blocks
# ---------------------------------------------------------------------------

def _scan_shopping(data, retailer, part_number, brand_name=""):
    """Look through shopping_results / immersive_products for a retailer match."""
    blocks = []
    blocks.extend(data.get("shopping_results", []) or [])
    blocks.extend(data.get("immersive_products", []) or [])
    blocks.extend(data.get("inline_shopping_results", []) or [])

    best = None
    for item in blocks:
        if not _matches_retailer(item, retailer):
            continue
        title_matches = _matches_part_title(item, part_number)
        generic_title_matches = (
            not title_matches
            and _matches_requested_brand_title(item, brand_name)
            and not _shopping_title_has_conflicting_part(item, part_number)
        )
        if not title_matches and not generic_title_matches:
            logger.info(
                f"[SerpApi][{retailer}] rejected shopping near-match without exact part: "
                f"{str(item.get('title', ''))[:80]}"
            )
            continue
        price = _extracted_price(item)
        if price is None:
            continue
        url = (
            item.get("product_link")
            or item.get("link")
            or item.get("source_link")
            or item.get("redirect_link")
            or ""
        )
        candidate = _build_result(
            retailer,
            part_number=part_number,
            title=item.get("title"),
            price=price,
            url=url,
            rating=_extract_rating(item),
            in_stock=True,
            pack_size=_extract_pack_size(item),
            rrp_message=_extract_msrp(item, current_price=price),
            delivery_message=_extract_delivery_message(item),
            in_store_pickup=_extract_in_store_pickup(item),
            availability_message=_extract_availability_message(item, in_stock_default=True),
            sale_and_regular_price=_extract_sale_and_regular_price(item, current_price=price),
            confidence=0.92 if title_matches else 0.82,
        )
        # Prefer the cheapest legit listing for this retailer
        if best is None or float(price) < float(best["price"]):
            best = candidate
    return best


def _scan_organic(data, retailer, part_number, allow_url_only=False):
    """Look through organic_results for a retailer match (URL + optional price)."""
    best = None
    best_score = -1

    for item in data.get("organic_results", []) or []:
        if not _matches_retailer(item, retailer):
            continue

        title_matches = _matches_part_title(item, part_number)
        text_matches = _matches_part_number(item, part_number, include_urls=True)
        contextual_price = _contextual_snippet_price(item, part_number)
        if not title_matches and not contextual_price and not (allow_url_only and text_matches):
            logger.info(
                f"[SerpApi][{retailer}] rejected organic near-match without exact part: "
                f"{str(item.get('title', ''))[:80]}"
            )
            continue

        if _title_has_conflicting_part_number(item, part_number):
            logger.info(
                f"[SerpApi][{retailer}] rejected organic conflicting title: "
                f"{str(item.get('title', ''))[:80]}"
            )
            continue

        url = item.get("link") or item.get("redirect_link") or ""
        title = item.get("title")

        # Try to find a price inside the rich snippet. Prices from
        # detected_extensions are structured (Google-parsed) and therefore
        # trustworthy even when the title does not repeat the part number.
        price = None
        structured_price = False
        range_low = None  # low end when Google reports a price range
        rich = item.get("rich_snippet", {}) or {}
        for section in ("top", "bottom"):
            det = (rich.get(section, {}) or {}).get("detected_extensions", {}) or {}
            if range_low is None and det.get("price_from") is not None:
                range_low = _price_to_str(det.get("price_from"))
            for k in ("price", "price_from", "price_to"):
                if det.get(k) is not None:
                    price = _price_to_str(det.get(k))
                    if price:
                        structured_price = True
                        break
            if price:
                break

        # When Google reports a price range (price_from/price_to) instead of a
        # single price, the real selling price is usually in the snippet text as
        # "current price $X". Use that as the actual price, and keep the range's
        # low end for the sale/regular field.
        sale_regular_override = None
        m_current = re.search(
            r"current price\s*\$?\s*([\d,]+(?:\.\d{2})?)",
            f"{item.get('title', '')} {item.get('snippet', '')} {item.get('description', '')}",
            flags=re.IGNORECASE,
        )
        current_price = m_current.group(1).replace(",", "") if m_current else None
        if current_price and range_low:
            price = current_price
            structured_price = True
            sale_regular_override = f"${range_low}"

        # Retailers such as Advance Auto express promo pricing in the snippet as
        # "$<regular> As low as $<sale> (20% Off)". Google's detected_extensions
        # can report a different (member/online) price here, so trust the snippet:
        # show the regular price as the price and the discounted price as sale.
        m_as_low = re.search(
            r"\$\s*([\d,]+(?:\.\d{2})?)\s+as low as\s+\$\s*([\d,]+(?:\.\d{2})?)",
            f"{item.get('title', '')} {item.get('snippet', '')} {item.get('description', '')}",
            flags=re.IGNORECASE,
        )
        if m_as_low:
            regular_price = m_as_low.group(1).replace(",", "")
            as_low_price = m_as_low.group(2).replace(",", "")
            price = regular_price
            structured_price = True
            sale_regular_override = f"${as_low_price} (was ${regular_price})"

        # Fall back to scraping a $-price out of the snippet text
        if not price:
            if title_matches:
                snippet = str(item.get("snippet", ""))
                m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", snippet)
                if m:
                    price = m.group(1).replace(",", "")
            else:
                price = contextual_price

        # Snippet-only matches prove the product exists, but loosely-scraped
        # snippet prices are only trusted when contextually close to the part.
        # A structured detected_extensions price on a page whose text/URL
        # contains the exact part number (text_matches) stays trusted.
        if allow_url_only and not title_matches and not contextual_price:
            if not (structured_price and text_matches):
                price = None

        if not price and not allow_url_only:
            continue

        result_title = title if title_matches else f"{part_number} listing"

        # Fall back to Google's aggregated product rating when the retailer's own
        # listing has no rating (e.g. Amazon organic results expose no rating).
        rating = _extract_rating(item)
        if rating in (None, "", "N/A"):
            product_rating = (data.get("product_result") or {}).get("rating")
            if product_rating not in (None, ""):
                rating = str(product_rating)

        candidate = _build_result(
            retailer,
            part_number=part_number,
            title=result_title,
            price=price,
            url=url,
            in_stock=bool(price),
            pack_size=_extract_pack_size(item),
            rating=rating,
            rrp_message=_extract_msrp(item, current_price=price),
            delivery_message=_extract_delivery_message(item),
            in_store_pickup=_extract_in_store_pickup(item),
            availability_message=_extract_availability_message(item, in_stock_default=bool(price)),
            sale_and_regular_price=sale_regular_override or _extract_sale_and_regular_price(item, current_price=price),
            confidence=0.78 if price else 0.55,
            found=bool(price) or allow_url_only,
        )

        score = 0
        if title_matches:
            score += 50
        if price:
            score += 25
        if contextual_price:
            score += 20
        url_lower = url.lower()
        if title_matches and any(part in url_lower for part in ("/parts/", "/dp/", "/p/")):
            score += 10
        if not title_matches and "search" in url_lower:
            score += 10
        if not title_matches and "/parts/" in url_lower:
            score -= 20
        if "search" not in url_lower:
            score += 5

        if score > best_score:
            best = candidate
            best_score = score

    if best:
        return best
    return None


def _extract_next_data_payload(html):
    match = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        str(html or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    raw_payload = match.group(1).strip()
    if not raw_payload:
        return None
    try:
        return json.loads(raw_payload)
    except Exception:
        return None


def _iter_walmart_search_items(next_data):
    props = next_data.get("props") if isinstance(next_data, dict) else None
    page_props = props.get("pageProps") if isinstance(props, dict) else None
    initial_data = page_props.get("initialData") if isinstance(page_props, dict) else None
    search_result = initial_data.get("searchResult") if isinstance(initial_data, dict) else None
    item_stacks = search_result.get("itemStacks") if isinstance(search_result, dict) else None
    if not isinstance(item_stacks, list):
        return

    for stack in item_stacks:
        if not isinstance(stack, dict):
            continue
        items = stack.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                yield item


def _scan_walmart_site_search(part_number, brand_name):
    """
    Fallback when Google index does not return exact Walmart SKU URLs.
    Parse Walmart's own search page JSON payload and extract the best SKU match.
    """
    query = " ".join(p for p in (str(brand_name or "").strip(), str(part_number or "").strip()) if p).strip()
    if not query:
        return None

    search_url = f"https://www.walmart.com/search/?query={urllib.parse.quote_plus(query)}"
    try:
        response = c_requests.get(search_url, impersonate="chrome124", timeout=25)
    except Exception as e:
        logger.warning(f"[SerpApi][Walmart] site-search fallback request failed: {e}")
        return None
    if response.status_code != 200:
        return None

    next_data = _extract_next_data_payload(response.text)
    if not isinstance(next_data, dict):
        return None

    best = None
    best_score = -1
    brand_needle = _normalise_part(brand_name)

    for item in _iter_walmart_search_items(next_data):
        name = str(item.get("name") or item.get("title") or "").strip()
        canonical = str(item.get("canonicalUrl") or item.get("url") or "").strip()
        if not canonical:
            continue
        url = canonical if canonical.startswith("http") else f"https://www.walmart.com{canonical}"

        pseudo = {
            "title": name,
            "link": url,
            "snippet": " ".join(
                str(item.get(k, "")) for k in (
                    "name",
                    "description",
                    "usItemId",
                    "id",
                    "offerId",
                )
            ),
        }

        title_matches = _matches_part_title(pseudo, part_number)
        text_matches = _matches_part_number(pseudo, part_number, include_urls=True)
        if not text_matches:
            continue

        raw_price_info = item.get("priceInfo")
        price_info: dict[str, object] = raw_price_info if isinstance(raw_price_info, dict) else {}
        price = (
            _price_to_str(price_info.get("linePrice"))
            or _price_to_str(price_info.get("itemPrice"))
            or _price_to_str(item.get("price"))
        )
        if not price:
            continue

        was_price = _price_to_str(price_info.get("wasPrice"))
        if was_price and was_price != price:
            sale_regular = f"${price} (was ${was_price})"
        else:
            sale_regular = f"${price}"

        delivery = "N/A"
        ship_price = _price_to_str(price_info.get("shipPrice"))
        if ship_price is not None:
            delivery = "Free delivery" if ship_price == "0" else f"${ship_price} delivery"

        availability_text = (
            _stringify_field(item.get("availabilityStatus"))
            or _stringify_field(item.get("availabilityText"))
            or "In stock"
        )

        score = 0
        score += 60 if title_matches else 0
        score += 20 if text_matches else 0
        score += 10 if brand_needle and brand_needle in _normalise_part(name) else 0
        score += 5 if "/ip/" in url.lower() else 0

        candidate = _build_result(
            "Walmart",
            part_number=part_number,
            title=name or f"{part_number} listing",
            price=price,
            url=url,
            rating=item.get("averageRating"),
            in_stock=not re.search(r"out\\s+of\\s+stock|unavailable", availability_text, flags=re.IGNORECASE),
            pack_size=_extract_pack_size(pseudo),
            rrp_message=_format_money(was_price) or "N/A",
            delivery_message=delivery,
            in_store_pickup="N/A",
            availability_message=availability_text,
            sale_and_regular_price=sale_regular,
            confidence=0.88,
        )

        if score > best_score:
            best = candidate
            best_score = score

    return best


def _parse_buying_options(options):
    rating = None
    availability_message = None
    delivery_message = "N/A"
    in_store_pickup = "N/A"

    if not isinstance(options, list):
        return {
            "rating": rating,
            "availability_message": availability_message,
            "delivery_message": delivery_message,
            "in_store_pickup": in_store_pickup,
        }

    for raw_option in options:
        text = str(raw_option or "").strip()
        if not text:
            continue

        lower = text.lower()

        if rating is None:
            rating_match = re.search(r"\b([0-5](?:\.\d+)?)\s*(?:/|out of)\s*5\b", text, flags=re.IGNORECASE)
            if rating_match:
                rating = rating_match.group(1)

        if availability_message is None and re.search(
            r"\b(in stock|out of stock|backorder|backordered|unavailable|limited)\b",
            lower,
            flags=re.IGNORECASE,
        ):
            availability_message = text

        if delivery_message == "N/A" and re.search(
            r"\b(deliver|delivery|ship|shipping|arrive|arrival)\b",
            lower,
            flags=re.IGNORECASE,
        ):
            delivery_message = text

        if in_store_pickup == "N/A" and re.search(
            r"\b(pickup|pick up|in[- ]store)\b",
            lower,
            flags=re.IGNORECASE,
        ):
            in_store_pickup = text

    return {
        "rating": rating,
        "availability_message": availability_message,
        "delivery_message": delivery_message,
        "in_store_pickup": in_store_pickup,
    }


def _scan_product_pricing(data, retailer, part_number):
    product_result = data.get("product_result") if isinstance(data, dict) else None
    if not isinstance(product_result, dict):
        return None

    pricing_entries = product_result.get("pricing")
    if not isinstance(pricing_entries, list):
        return None

    product_title = str(product_result.get("title") or "").strip()
    best = None
    best_score = -1

    for entry in pricing_entries:
        if not isinstance(entry, dict):
            continue

        buying_options = entry.get("buying_options")
        options_info = _parse_buying_options(buying_options)
        entry_title = str(entry.get("title") or product_title).strip() or f"{part_number} listing"
        source_url = str(entry.get("link") or entry.get("url") or "").strip()

        synthetic = {
            "title": entry_title,
            "snippet": " ".join(
                str(entry.get(key, "")) for key in ("description", "name", "store", "merchant", "seller")
            ).strip(),
            "description": str(entry.get("description") or ""),
            "source": str(entry.get("name") or entry.get("store") or entry.get("merchant") or ""),
            "merchant": str(entry.get("name") or entry.get("merchant") or ""),
            "merchant_name": str(entry.get("name") or entry.get("merchant") or ""),
            "store_name": str(entry.get("name") or entry.get("store") or ""),
            "link": source_url,
            "product_link": source_url,
            "price": entry.get("price"),
            "extracted_price": entry.get("extracted_price"),
            "original_price": entry.get("original_price"),
            "extracted_original_price": entry.get("extracted_original_price"),
            "rating": entry.get("rating") if entry.get("rating") not in (None, "") else product_result.get("rating"),
            "extensions": buying_options if isinstance(buying_options, list) else [],
        }
        if options_info["availability_message"]:
            synthetic["availability_message"] = options_info["availability_message"]
            synthetic["availability"] = options_info["availability_message"]
        if options_info["delivery_message"] != "N/A":
            synthetic["delivery_message"] = options_info["delivery_message"]
        if options_info["in_store_pickup"] != "N/A":
            synthetic["in_store_pickup"] = options_info["in_store_pickup"]
        if options_info["rating"] and synthetic.get("rating") in (None, "", "N/A"):
            synthetic["rating"] = options_info["rating"]

        if not _matches_retailer(synthetic, retailer):
            continue
        if not (_matches_part_title(synthetic, part_number) or _matches_part_number(synthetic, part_number)):
            continue

        price = _extracted_price(synthetic)
        availability_message = _extract_availability_message(synthetic, in_stock_default=bool(price))
        in_stock = not re.search(
            r"\b(out of stock|unavailable|backorder|backordered)\b",
            str(availability_message or ""),
            flags=re.IGNORECASE,
        )

        candidate = _build_result(
            retailer,
            part_number=part_number,
            title=entry_title,
            price=price,
            url=source_url,
            rating=_extract_rating(synthetic),
            in_stock=in_stock,
            pack_size=_extract_pack_size(synthetic),
            rrp_message=_extract_msrp(synthetic, current_price=price),
            delivery_message=_extract_delivery_message(synthetic),
            in_store_pickup=_extract_in_store_pickup(synthetic),
            availability_message=availability_message,
            sale_and_regular_price=_extract_sale_and_regular_price(synthetic, current_price=price),
            confidence=0.95 if price else 0.82,
            found=True,
        )

        score = 0
        if _has_usable_price(price):
            score += 60
        if _matches_part_title(synthetic, part_number):
            score += 25
        if _matches_part_number(synthetic, part_number):
            score += 10
        if source_url and RETAILER_MATCH.get(retailer, {}).get("domain", "") in source_url.lower():
            score += 5

        if score > best_score:
            best = candidate
            best_score = score

    return best


def _to_live_retailer_card(retailer, parsed):
    meta = RETAILER_CARD_META.get(retailer, {})
    found = bool((parsed or {}).get("found"))
    blocked = str((parsed or {}).get("status", "")).lower() == "blocked"

    if found:
        raw_price = (parsed or {}).get("price")
        if _has_usable_price(raw_price):
            price_display = str(raw_price)
        else:
            price_display = "Price not available"

        status_display = "success"
        title_display = str((parsed or {}).get("product_title", "N/A"))
        rating_display = str((parsed or {}).get("rating", "N/A"))
        raw_avail_msg = str((parsed or {}).get("availability_message", "")).strip()
        if not raw_avail_msg or raw_avail_msg.lower() == "n/a":
            avail_msg_display = "In Stock" if (parsed or {}).get("in_stock") else "Availability not shown"
        else:
            avail_msg_display = raw_avail_msg
        avail_display = avail_msg_display
        pack_display = str((parsed or {}).get("pack_size", "1"))
        delivery_display = str((parsed or {}).get("delivery_message", "N/A") or "N/A")
        pickup_display = str((parsed or {}).get("in_store_pickup", "N/A") or "N/A")
        sale_regular_display = str((parsed or {}).get("sale_and_regular_price", "N/A") or "N/A")
    elif blocked:
        price_display = "Website can't be bypassed"
        status_display = "blocked"
        title_display = "N/A"
        rating_display = "N/A"
        avail_display = "N/A"
        avail_msg_display = "N/A"
        pack_display = "N/A"
        delivery_display = "N/A"
        pickup_display = "N/A"
        sale_regular_display = "N/A"
    else:
        price_display = "Part not found"
        status_display = "error"
        title_display = "N/A"
        rating_display = "N/A"
        avail_display = "N/A"
        avail_msg_display = "N/A"
        pack_display = "N/A"
        delivery_display = "N/A"
        pickup_display = "N/A"
        sale_regular_display = "N/A"

    return {
        "retailer": retailer,
        "retailer_code": meta.get("code", "??"),
        "currency": (parsed or {}).get("currency", meta.get("currency", "USD")),
        "search_url": (parsed or {}).get("source_url", ""),
        "screenshot_url": "",
        "status": status_display,
        "product_title": title_display,
        "price": price_display,
        "rrp_message": (parsed or {}).get("rrp_message", "N/A"),
        "sale_and_regular_price": sale_regular_display,
        "rating": rating_display,
        "availability": avail_display,
        "availability_message": avail_msg_display,
        "delivery_message": delivery_display,
        "in_store_pickup": pickup_display,
        "pack_size": pack_display,
        "source_url": (parsed or {}).get("source_url", ""),
        "confidence": (parsed or {}).get("confidence", 0.0),
        "found": found,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def serpapi_search_google_product(part_number, brand_name, *, location=None, api_key=None):
    query = f"{part_number} {brand_name}".strip()
    loc = location or DEFAULT_LOCATION
    return _serpapi_request_paginated(
        {"engine": "google", "q": query, "location": loc},
        api_key=api_key,
        timeout=_SERPAPI_RETRY_TIMEOUT,
    )


def build_missing_sites_retry_query(part_number, brand_name, missing_retailers):
    """
    Build the attempt-2 query for the missing retailers using a valid Google
    site: filter, one per retailer domain, joined with OR, e.g.:

      "RS77149 RANCHO (site:advanceautoparts.com OR site:amazon.com OR ...)"

    This restricts Google to the listed retailer domains for retailers that
    were missing in attempt 1, while still using a single SerpApi call.
    """
    base = f"{part_number} {brand_name}".strip()

    domains = []
    name_only_hints = []
    for retailer in (missing_retailers or []):
        retailer_match = RETAILER_MATCH.get(retailer, {})
        domain = str(retailer_match.get("domain") or "").strip().lower()
        if domain:
            if domain not in domains:
                domains.append(domain)
            continue

        name_hint = _retailer_query_hint(retailer)
        if name_hint and name_hint not in name_only_hints:
            name_only_hints.append(name_hint)

    if not domains and not name_only_hints:
        return base

    query = base
    if domains:
        site_clause = " OR ".join(f"site:{domain}" for domain in domains)
        query = f"{query} ({site_clause})"
    if name_only_hints:
        query = f"{query} {' '.join(name_only_hints)}"
    return query.strip()


def serpapi_search_google_product_missing_sites(
    part_number,
    brand_name,
    missing_retailers,
    *,
    location=None,
    api_key=None,
):
    query = build_missing_sites_retry_query(part_number, brand_name, missing_retailers)
    loc = location or DEFAULT_LOCATION
    return _serpapi_request_paginated(
        {"engine": "google", "q": query, "location": loc},
        api_key=api_key,
        timeout=_SERPAPI_RETRY_TIMEOUT,
    )


def extract_retailer_results(data, part_number, brand_name, retailers):
    """
    Map one SerpApi `engine=google` response to fixed retailer cards.

    Priority per retailer:
      1) product_result.pricing
      2) organic_results
      3) shopping_results/immersive fallback blocks
    """
    if not isinstance(data, dict):
        data = {}

    results = []
    for retailer in retailers or []:
        if retailer not in RETAILER_MATCH:
            parsed = _not_found(retailer, part_number)
            results.append(_to_live_retailer_card(retailer, parsed))
            continue

        parsed = _scan_product_pricing(data, retailer, part_number)
        if not parsed:
            parsed = _scan_organic(data, retailer, part_number, allow_url_only=True)
        if not parsed:
            parsed = _scan_shopping(data, retailer, part_number, brand_name)
        if not parsed:
            parsed = _not_found(retailer, part_number)

        results.append(_to_live_retailer_card(retailer, parsed))

    return results

def serpapi_scrape_retailer(part_number, brand_name, retailer,
                            api_key=None, location=None):
    """
    Finds pricing for `brand_name part_number` at a specific `retailer` using
    SerpApi (Google Shopping first, then Google organic with a site: filter).

    Returns a dict matching the strict schema the orchestrator expects:
        part_number, product_title, retailer, price, currency, pack_size,
        rating, in_stock, source_url, confidence, found
    """
    if retailer not in RETAILER_MATCH:
        logger.warning(f"[SerpApi] Unknown retailer '{retailer}'.")
        return _not_found(retailer, part_number)

    loc = location or DEFAULT_LOCATION
    query = f'"{part_number}" {brand_name}'.strip()
    domain = RETAILER_MATCH[retailer]["domain"]
    retailer_hint = _retailer_query_hint(retailer)

    # --- Stage 1: Google Shopping (best source of real prices) ---------------
    try:
        shopping = serpapi_request({
            "engine":   "google_shopping",
            "q":        query,
            "location": loc,
        }, api_key=api_key)
        hit = _scan_shopping(shopping, retailer, part_number, brand_name)
        if hit:
            logger.info(f"[SerpApi][{retailer}] shopping hit: {hit['price']} — {hit['product_title'][:50]}")
            return hit
    except Exception as e:
        logger.warning(f"[SerpApi][{retailer}] shopping search failed: {e}")

    # --- Stage 2: exact product exists, but Google did not expose price -------
    # Some retailers (notably Amazon with delivery restrictions, and Summit
    # category/search pages) expose the exact product URL/title without a price.
    # Returning this as "Price not available" is more accurate than "not found".
    no_price_hit = None
    for fallback_query in (
        f'"{part_number}" "{retailer_hint}" {brand_name}',
        f"{query} site:{domain}",
        f"{part_number} {brand_name} site:{domain}",
    ):
        try:
            fallback = serpapi_request({
                "engine":   "google",
                "q":        fallback_query,
                "location": loc,
            }, api_key=api_key)
            hit = _scan_organic(fallback, retailer, part_number, allow_url_only=True)
            if hit:
                if hit.get("price") not in (None, "", 0, 0.0, "0", "0.0"):
                    logger.info(f"[SerpApi][{retailer}] exact product found with snippet price: {hit['price']}")
                    return hit
                if no_price_hit is None:
                    no_price_hit = hit
        except Exception as e:
            logger.warning(f"[SerpApi][{retailer}] exact-product fallback failed: {e}")

    # --- Stage 3: broad Google search, then filter by domain across blocks ---
    try:
        broad = serpapi_request({
            "engine":   "google",
            "q":        f"{query} {retailer_hint}",
            "location": loc,
        }, api_key=api_key)
        hit = _scan_shopping(broad, retailer, part_number, brand_name) or _scan_organic(broad, retailer, part_number)
        if hit:
            logger.info(f"[SerpApi][{retailer}] broad hit: price={hit['price']}")
            return hit
    except Exception as e:
        logger.warning(f"[SerpApi][{retailer}] broad search failed: {e}")

    if retailer == "Walmart":
        walmart_site_hit = _scan_walmart_site_search(part_number, brand_name)
        if walmart_site_hit:
            logger.info(
                f"[SerpApi][{retailer}] walmart-site fallback hit: "
                f"price={walmart_site_hit.get('price')}"
            )
            return walmart_site_hit

    if no_price_hit:
        logger.info(f"[SerpApi][{retailer}] exact product found without price: {no_price_hit['source_url'][:60]}")
        return no_price_hit

    logger.info(f"[SerpApi][{retailer}] no match for '{query}'.")
    return _not_found(retailer, part_number)


def serpapi_find_product_url(part_number, brand_name, retailer,
                             api_key=None, location=None):
    """
    Lightweight helper that returns just a product URL on the retailer's site
    (used as a replacement for the old Gemini grounding step in Path D).
    Returns (url, title) or (None, None).
    """
    loc = location or DEFAULT_LOCATION
    domain = RETAILER_MATCH.get(retailer, {}).get("domain")
    if not domain:
        return None, None

    query = f'"{part_number}" {brand_name}'.strip()
    data = serpapi_request({
        "engine": "google",
        "q": f"{query} site:{domain}",
        "location": loc,
    }, api_key=api_key)
    res = _scan_organic(data, retailer, part_number, allow_url_only=True)
    if res and res.get("source_url"):
        return res["source_url"], res.get("product_title")
    return None, None
