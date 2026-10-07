# --- app_config.py ---
# Loads backend/fixtures/config.yaml and resolves runtime configuration/secrets.
#
# Resolution order for the SerpApi key (GCP Secret Manager is primary):
#
#   1. Secret Manager via the native client (Application Default Credentials —
#      the Cloud Run service-account path; the default for GCP).
#   2. Secret Manager via the impersonated service account + restricted VIP
#      (LOCAL-dev fallback behind Ford's VPC-SC perimeter; `gcloud auth login`).
#   3. SERPAPI_API_KEY environment variable (optional manual fallback).
#
# The resolved key is cached in-process after the first successful fetch.

import base64
import json
import logging
import os
import shutil
import subprocess
import threading

from dotenv import load_dotenv

import gcs_storage

logger = logging.getLogger("NA-Parts-API.config")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load backend/.env so SERPAPI_API_KEY (and other vars) are available regardless
# of the process working directory. Does not override existing environment vars.
load_dotenv(os.path.join(_BASE_DIR, ".env"))

_CONFIG_PATH = os.path.join(_BASE_DIR, "fixtures", "config.yaml")
_SECRET_TIMEOUT = int(os.environ.get("SERPAPI_SECRET_TIMEOUT", "30") or "30")

_key_lock = threading.Lock()
_serpapi_key_cache = ""


def load_config() -> dict:
    """Load config.yaml; return {} on any failure."""
    try:
        import yaml

        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("config.yaml not found at %s", _CONFIG_PATH)
        return {}
    except Exception as e:
        logger.warning("Could not load config.yaml — %s", e)
        return {}


CONFIG = load_config()


def get_location() -> str:
    """Default SerpApi query location (env > config.yaml > built-in default)."""
    return (
        (os.environ.get("SERPAPI_LOCATION") or "").strip()
        or str(CONFIG.get("SERPAPI_LOCATION", "")).strip()
        or "Dearborn, Michigan, United States"
    )


def _normalise_secret_name(secret_name: str) -> str:
    if secret_name and "/versions/" not in secret_name:
        return f"{secret_name}/versions/latest"
    return secret_name


def _fetch_secret_via_vip(secret_name: str) -> str:
    """Fetch a Secret Manager secret via impersonated token + restricted VIP (curl)."""
    token = gcs_storage._get_impersonated_token()
    if not token:
        return ""

    curl = shutil.which("curl") or "/usr/bin/curl"
    url = f"https://secretmanager.googleapis.com/v1/{secret_name}:access"
    cmd = [
        curl, "-sS", "--noproxy", "*", "--max-time", str(_SECRET_TIMEOUT),
        "--resolve", f"secretmanager.googleapis.com:443:{gcs_storage.RESTRICTED_VIP}",
        "-H", f"Authorization: Bearer {token}",
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SECRET_TIMEOUT + 5,
            env=gcs_storage._clean_env(),
        )
    except Exception as e:
        logger.error("Secret fetch subprocess failed: %s", e)
        return ""

    if proc.returncode != 0:
        logger.warning(
            "Secret fetch via VIP failed (curl %s): %s",
            proc.returncode,
            (proc.stderr or "").strip()[:200],
        )
        return ""
    try:
        encoded = json.loads(proc.stdout)["payload"]["data"]
        return base64.b64decode(encoded).decode("utf-8").strip()
    except Exception as e:
        logger.error("Could not parse Secret Manager response: %s", e)
        return ""


def _fetch_secret_native(secret_name: str) -> str:
    """Fetch a Secret Manager secret via the native client (ADC / in-perimeter)."""
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=secret_name)
        return response.payload.data.decode("utf-8").strip()
    except Exception as e:
        logger.warning("Native Secret Manager fetch failed: %s", e)
        return ""


def get_serpapi_key() -> str:
    """Resolve (and cache) the SerpApi key.

    Resolution order:
      0. SERPAPI_API_KEY env var — an explicit local key takes priority so the
         app can run fully locally with NO GCP/Secret Manager calls.
      1. GCP Secret Manager (native ADC, then impersonated VIP).
    """
    global _serpapi_key_cache
    with _key_lock:
        if _serpapi_key_cache:
            return _serpapi_key_cache

        # 0. Local override: explicit env key wins — no GCP contact at all.
        env_key = (os.environ.get("SERPAPI_API_KEY") or "").strip()
        if env_key:
            logger.info("SerpApi key loaded from environment (local override).")
            _serpapi_key_cache = env_key
            return env_key

        # 1. GCP Secret Manager (primary source of truth when no local key).
        secret_name = _normalise_secret_name(str(CONFIG.get("SERPAPI_SECRET_NAME", "")).strip())
        if secret_name:
            key = _fetch_secret_native(secret_name) or _fetch_secret_via_vip(secret_name)
            if key:
                _serpapi_key_cache = key
                logger.info("SerpApi key loaded from Secret Manager.")
                return key

        return ""
