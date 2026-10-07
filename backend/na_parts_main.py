# --- na_parts_main.py ---
# FastAPI backend for North America multi-retailer price intelligence.
# Runs independently on port 8001.
#
# Start with:  python na_parts_main.py
# Or:          uvicorn na_parts_main:app --host 127.0.0.1 --port 8001

import io
import json
import os
import time
import asyncio
import logging
import re
import urllib.request
import urllib.error
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import pandas as pd
import requests as http_requests
from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load backend/.env EARLY — before importing local modules (gcs_storage, app_config,
# serpapi_search) that read env vars at import time — so SERPAPI_API_KEY and
# SERP_DATA_GCS_ENABLED take effect. load_dotenv does NOT override vars already set
# in the shell environment.
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from na_retailers_scraper import RETAILER_CONFIG
from lkq_search import lkq_search_part
from serpapi_search import (
    extract_retailer_results,
    serpapi_search_google_product,
)
import gcs_storage

# ---------------------------------------------------------------------------
# Bulk pipeline config (env-driven; safe defaults for local dev)
# ---------------------------------------------------------------------------
GCP_PROJECT          = os.environ.get("GCP_PROJECT",           "ford-5ae865a4a2f14ba62fcc8c2d")
BQ_DATASET           = os.environ.get("BQ_DATASET",            "part_pricing_landing")
BQ_TABLE             = os.environ.get("BQ_TABLE",              "na_retailers_brand_parts")
COMPOSER_API_URL     = os.environ.get("COMPOSER_API_URL",      "")   # Cloud Composer webserver root URL
COMPOSER_IAP_CLIENT  = os.environ.get("COMPOSER_IAP_CLIENT_ID", "")  # IAP OAuth2 Client ID

DAG_ID = "dag_trigger_parts_pricing_cldrun_na"

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("NA-Parts-API")

# Live-search JSON is persisted locally; batch result JSON is uploaded to GCS
# when enabled. See gcs_storage.py for all configuration.

app = FastAPI(
    title="NA Parts Price Intelligence API",
    description="Scrapes AutoZone, O'Reilly, Advance Auto, Amazon, NAPA, Summit Racing, Walmart for competitor pricing.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class LiveScrapeRequest(BaseModel):
    part_number: str
    brand_name:  str
    retailers:   List[str]   # e.g. ["AutoZone", "O'Reilly"]


class LiveScrapeResult(BaseModel):
    results: list
    elapsed: float
    scraped_at: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

BATCH_OUTPUT_COLUMNS = ["retailer_name", "brand_part_number", "brand_name", "mli", "part_price"]
BATCH_RESULT_COLUMN_MAP = (
    ("MSRP", "rrp_message"),
    ("Sale/Regular", "sale_and_regular_price"),
    ("Rating", "rating"),
    ("Pack Size", "pack_size"),
    ("Delivery", "delivery_message"),
    ("Store Pickup", "in_store_pickup"),
    ("Availability Msg", "availability_message"),
)
MAX_BATCH_ROWS = 200
BATCH_STREAM_HEARTBEAT_SECONDS = max(
    1.0,
    float(os.environ.get("BATCH_STREAM_HEARTBEAT_SECONDS", "15") or "15"),
)
COLLISION_BATCH_OUTPUT_COLUMNS = [
    "retailer_name",
    "brand_part_number",
    "part_price",
    "product_title",
    "condition",
    "availability",
    "part_link",
]

_BATCH_RETAILER_ALIASES = {
    "AUTOZONE": "AutoZone",
    "OREILLY": "O'Reilly",
    "OREILLYAUTO": "O'Reilly",
    "ADVANCEAUTO": "Advance Auto",
    "ADVANCEAUTOPARTS": "Advance Auto",
    "AMAZON": "Amazon",
    "NAPA": "NAPA",
    "SUMMITRACING": "Summit Racing",
    "WALMART": "Walmart",
    "CARPARTS": "CarParts",
    "CARPARTSCOM": "CarParts",
}


def _get_identity_token(audience: str) -> str:
    """
    Fetches a short-lived OIDC token from the GCE metadata server.
    Only available when running on GCP (Cloud Run, GCE, GKE …).
    Used to authenticate against IAP-protected Cloud Composer.
    """
    url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        f"service-accounts/default/identity?audience={audience}"
    )
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def _column_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _retailer_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _normalize_batch_retailer_name(raw_name: str) -> Optional[str]:
    return _BATCH_RETAILER_ALIASES.get(_retailer_key(raw_name))


def _normalize_sheet_price(raw_price) -> str:
    if raw_price is None:
        return ""

    if isinstance(raw_price, (int, float)):
        numeric = float(raw_price)
        if numeric <= 0:
            return ""
        if numeric.is_integer():
            return str(int(numeric))
        return f"{numeric:.2f}".rstrip("0").rstrip(".")

    text = str(raw_price).strip()
    if not text:
        return ""
    if text.lower() in {"n/a", "na", "none", "null", "price not available", "part not found", "error"}:
        return ""

    match = re.search(r"[\d,]+(?:\.\d+)?", text.replace(" ", ""))
    if not match:
        return ""

    try:
        numeric = float(match.group(0).replace(",", ""))
    except ValueError:
        return ""
    if numeric <= 0:
        return ""
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.2f}".rstrip("0").rstrip(".")


def _batch_result_fields(result: Dict[str, Any]) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for _, result_key in BATCH_RESULT_COLUMN_MAP:
        value = result.get(result_key)
        text = str(value).strip() if value is not None else ""
        fields[result_key] = text or "N/A"
    return fields


def _sanitize_filename_token(value: str, fallback: str = "na") -> str:
    token = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip())
    token = token.strip("_")
    return token or fallback


def _normalize_serpapi_attempts(
    payload: Dict[str, Any],
    serpapi_attempts: Union[List[Dict[str, Any]], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if isinstance(serpapi_attempts, list):
        attempts_input = serpapi_attempts
    elif isinstance(serpapi_attempts, dict):
        attempts_input = [
            {
                "attempt": 1,
                "query": "",
                "engine": "google",
                "missing_retailers": [],
                "serpapi_response": serpapi_attempts,
            }
        ]
    else:
        attempts_input = []

    normalized_attempts: List[Dict[str, Any]] = []
    for idx, attempt in enumerate(attempts_input, start=1):
        if not isinstance(attempt, dict):
            continue

        raw_response = attempt.get("serpapi_response")
        response_dict = raw_response if isinstance(raw_response, dict) else {}
        search_parameters = response_dict.get("search_parameters", {}) if isinstance(response_dict, dict) else {}
        attempt_entry: Dict[str, Any] = {
            "attempt": int(attempt.get("attempt") or idx),
            "query": str(
                attempt.get("query")
                or search_parameters.get("q")
                or f"{payload.get('part_number', '')} {payload.get('brand_name', '')}"
            ).strip(),
            "engine": str(attempt.get("engine") or search_parameters.get("engine") or "google").strip() or "google",
            "missing_retailers": list(attempt.get("missing_retailers") or []),
            "serpapi_response": raw_response,
        }
        if attempt.get("error"):
            attempt_entry["error"] = str(attempt.get("error"))
        normalized_attempts.append(attempt_entry)

    return normalized_attempts


def _build_live_search_payload(
    part_number: str,
    brand_name: str,
    retailers: List[str],
    results: List[Dict[str, Any]],
    elapsed: float,
    scraped_at: str,
) -> Dict[str, Any]:
    results_by_retailer: Dict[str, Dict[str, Any]] = {}
    trailing_results: List[Dict[str, Any]] = []
    for result in results:
        retailer_name = str(result.get("retailer", "")).strip()
        if retailer_name and retailer_name not in results_by_retailer:
            results_by_retailer[retailer_name] = result
        else:
            trailing_results.append(result)

    normalized_results: List[Dict[str, Any]] = []
    for retailer_name in retailers:
        if retailer_name in results_by_retailer:
            normalized_results.append(results_by_retailer[retailer_name])
            continue
        normalized_results.append(
            {
                "retailer": retailer_name,
                "retailer_code": RETAILER_CONFIG.get(retailer_name, {}).get("code", "??"),
                "currency": RETAILER_CONFIG.get(retailer_name, {}).get("currency", "USD"),
                "search_url": "",
                "screenshot_url": "",
                "status": "error",
                "product_title": "N/A",
                "price": "Error",
                "rrp_message": "N/A",
                "sale_and_regular_price": "N/A",
                "rating": "N/A",
                "availability": "N/A",
                "availability_message": "N/A",
                "delivery_message": "N/A",
                "in_store_pickup": "N/A",
                "pack_size": "N/A",
                "confidence": 0.0,
                "found": False,
            }
        )

    normalized_results.extend(trailing_results)
    total = len(normalized_results)
    success = sum(1 for result in normalized_results if str(result.get("status", "")).lower() == "success")
    error = max(0, total - success)
    return {
        "part_number": part_number,
        "brand_name": brand_name,
        "retailers": retailers,
        "results": normalized_results,
        "elapsed": elapsed,
        "scraped_at": scraped_at,
        "summary": {
            "total": total,
            "success": success,
            "error": error,
        },
    }


def _build_collision_search_payload(
    part_number: str,
    results: List[Dict[str, Any]],
    count: int,
    elapsed: float,
    scraped_at: str,
    latitude: float,
    longitude: float,
) -> Dict[str, Any]:
    total = len(results)
    success = sum(1 for result in results if str(result.get("status", "")).lower() == "success")
    error = max(0, total - success)
    return {
        "part_number": part_number,
        "retailer": "LKQ",
        "results": results,
        "count": int(count),
        "fetched": total,
        "elapsed": elapsed,
        "scraped_at": scraped_at,
        "latitude": latitude,
        "longitude": longitude,
        "summary": {
            "total": total,
            "success": success,
            "error": error,
        },
    }


def _persist_live_search(
    payload: Dict[str, Any],
    serpapi_attempts: Union[List[Dict[str, Any]], Dict[str, Any]],
) -> Optional[str]:
    """
    Persist a live-search JSON locally and optionally to GCS. Returns the GCS
    URI when uploaded, otherwise the local path.
    """
    part_token = _sanitize_filename_token(str(payload.get("part_number", "")), "part")
    brand_token = _sanitize_filename_token(str(payload.get("brand_name", "")), "brand")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"live_search_{timestamp}_{part_token}_{brand_token}.json"

    payload_to_save = dict(payload)
    payload_to_save["serpapi_attempts"] = _normalize_serpapi_attempts(payload, serpapi_attempts)

    local_path = gcs_storage.save_json_local(gcs_storage.LIVE_SUBDIR, filename, payload_to_save)
    gcs_uri = None
    if gcs_storage.is_live_gcs_enabled():
        gcs_uri = gcs_storage.upload_json(gcs_storage.LIVE_SUBDIR, filename, payload_to_save)
    return gcs_uri or local_path


def _persist_batch_part_to_gcs(
    date_folder: str,
    part_number: str,
    payload: Dict[str, Any],
    serpapi_attempts: Union[List[Dict[str, Any]], Dict[str, Any]],
) -> Optional[str]:
    """Upload one batch result to the configured GCS date/part-number path."""
    if not gcs_storage.is_batch_gcs_enabled():
        return None

    part_token = _sanitize_filename_token(str(part_number), "part")
    object_filename = f"{date_folder}/{part_token}.json"
    payload_to_save = dict(payload)
    payload_to_save["serpapi_attempts"] = _normalize_serpapi_attempts(
        payload,
        serpapi_attempts,
    )
    return gcs_storage.upload_json(
        gcs_storage.BATCH_SUBDIR,
        object_filename,
        payload_to_save,
    )


def _read_batch_input_dataframe(upload_file: UploadFile, raw_bytes: bytes) -> pd.DataFrame:
    filename = str(upload_file.filename or "").lower()
    buf = io.BytesIO(raw_bytes)

    if filename.endswith((".xlsx", ".xlsm", ".xltx", ".xltm", ".xls")):
        def _read_excel(engine: Optional[Literal["openpyxl"]] = None) -> pd.DataFrame:
            buf.seek(0)
            preview = pd.read_excel(
                buf,
                dtype=str,
                header=None,
                nrows=20,
                engine=engine,
            )
            retailer_headers = {"retailer_name", "retailer"}
            part_headers = {
                "brand_part_number",
                "part_number",
                "collision_part_number",
                "part_name",
                "part_no",
            }
            header_row = 0
            for row_number, row in enumerate(preview.itertuples(index=False, name=None)):
                row_keys = {
                    _column_key(value)
                    for value in row
                    if pd.notna(value) and str(value).strip()
                }
                if row_keys & retailer_headers and row_keys & part_headers:
                    header_row = row_number
                    break

            buf.seek(0)
            return pd.read_excel(buf, dtype=str, header=header_row, engine=engine)

        try:
            return _read_excel()
        except Exception:
            return _read_excel(engine="openpyxl")

    return pd.read_csv(buf, dtype=str)


def _trim_batch_value(value: Any) -> str:
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _trim_batch_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    trimmed = df.copy()
    trimmed.columns = [_trim_batch_value(column) for column in trimmed.columns]
    return trimmed.apply(lambda column: column.map(_trim_batch_value))


def _build_single_retailer_batch_df(df: pd.DataFrame) -> pd.DataFrame:
    df = _trim_batch_dataframe(df)
    key_to_column = {_column_key(col): col for col in df.columns}

    def _resolve_column(*candidates: str) -> Optional[str]:
        for candidate in candidates:
            if candidate in key_to_column:
                return key_to_column[candidate]
        return None

    # Accept the current header (brand_part_number) plus older/looser part-column
    # names (part_name, part_number, …) so legacy batch files keep working.
    retailer_col = _resolve_column("retailer_name", "retailer")
    part_col = _resolve_column("brand_part_number", "part_number", "part_name", "part_no")

    missing = []
    if retailer_col is None:
        missing.append("retailer_name")
    if part_col is None:
        missing.append("brand_part_number")
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # brand_name and mli are optional: they sharpen the search query when present
    # (e.g. "BX35 GATES BATTERIES"). Missing columns fall back to blank.
    brand_col = _resolve_column("brand_name", "brand")
    mli_col = _resolve_column("mli")
    price_col = _resolve_column("part_price", "price")

    out = pd.DataFrame(
        {
            "retailer_name": df[retailer_col],
            "brand_part_number": df[part_col],
            "brand_name": df[brand_col] if brand_col else "",
            "mli": df[mli_col] if mli_col else "",
        }
    )
    out["retailer_name"] = out["retailer_name"].fillna("").astype(str).str.strip()
    out["brand_part_number"] = out["brand_part_number"].fillna("").astype(str).str.strip()
    out["brand_name"] = out["brand_name"].fillna("").astype(str).str.strip()
    out.loc[out["brand_name"].str.lower() == "nan", "brand_name"] = ""
    out["mli"] = out["mli"].fillna("").astype(str).str.strip()
    out.loc[out["mli"].str.lower() == "nan", "mli"] = ""

    # Some retailer exports intermittently reverse these two cells, producing
    # rows such as part_number="Bosch ICON", brand_name="24A". Repair only the
    # unambiguous shape before any SerpApi request is made.
    reversed_fields = (
        ~out["brand_part_number"].str.contains(r"\d", regex=True, na=False)
        & out["brand_name"].str.contains(r"\d", regex=True, na=False)
    )
    reversed_count = int(reversed_fields.sum())
    if reversed_count:
        original_parts = out.loc[reversed_fields, "brand_part_number"].copy()
        out.loc[reversed_fields, "brand_part_number"] = out.loc[reversed_fields, "brand_name"]
        out.loc[reversed_fields, "brand_name"] = original_parts
        logger.warning(
            "Corrected reversed brand_name/brand_part_number values in %s batch row(s).",
            reversed_count,
        )

    # Preserve any extra/unknown columns verbatim so they can be echoed back in the
    # results download. These are NOT used to build the search query.
    consumed = {retailer_col, part_col, brand_col, mli_col, price_col}
    for col in df.columns:
        if col in consumed or col in ("retailer_name", "brand_part_number", "brand_name", "mli"):
            continue
        extra_series = df[col].fillna("").astype(str).str.strip()
        out[col] = extra_series.where(extra_series.str.lower() != "nan", "")

    out = out[
        (out["retailer_name"] != "")
        & (out["brand_part_number"] != "")
        & (out["retailer_name"].str.lower() != "nan")
        & (out["brand_part_number"].str.lower() != "nan")
    ]
    return out.reset_index(drop=True)


def _build_collision_batch_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a collision batch sheet while preserving user-defined columns."""
    df = _trim_batch_dataframe(df)
    key_to_column = {_column_key(col): col for col in df.columns}

    def _resolve_column(*candidates: str) -> Optional[str]:
        for candidate in candidates:
            if candidate in key_to_column:
                return key_to_column[candidate]
        return None

    part_col = _resolve_column(
        "brand_part_number",
        "part_number",
        "collision_part_number",
        "part_name",
        "part_no",
    )
    if part_col is None:
        raise ValueError("Missing required column: brand_part_number")

    retailer_col = _resolve_column("retailer_name", "retailer")
    out = pd.DataFrame(
        {
            "retailer_name": df[retailer_col] if retailer_col else "LKQ",
            "brand_part_number": df[part_col],
        }
    )
    out["retailer_name"] = out["retailer_name"].fillna("").astype(str).str.strip()
    out.loc[out["retailer_name"].str.lower().isin(["", "nan"]), "retailer_name"] = "LKQ"
    out["brand_part_number"] = out["brand_part_number"].fillna("").astype(str).str.strip()

    consumed = {retailer_col, part_col}
    generated_cols = {
        "part_price",
        "product_title",
        "condition",
        "availability",
        "part_link",
        "found",
        "error",
        "cached",
    }
    for col in df.columns:
        if col in consumed or _column_key(col) in generated_cols:
            continue
        extra_series = df[col].fillna("").astype(str).str.strip()
        out[col] = extra_series.where(extra_series.str.lower() != "nan", "")

    out = out[
        (out["brand_part_number"] != "")
        & (out["brand_part_number"].str.lower() != "nan")
    ]
    return out.reset_index(drop=True)


def _scrape_single_retailer_price(part_number: str, retailer_name: str, brand_name: str = "", mli: str = "") -> Tuple[str, bool, str, Dict[str, Any]]:
    part_number = str(part_number or "").strip()
    brand_name = str(brand_name or "").strip()
    mli = str(mli or "").strip()
    if not part_number or retailer_name not in RETAILER_CONFIG:
        return "", False, "Invalid part number or retailer", {
            "result": _error_live_result(retailer_name),
            "serpapi_attempts": [],
        }

    # Batch query combines part number + brand + retailer + mli (whichever are
    # present), e.g. "BX35 ACDELCO NAPA BATTERIES". Exactly ONE SerpApi call per row.
    search_terms = " ".join(t for t in (brand_name, retailer_name, mli) if t)

    attempts = (
        (
            "combined-query",
            lambda: serpapi_search_google_product(part_number, search_terms),
        ),
    )
    had_request_error = False
    last_error = ""
    attempts_payload: List[Dict[str, Any]] = []
    query_label = f"{part_number} {search_terms}".strip()

    for attempt_idx, (attempt_name, loader) in enumerate(attempts, start=1):
        attempt_entry: Dict[str, Any] = {
            "attempt": attempt_idx,
            "query": query_label,
            "engine": "google",
            "missing_retailers": [],
        }
        try:
            raw_serp = loader()
            parsed = extract_retailer_results(raw_serp, part_number, search_terms, [retailer_name])
            attempt_entry["serpapi_response"] = raw_serp
        except Exception as e:
            had_request_error = True
            last_error = str(e)
            attempt_entry["serpapi_response"] = None
            attempt_entry["error"] = str(e)
            attempts_payload.append(attempt_entry)
            logger.warning(
                "Single-retailer lookup failed (%s) for '%s' at '%s': %s",
                attempt_name,
                part_number,
                retailer_name,
                e,
            )
            continue

        attempts_payload.append(attempt_entry)
        if not parsed:
            continue
        best_result = parsed[0] if isinstance(parsed[0], dict) else {}
        price = _normalize_sheet_price(best_result.get("price"))
        if price:
            return price, True, "", {
                "result": best_result,
                "serpapi_attempts": attempts_payload,
            }

    if had_request_error:
        if "proxy" in last_error.lower() or "tunnel connection failed" in last_error.lower():
            error_msg = "Search blocked by proxy/network"
        else:
            error_msg = "Search service/network error"
        return "", False, error_msg, {
            "result": _error_live_result(retailer_name),
            "serpapi_attempts": attempts_payload,
        }
    return "", False, "Price not found", {
        "result": _error_live_result(retailer_name, price_label="Part not found"),
        "serpapi_attempts": attempts_payload,
    }


def _format_result_row(res: dict, ford_pn: str, brand: str, part_no: str) -> dict:
    """Flattens a scraper result into a CSV-friendly dict."""
    return {
        "Ford Part Number": ford_pn,
        "Brand Name":       brand,
        "Part Number":      part_no,
        "Retailer":         res.get("retailer",       "N/A"),
        "Retailer Code":    res.get("retailer_code",  "N/A"),
        "Product Title":    res.get("product_title", "N/A"),
        "Price":            res.get("price",         "N/A"),
        "Currency":         res.get("currency",      "USD"),
        "RRP Message":      res.get("rrp_message",   "N/A"),
        "Rating":           res.get("rating",        "N/A"),
        "Availability":     res.get("availability",  "N/A"),
        "Pack Size":        res.get("pack_size",     "N/A"),
        "Status":           res.get("status",        "error"),
        "Search URL":       res.get("search_url",    ""),
        "Scraped At":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _error_live_result(retailer_name: str, price_label: str = "Error") -> dict:
    return {
        "retailer": retailer_name,
        "retailer_code": RETAILER_CONFIG.get(retailer_name, {}).get("code", "??"),
        "currency": RETAILER_CONFIG.get(retailer_name, {}).get("currency", "USD"),
        "search_url": "",
        "screenshot_url": "",
        "status": "error",
        "product_title": "N/A",
        "price": price_label,
        "rrp_message": "N/A",
        "sale_and_regular_price": "N/A",
        "rating": "N/A",
        "availability": "N/A",
        "availability_message": "N/A",
        "delivery_message": "N/A",
        "in_store_pickup": "N/A",
        "pack_size": "N/A",
        "confidence": 0.0,
        "found": False,
    }


def _run_live_search_with_retry_detailed(
    part_number: str,
    brand_name: str,
    retailers: list[str],
) -> Tuple[list[dict], List[Dict[str, Any]]]:
    """Run the live single-call flow, returning (results, serpapi_attempts).

    Note: the attempt-2 (missing-retailers) retry was removed from live search.
    The extracted code is preserved in ATTEMPT2_LIVE_RETRY.md at the project root.
    """
    attempts_payload: List[Dict[str, Any]] = []

    raw_one = serpapi_search_google_product(part_number, brand_name)
    attempt_one_results = extract_retailer_results(raw_one, part_number, brand_name, retailers)
    attempts_payload.append({
        "attempt": 1,
        "query": f"{part_number} {brand_name}".strip(),
        "engine": "google",
        "missing_retailers": [],
        "serpapi_response": raw_one,
    })
    return attempt_one_results, attempts_payload


def _run_live_search_with_retry(
    part_number: str,
    brand_name: str,
    retailers: list[str],
) -> list[dict]:
    results, _ = _run_live_search_with_retry_detailed(part_number, brand_name, retailers)
    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/me")
async def me(request: Request):
    """
    Returns the authenticated user's email from the IAP-injected header.
    IAP sets: X-Goog-Authenticated-User-Email: accounts.google.com:user@ford.com
    (Mirrors the EU app's /api/me so the frontend Header can show the signed-in user.)
    """
    iap_email = request.headers.get("X-Goog-Authenticated-User-Email", "")
    # Strip the "accounts.google.com:" prefix if present
    email = iap_email.split(":")[-1] if ":" in iap_email else iap_email
    return {"email": email or None}


@app.get("/na-parts/health")
async def health():
    """Quick health-check endpoint (includes GCS retry-buffer backlog)."""
    return {
        "status": "ok",
        "service": "NA Parts Price Intelligence",
        "gcs": gcs_storage.pending_status(),
    }


@app.post("/na-parts/flush-pending")
async def flush_pending():
    """Force-retry any SERP JSON uploads buffered while auth was unavailable."""
    flushed, remaining = gcs_storage._flush_pending()
    return {"flushed": flushed, "remaining": remaining, "gcs": gcs_storage.pending_status()}


@app.get("/na-parts/retailers")
async def get_retailers():
    """Returns the list of supported retailers with their metadata."""
    return {
        name: {
            "code":     cfg["code"],
            "language": cfg["language"],
            "currency": cfg["currency"],
            "base_url": cfg["base_url"],
        }
        for name, cfg in RETAILER_CONFIG.items()
    }


@app.post("/collision-parts/scrape-live")
async def scrape_collision_live(payload: Dict[str, Any] = Body(...)):
    """
    Searches LKQ open catalog for a collision part number.
    This flow is LKQ-specific and does not use SerpApi.
    """
    data = payload or {}
    part_number = str(data.get("part_number", "")).strip()
    if not part_number:
        return JSONResponse({"error": "part_number is required."}, status_code=400)

    latitude_raw = data.get("latitude")
    longitude_raw = data.get("longitude")
    try:
        latitude = float(latitude_raw) if latitude_raw not in (None, "") else float(os.environ.get("LKQ_LATITUDE", "38.742271"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "latitude must be a number."}, status_code=400)
    try:
        longitude = float(longitude_raw) if longitude_raw not in (None, "") else float(os.environ.get("LKQ_LONGITUDE", "-97.262715"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "longitude must be a number."}, status_code=400)

    start_time = time.time()
    logger.info("Collision scrape [LKQ]: '%s' at (%s, %s)", part_number, latitude, longitude)
    try:
        results, count = lkq_search_part(
            part_number,
            latitude=latitude,
            longitude=longitude,
        )
    except Exception as e:
        logger.error("Collision scrape [LKQ] failed for '%s': %s", part_number, e)
        results, count = [], 0

    elapsed = round(time.time() - start_time, 2)
    scraped_at = datetime.now().isoformat()
    response_payload = _build_collision_search_payload(
        part_number=part_number,
        results=results,
        count=count,
        elapsed=elapsed,
        scraped_at=scraped_at,
        latitude=latitude,
        longitude=longitude,
    )
    logger.info(
        "Collision scrape [LKQ] complete: fetched=%s count=%s elapsed=%ss",
        response_payload.get("fetched", 0),
        count,
        elapsed,
    )
    return response_payload


@app.post("/collision-parts/scrape-batch-stream")
async def scrape_collision_batch_stream(file: UploadFile = File(...)):
    """Stream one LKQ live-catalog lookup for each valid collision batch row."""
    raw_bytes = await file.read()
    try:
        df = _read_batch_input_dataframe(file, raw_bytes)
        batch_df = _build_collision_batch_df(df)
    except Exception as e:
        return JSONResponse({"error": f"Could not parse collision batch file: {e}"}, status_code=400)

    if batch_df.empty:
        return JSONResponse(
            {"error": "File contains no valid rows for brand_part_number."},
            status_code=400,
        )

    invalid_retailers = sorted({
        str(value).strip()
        for value in batch_df["retailer_name"]
        if _retailer_key(value) != "LKQ"
    })
    if invalid_retailers:
        return JSONResponse(
            {"error": f"Collision batch supports only LKQ. Invalid retailers: {invalid_retailers}"},
            status_code=400,
        )

    try:
        latitude = float(os.environ.get("LKQ_LATITUDE", "38.742271"))
        longitude = float(os.environ.get("LKQ_LONGITUDE", "-97.262715"))
    except ValueError:
        return JSONResponse({"error": "LKQ latitude/longitude configuration is invalid."}, status_code=500)

    uploaded_rows = len(batch_df)
    ignored_rows = max(0, uploaded_rows - MAX_BATCH_ROWS)
    if ignored_rows:
        batch_df = batch_df.head(MAX_BATCH_ROWS).reset_index(drop=True)
        logger.warning(
            "Collision batch [LKQ] received %s valid rows; processing the first %s and ignoring %s.",
            uploaded_rows,
            MAX_BATCH_ROWS,
            ignored_rows,
        )
    total_rows = len(batch_df)
    extra_cols = [
        col for col in batch_df.columns
        if col not in ("retailer_name", "brand_part_number")
    ]
    start_time = time.time()
    logger.info("Collision batch stream [LKQ] started with %s row(s).", total_rows)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'start', 'total': total_rows, 'uploaded_total': uploaded_rows, 'ignored_rows': ignored_rows, 'max_rows': MAX_BATCH_ROWS})}\n\n"
            done = 0
            found_count = 0
            dedup_cache: Dict[str, Dict[str, Any]] = {}

            for _, row in batch_df.iterrows():
                part_number = str(row["brand_part_number"]).strip()
                cache_key = part_number.upper()
                is_duplicate = cache_key in dedup_cache

                if is_duplicate:
                    cached = dedup_cache[cache_key]
                    results = cached["results"]
                    count = cached["count"]
                    error = cached["error"]
                else:
                    error = ""
                    try:
                        results, count = lkq_search_part(
                            part_number,
                            latitude=latitude,
                            longitude=longitude,
                        )
                    except Exception as e:
                        logger.error("Collision batch [LKQ] failed for '%s': %s", part_number, e)
                        results, count = [], 0
                        error = f"LKQ search failed: {str(e)[:160]}"
                    dedup_cache[cache_key] = {
                        "results": results,
                        "count": count,
                        "error": error,
                    }

                best = results[0] if results and isinstance(results[0], dict) else {}
                found = bool(best) and str(best.get("status", "")).lower() == "success"
                if found:
                    found_count += 1
                elif not error:
                    error = "Part not found"

                done += 1
                extras = {col: str(row.get(col, "")) for col in extra_cols}
                payload = {
                    **extras,
                    "retailer_name": "LKQ",
                    "brand_part_number": part_number,
                    "part_price": _normalize_sheet_price(best.get("price", "")),
                    "product_title": str(best.get("product_title", "")),
                    "condition": str(best.get("condition", "")),
                    "availability": str(best.get("availability_message") or best.get("availability") or ""),
                    "part_link": str(best.get("part_link") or best.get("source_url") or ""),
                    "found": found,
                    "cached": is_duplicate,
                }
                if error:
                    payload["error"] = error

                yield (
                    f"data: {json.dumps({'type': 'progress', 'done': done, 'total': total_rows, 'row': payload})}\n\n"
                )

            elapsed = round(time.time() - start_time, 2)
            logger.info(
                "Collision batch stream [LKQ] complete: %s/%s rows found in %ss.",
                found_count,
                total_rows,
                elapsed,
            )
            yield "data: " + json.dumps({
                "type": "done",
                "elapsed": elapsed,
                "results_count": total_rows,
                "found_count": found_count,
                "uploaded_total": uploaded_rows,
                "ignored_rows": ignored_rows,
                "max_rows": MAX_BATCH_ROWS,
            }) + "\n\n"
        except asyncio.CancelledError:
            logger.info("Collision batch stream cancelled (client disconnected).")
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/collision-parts/export-batch-xlsx")
async def export_collision_batch_xlsx(results: list = Body(...)):
    """Export collision batch rows and LKQ result details to XLSX."""
    if not isinstance(results, list) or not results:
        return JSONResponse({"error": "No collision batch rows to export."}, status_code=400)

    reserved_keys = {"found", "error", "cached"}
    standard_cols = list(COLLISION_BATCH_OUTPUT_COLUMNS)
    extra_order: List[str] = []
    normalized_rows = []
    for row in results[:MAX_BATCH_ROWS]:
        if not isinstance(row, dict):
            continue
        record = {
            "retailer_name": "LKQ",
            "brand_part_number": str(row.get("brand_part_number", "")).strip(),
            "part_price": _normalize_sheet_price(row.get("part_price", "")),
            "product_title": str(row.get("product_title", "")).strip(),
            "condition": str(row.get("condition", "")).strip(),
            "availability": str(row.get("availability", "")).strip(),
            "part_link": str(row.get("part_link", "")).strip(),
        }
        for key, value in row.items():
            if key in record or key in reserved_keys:
                continue
            record[key] = "" if value is None else str(value).strip()
            if key not in extra_order:
                extra_order.append(key)
        normalized_rows.append(record)

    if not normalized_rows:
        return JSONResponse({"error": "No valid collision batch rows to export."}, status_code=400)

    columns = standard_cols + extra_order
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        pd.DataFrame(normalized_rows).reindex(columns=columns).fillna("").to_excel(
            writer,
            index=False,
        )
    out.seek(0)
    filename = f"collision_parts_prices_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/collision-parts/batch-template")
async def collision_batch_template():
    """Return a ready-to-fill LKQ collision batch template."""
    rows = [
        {"retailer_name": "LKQ", "brand_part_number": "FO1000648"},
        {"retailer_name": "LKQ", "brand_part_number": "FO1095231PP"},
    ]
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        pd.DataFrame(rows, columns=["retailer_name", "brand_part_number"]).to_excel(
            writer,
            index=False,
        )
    out.seek(0)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=collision_parts_batch_template.xlsx"},
    )


@app.post("/na-parts/scrape-live")
async def scrape_live(request: LiveScrapeRequest):
    """
    Scrapes a single part across one or more retailers in real-time.
    """
    if not request.part_number.strip() or not request.brand_name.strip():
        return JSONResponse(
            {"error": "part_number and brand_name are required."},
            status_code=400,
        )

    unknown = [r for r in request.retailers if r not in RETAILER_CONFIG]
    if unknown:
        return JSONResponse(
            {"error": f"Unknown retailers: {unknown}. Valid: {list(RETAILER_CONFIG.keys())}"},
            status_code=400,
        )

    start_time = time.time()
    logger.info(
        f"Live scrape: '{request.brand_name} {request.part_number}' "
        f"for {request.retailers}"
    )
    serpapi_attempts: List[Dict[str, Any]] = []
    try:
        results, serpapi_attempts = _run_live_search_with_retry_detailed(
            request.part_number,
            request.brand_name,
            request.retailers,
        )
    except Exception as e:
        logger.error(f"Live max-two-call flow failed for '{request.brand_name} {request.part_number}': {e}")
        results = [_error_live_result(retailer_name) for retailer_name in request.retailers]

    elapsed = round(time.time() - start_time, 2)
    scraped_at = datetime.now().isoformat()
    logger.info(f"Live scrape completed in {elapsed}s. {len(results)} result(s) returned.")

    payload = _build_live_search_payload(
        part_number=request.part_number,
        brand_name=request.brand_name,
        retailers=request.retailers,
        results=results,
        elapsed=elapsed,
        scraped_at=scraped_at,
    )
    json_file = _persist_live_search(payload, serpapi_attempts)

    return {
        "results":    results,
        "elapsed":    elapsed,
        "scraped_at": scraped_at,
        "json_file":  json_file,
    }


@app.post("/na-parts/scrape-live-stream")
async def scrape_live_stream(request: LiveScrapeRequest):
    """
    Streams live scrape results as Server-Sent Events.
    """
    if not request.part_number.strip() or not request.brand_name.strip():
        return JSONResponse(
            {"error": "part_number and brand_name are required."},
            status_code=400,
        )

    unknown = [r for r in request.retailers if r not in RETAILER_CONFIG]
    if unknown:
        return JSONResponse(
            {"error": f"Unknown retailers: {unknown}. Valid: {list(RETAILER_CONFIG.keys())}"},
            status_code=400,
        )

    logger.info(
        "Live stream scrape: '%s %s' for %s",
        request.brand_name,
        request.part_number,
        request.retailers,
    )
    start_time = time.time()

    async def event_generator():
        try:
            total = len(request.retailers)
            yield f"data: {json.dumps({'type': 'start', 'total': total})}\n\n"

            serpapi_attempts: List[Dict[str, Any]] = []
            try:
                parsed_results, serpapi_attempts = _run_live_search_with_retry_detailed(
                    request.part_number,
                    request.brand_name,
                    request.retailers,
                )
            except Exception as e:
                logger.error(
                    "Live stream flow failed for '%s %s': %s",
                    request.brand_name,
                    request.part_number,
                    e,
                )
                parsed_results = [_error_live_result(retailer_name) for retailer_name in request.retailers]
                serpapi_attempts = [{
                    "attempt": 1,
                    "query": f"{request.part_number} {request.brand_name}".strip(),
                    "engine": "google",
                    "missing_retailers": list(request.retailers),
                    "serpapi_response": None,
                    "error": str(e),
                }]

            done = 0
            results: List[Dict[str, Any]] = []
            for result in parsed_results:
                done += 1
                results.append(result)
                yield (
                    f"data: {json.dumps({'type': 'progress', 'done': done, 'total': total, 'result': result})}\n\n"
                )

            elapsed = round(time.time() - start_time, 2)
            scraped_at = datetime.now().isoformat()
            payload = _build_live_search_payload(
                part_number=request.part_number,
                brand_name=request.brand_name,
                retailers=request.retailers,
                results=results,
                elapsed=elapsed,
                scraped_at=scraped_at,
            )
            json_file = _persist_live_search(payload, serpapi_attempts)
            done_event = {
                "type": "done",
                "elapsed": elapsed,
                "results_count": payload.get("summary", {}).get("total", done),
                "scraped_at": scraped_at,
                "summary": payload.get("summary", {}),
                "json_file": json_file,
            }
            logger.info("Live stream scrape complete: %s retailer(s) in %ss.", done, elapsed)
            yield f"data: {json.dumps(done_event)}\n\n"
        except asyncio.CancelledError:
            logger.info("Live stream scrape cancelled (client disconnected).")
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/na-parts/scrape-batch-stream")
async def scrape_batch_stream(request: Request, file: UploadFile = File(...)):
    """
    Single-retailer batch stream for sheets with:
      - retailer_name
      - brand_part_number
      - brand_name (optional; sharpens the search query, e.g. "BX35 GATES")
      - mli (optional; appended to the query when present, e.g. "BX35 GATES BATTERIES")
      - part_price (optional input, overwritten in output)
    """
    raw_bytes = await file.read()
    try:
        df = _read_batch_input_dataframe(file, raw_bytes)
        batch_df = _build_single_retailer_batch_df(df)
    except Exception as e:
        return JSONResponse({"error": f"Could not parse batch file: {e}"}, status_code=400)

    if batch_df.empty:
        return JSONResponse(
            {"error": "File contains no valid rows for retailer_name and brand_part_number."},
            status_code=400,
        )

    uploaded_rows = len(batch_df)
    ignored_rows = 0  # Mechanical batch has no per-batch row cap.
    total_rows = len(batch_df)
    # Columns beyond the recognized ones are pass-through extras: preserved in the
    # output/download but never used in the search query.
    _KNOWN_BATCH_COLS = ("retailer_name", "brand_part_number", "brand_name", "mli")
    extra_cols = [c for c in batch_df.columns if c not in _KNOWN_BATCH_COLS]
    logger.info("Single-retailer batch stream started with %s row(s).", total_rows)
    start_time = time.time()
    date_folder = datetime.now().strftime("%Y%m%d")

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'start', 'total': total_rows, 'uploaded_total': uploaded_rows, 'ignored_rows': ignored_rows, 'max_rows': None})}\n\n"

            done = 0
            found_count = 0
            # Cache of prior lookups so a repeated (part, brand, retailer) combo is
            # NOT searched again on SerpApi — the first result is reused.
            dedup_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            for _, row in batch_df.iterrows():
                # Stop immediately if the client has gone away (tab closed, navigated
                # away, or Stop pressed) so we never keep spending SerpApi credits on
                # a search nobody is receiving.
                if await request.is_disconnected():
                    logger.info(
                        "Batch stream: client disconnected after %s/%s rows — stopping (no more credits spent).",
                        done, total_rows,
                    )
                    return
                retailer_name = str(row["retailer_name"]).strip()
                part_number = str(row["brand_part_number"]).strip()
                brand_name = str(row.get("brand_name", "")).strip()
                mli = str(row.get("mli", "")).strip()
                normalized_retailer = _normalize_batch_retailer_name(retailer_name)

                price = ""
                found = False
                error = ""
                is_duplicate = False
                scrape_detail: Dict[str, Any] = {
                    "result": _error_live_result(retailer_name),
                    "serpapi_attempts": [],
                }
                dedup_key = (part_number.upper(), brand_name.upper(), retailer_name.upper())

                if normalized_retailer is None:
                    error = f"Unknown retailer '{retailer_name}'"
                    logger.warning(
                        "Skipping batch row with unknown retailer '%s' for part '%s'.",
                        retailer_name,
                        part_number,
                    )
                elif dedup_key in dedup_cache:
                    cached = dedup_cache[dedup_key]
                    price, found, error, scrape_detail = (
                        cached["price"],
                        cached["found"],
                        cached["error"],
                        cached["scrape_detail"],
                    )
                    is_duplicate = True
                    logger.info(
                        "Batch dedup: reusing result for '%s' / '%s' / '%s' (no new SerpApi call).",
                        part_number,
                        brand_name,
                        retailer_name,
                    )
                else:
                    # Run the (blocking) scrape off the event loop so the loop stays
                    # responsive. Heartbeats keep long-lived browser/proxy streams
                    # open while an individual SerpApi request is still in flight.
                    scrape_task = asyncio.create_task(
                        asyncio.to_thread(
                            _scrape_single_retailer_price,
                            part_number,
                            normalized_retailer,
                            brand_name,
                            mli,
                        )
                    )
                    while not scrape_task.done():
                        completed, _ = await asyncio.wait(
                            {scrape_task},
                            timeout=BATCH_STREAM_HEARTBEAT_SECONDS,
                        )
                        if scrape_task in completed:
                            break
                        if await request.is_disconnected():
                            scrape_task.cancel()
                            logger.info(
                                "Batch stream: client disconnected during row %s/%s — stopping.",
                                done + 1,
                                total_rows,
                            )
                            return
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "heartbeat",
                                    "done": done,
                                    "total": total_rows,
                                }
                            )
                            + "\n\n"
                        )

                    price, found, scrape_error, scrape_detail = await scrape_task
                    if not found:
                        error = scrape_error or "Price not found"
                    cached_scrape_detail = {
                        "result": scrape_detail.get("result"),
                        "serpapi_attempts": [],
                    }
                    dedup_cache[dedup_key] = {
                        "price": price,
                        "found": found,
                        "error": error,
                        "scrape_detail": cached_scrape_detail,
                    }

                if found:
                    found_count += 1

                scrape_result = scrape_detail.get("result")
                if not isinstance(scrape_result, dict):
                    scrape_result = _error_live_result(retailer_name)
                result_fields = _batch_result_fields(scrape_result)

                done += 1
                extras = {col: str(row.get(col, "")) for col in extra_cols}
                payload = {
                    **extras,
                    "retailer_name": retailer_name,
                    "brand_part_number": part_number,
                    "brand_name": brand_name,
                    "mli": mli,
                    "part_price": price,
                    **result_fields,
                    "found": found,
                    "cached": is_duplicate,
                }
                if error:
                    payload["error"] = error

                if not is_duplicate:
                    scrape_attempts = scrape_detail.get("serpapi_attempts")
                    if not isinstance(scrape_attempts, list):
                        scrape_attempts = []
                    part_payload = {
                        "brand_part_number": part_number,
                        "brand_name": brand_name,
                        "mli": mli,
                        "retailer_name": retailer_name,
                        "part_price": price,
                        "found": found,
                        "error": error,
                        "scraped_at": datetime.now().isoformat(),
                        "result": scrape_result,
                    }
                    await asyncio.to_thread(
                        _persist_batch_part_to_gcs,
                        date_folder,
                        part_number,
                        part_payload,
                        scrape_attempts,
                    )

                yield (
                    f"data: {json.dumps({'type': 'progress', 'done': done, 'total': total_rows, 'row': payload})}\n\n"
                )

            elapsed = round(time.time() - start_time, 2)
            logger.info(
                "Single-retailer batch stream complete: %s/%s rows with price in %ss.",
                found_count,
                total_rows,
                elapsed,
            )
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "done",
                        "elapsed": elapsed,
                        "results_count": total_rows,
                        "found_count": found_count,
                        "uploaded_total": uploaded_rows,
                        "ignored_rows": ignored_rows,
                        "max_rows": None,
                    }
                )
                + "\n\n"
            )

        except asyncio.CancelledError:
            logger.info("Single-retailer batch stream cancelled (client disconnected).")
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/na-parts/export-csv")
async def export_csv(results: list = Body(...)):
    """
    Converts a results list (from /scrape-live) to a
    downloadable CSV file in the browser.
    """
    if not results:
        return JSONResponse({"error": "No results to export."}, status_code=400)

    df = pd.DataFrame(results)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)

    filename = f"na_parts_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/na-parts/export-batch-xlsx")
async def export_batch_xlsx(results: list = Body(...)):
    """
        Exports single-retailer batch rows with pricing, rating, fulfillment,
        pack-size, and availability details.
    """
    if not isinstance(results, list) or not results:
        return JSONResponse({"error": "No batch rows to export."}, status_code=400)

    # Internal fields that should never become spreadsheet columns.
    reserved_keys = {"found", "error", "cached"}
    result_keys = {result_key for _, result_key in BATCH_RESULT_COLUMN_MAP}
    standard_cols = ["retailer_name", "brand_part_number", "brand_name", "mli"]
    extra_order: List[str] = []
    normalized_rows = []
    for row in results:
        if not isinstance(row, dict):
            continue
        record = {
            "retailer_name": str(row.get("retailer_name", "")).strip(),
            "brand_part_number": str(row.get("brand_part_number", "")).strip(),
            "brand_name": str(row.get("brand_name", "")).strip(),
            "mli": str(row.get("mli", "")).strip(),
            "part_price": _normalize_sheet_price(row.get("part_price", "")),
        }
        result_fields = _batch_result_fields(row)
        for column_name, result_key in BATCH_RESULT_COLUMN_MAP:
            record[column_name] = result_fields[result_key]

        # Echo back any extra columns the user included in their upload.
        for key, value in row.items():
            if key in record or key in reserved_keys or key in result_keys:
                continue
            record[key] = "" if value is None else str(value).strip()
            if key not in extra_order:
                extra_order.append(key)
        normalized_rows.append(record)

    if not normalized_rows:
        return JSONResponse({"error": "No valid batch rows to export."}, status_code=400)

    result_columns = [column_name for column_name, _ in BATCH_RESULT_COLUMN_MAP]
    columns = standard_cols + extra_order + ["part_price"] + result_columns
    df = pd.DataFrame(normalized_rows).reindex(columns=columns).fillna("")
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    out.seek(0)

    filename = f"na_parts_prices_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/na-parts/batch-template")
async def batch_template():
    """
    Returns a ready-to-fill .xlsx template for the single-retailer batch upload.
    Columns: retailer_name, brand_part_number, brand_name, mli, part_price (price left blank).
    """
    example_rows = [
        {"retailer_name": "AutoZone", "brand_part_number": "RS55047A", "brand_name": "Rancho", "mli": "SHOCKS", "part_price": ""},
        {"retailer_name": "O'Reilly", "brand_part_number": "SK3628", "brand_name": "Arnott", "mli": "SUSPENSION", "part_price": ""},
        {"retailer_name": "NAPA", "brand_part_number": "65ECO", "brand_name": "Optima", "mli": "BATTERIES", "part_price": ""},
    ]
    df = pd.DataFrame(example_rows, columns=BATCH_OUTPUT_COLUMNS)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    out.seek(0)

    filename = "na_parts_batch_template.xlsx"
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/na-parts/trigger-bulk")
async def trigger_bulk(file: UploadFile = File(...)):
    """
    Bulk pipeline trigger (3-step):
      1. Parse uploaded CSV (required: part_number, brand_name; optional: ford_part_number)
      2. Insert rows into BigQuery with retailer='na_retailers', price=NULL
      3. Trigger the Airflow DAG dag_trigger_parts_pricing_cldrun_na with run_number=1
    """
    from google.cloud import bigquery as bq_module  # lazy import

    # ── 1. Parse CSV ─────────────────────────────────────────────────────
    raw_bytes = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(raw_bytes), dtype=str)
    except Exception as e:
        return JSONResponse({"error": f"Could not parse CSV: {e}"}, status_code=400)

    df.columns = [c.strip().lower() for c in df.columns]

    missing_cols = {"part_number", "brand_name"} - set(df.columns)
    if missing_cols:
        return JSONResponse(
            {"error": f"Missing required CSV columns: {sorted(missing_cols)}"},
            status_code=400,
        )

    df = df.dropna(subset=["part_number", "brand_name"])
    df = df[
        (df["part_number"].str.strip() != "") &
        (df["brand_name"].str.strip()  != "") &
        (df["part_number"].str.lower() != "nan") &
        (df["brand_name"].str.lower()  != "nan")
    ]

    if df.empty:
        return JSONResponse({"error": "CSV has no valid rows."}, status_code=400)

    # ── 2. Build BQ row dicts ─────────────────────────────────────────────
    rows_to_insert = [
        {
            "part_number":      str(row["part_number"]).strip(),
            "brand_name":       str(row["brand_name"]).strip(),
            "ford_part_number": str(row.get("ford_part_number", "")).strip(),
            "retailer":         "na_retailers",
        }
        for _, row in df.iterrows()
    ]

    # ── 3. Insert into BigQuery ───────────────────────────────────────────
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
    try:
        bq_client = bq_module.Client(project=GCP_PROJECT)
        errors = bq_client.insert_rows_json(table_id, rows_to_insert)
        if errors:
            logger.error(f"BQ streaming insert errors: {errors}")
            return JSONResponse(
                {"error": f"BigQuery insertion failed: {errors[:3]}"},
                status_code=500,
            )
        logger.info(f"Inserted {len(rows_to_insert)} rows into {table_id}")
    except Exception as e:
        logger.error(f"BQ insert failed: {e}")
        return JSONResponse(
            {"error": f"BigQuery error: {str(e)[:300]}"},
            status_code=500,
        )

    # ── 4. Trigger Airflow DAG ────────────────────────────────────────────
    dag_run_id = f"ui_bulk_{datetime.now().strftime('%Y%m%dT%H%M%S')}"

    if not COMPOSER_API_URL:
        logger.warning("COMPOSER_API_URL not set — rows inserted but DAG not triggered.")
        return {
            "status":        "partial",
            "message":       (
                f"{len(rows_to_insert)} rows inserted into BigQuery. "
                "COMPOSER_API_URL is not configured — trigger the DAG manually."
            ),
            "rows_inserted": len(rows_to_insert),
            "dag_run_id":    None,
            "table":         table_id,
        }

    dag_endpoint = f"{COMPOSER_API_URL.rstrip('/')}/api/v1/dags/{DAG_ID}/dagRuns"
    headers = {"Content-Type": "application/json"}

    # Use OIDC token for IAP-protected Composer (only works on GCP)
    if COMPOSER_IAP_CLIENT:
        try:
            token = _get_identity_token(COMPOSER_IAP_CLIENT)
            headers["Authorization"] = f"Bearer {token}"
        except Exception as tok_err:
            logger.warning(f"IAP token fetch failed: {tok_err}. Trying without auth…")

    try:
        resp = http_requests.post(
            dag_endpoint,
            json={"dag_run_id": dag_run_id, "conf": {"run_number": 1}},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        dag_info = resp.json()
        logger.info(f"DAG triggered: dag_run_id={dag_run_id}  state={dag_info.get('state')}")
        return {
            "status":        "success",
            "message":       f"{len(rows_to_insert)} rows queued. Airflow DAG run started.",
            "rows_inserted": len(rows_to_insert),
            "dag_run_id":    dag_run_id,
            "dag_state":     dag_info.get("state", "queued"),
            "table":         table_id,
        }
    except Exception as dag_err:
        logger.error(f"DAG trigger failed: {dag_err}")
        return JSONResponse(
            {
                "status":        "partial",
                "message":       (
                    f"{len(rows_to_insert)} rows inserted into BigQuery, "
                    f"but DAG trigger failed: {str(dag_err)[:200]}"
                ),
                "rows_inserted": len(rows_to_insert),
                "dag_run_id":    None,
                "table":         table_id,
            },
            status_code=202,
        )


# ---------------------------------------------------------------------------
# Static file serving (production: FastAPI serves the Vite-built React app)
# The Dockerfile copies the Vite build/ output to ../frontend/dist.
# ---------------------------------------------------------------------------
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "dist")

if os.path.isdir(_STATIC_DIR):
    _assets_dir = os.path.join(_STATIC_DIR, "assets")
    if os.path.isdir(_assets_dir):
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="static-assets")

    _INDEX = os.path.join(_STATIC_DIR, "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(_full_path: str = ""):
        if os.path.exists(_INDEX):
            return FileResponse(_INDEX)
        return JSONResponse({"error": "Frontend not built"}, status_code=404)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="info")
