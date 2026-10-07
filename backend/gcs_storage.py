# --- gcs_storage.py ---
# Saves live-search JSON locally and uploads configured live/batch payloads to GCS.
#
# Two transport modes (SERP_DATA_GCS_MODE):
#   * "native" (default) — uses the google-cloud-storage client with Application
#     Default Credentials (ADC), i.e. the Cloud Run service account when
#     deployed. Also works locally once `gcloud auth application-default login`
#     is configured and the machine can reach GCS.
#   * "impersonate_vip" — LOCAL-dev fallback behind Ford's VPC Service Controls
#     perimeter. Mints a short-lived access token by impersonating a service
#     account via the local `gcloud` CLI, then uploads with `curl` routed
#     through Google's restricted VIP (199.36.153.4). Set
#     SERP_DATA_GCS_MODE=impersonate_vip from a dev machine that is not itself
#     inside the VPC-SC perimeter.
#
# Everything is env-configurable so the same code works locally and deployed.

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from typing import Optional, Tuple

logger = logging.getLogger("NA-Parts-API.gcs")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "")


# --- Configuration ---------------------------------------------------------
GCS_ENABLED       = _env_flag("SERP_DATA_GCS_ENABLED", "1")
BATCH_GCS_ENABLED = _env_flag("SERP_DATA_BATCH_GCS_ENABLED", "0")
GCS_MODE          = os.environ.get("SERP_DATA_GCS_MODE", "native").strip() or "native"
BUCKET            = os.environ.get("SERP_DATA_BUCKET", "m1-analytics-part-pricing-dev").strip()
PREFIX            = os.environ.get("SERP_DATA_PREFIX", "serp_data").strip().strip("/")
LIVE_SUBDIR       = os.environ.get("SERP_DATA_LIVE_SUBDIR", "live_search_data").strip().strip("/")
BATCH_SUBDIR      = os.environ.get("SERP_DATA_BATCH_SUBDIR", "batch_search_data").strip().strip("/")
LOCAL_ENABLED     = _env_flag("SERP_DATA_LOCAL_ENABLED", "1")
_local_root_config = os.environ.get("SERP_DATA_LOCAL_DIR", "").strip()
if _local_root_config:
    LOCAL_ROOT = (
        os.path.abspath(os.path.expanduser(_local_root_config))
        if os.path.isabs(os.path.expanduser(_local_root_config))
        else os.path.abspath(os.path.join(_BASE_DIR, os.path.expanduser(_local_root_config)))
    )
else:
    LOCAL_ROOT = os.path.join(_BASE_DIR, PREFIX or "serp_data")
IMPERSONATE_SA = os.environ.get(
    "SERP_DATA_IMPERSONATE_SA",
    "sa-developer@ford-5ae865a4a2f14ba62fcc8c2d.iam.gserviceaccount.com",
).strip()
RESTRICTED_VIP = os.environ.get("SERP_DATA_RESTRICTED_VIP", "199.36.153.4").strip()
UPLOAD_TIMEOUT = int(os.environ.get("SERP_DATA_UPLOAD_TIMEOUT", "30") or "30")

# Impersonated access tokens minted by gcloud are valid for at most ~1 hour
# (Google-enforced). The cache TTL therefore MUST stay below that ceiling — an
# "unlimited" TTL would keep reusing a token Google has already expired and every
# upload would 401. Resilience against auth *gaps* (e.g. gcloud needing re-auth)
# is provided by the disk-backed retry buffer below, NOT by a longer TTL.
_TOKEN_TTL_MAX = 3300  # 55 min, safely under the 60-min token lifetime
_token_ttl_cfg = int(os.environ.get("SERP_DATA_TOKEN_TTL_SEC", "3000") or "3000")
TOKEN_TTL_SEC  = min(_token_ttl_cfg, _TOKEN_TTL_MAX) if _token_ttl_cfg > 0 else _TOKEN_TTL_MAX
if _token_ttl_cfg > _TOKEN_TTL_MAX:
    logger.warning(
        "SERP_DATA_TOKEN_TTL_SEC=%s exceeds the safe max; capping at %ss "
        "(impersonated tokens expire after ~1h).",
        _token_ttl_cfg, _TOKEN_TTL_MAX,
    )

# --- Pending-upload retry buffer -------------------------------------------
# When an upload fails (typically: gcloud needs re-auth so no token can be
# minted, or the network/VPN is down), the payload is written to disk here and
# retried by a background flusher. This guarantees SERP JSON is never silently
# dropped and SerpApi credits are never spent for data that goes nowhere.
# Survives restarts.
PENDING_ENABLED    = _env_flag("SERP_DATA_PENDING_ENABLED", "1")
PENDING_DIR        = (os.environ.get("SERP_DATA_PENDING_DIR", "").strip()
                      or os.path.join(_BASE_DIR, ".pending_gcs"))
FLUSH_INTERVAL_SEC = int(os.environ.get("SERP_DATA_FLUSH_INTERVAL_SEC", "60") or "60")

# Candidate gcloud locations (env override first, then known local installs).
_GCLOUD_CANDIDATES = [
    os.environ.get("SERP_DATA_GCLOUD_BIN", "").strip(),
    "/Users/j/Downloads/google-cloud-sdk/bin/gcloud",
    "/Users/j/Desktop/NA SERP AI/gcloud auth application-default login/google-cloud-sdk/bin/gcloud",
]

_token_lock = threading.Lock()
_token_cache = {"value": None, "expires_at": 0.0}
_gcloud_bin_cache: Optional[str] = None

_flush_lock = threading.Lock()       # ensures only one flush runs at a time
_flusher_lock = threading.Lock()     # guards flusher-thread startup
_flusher_started = False


def is_enabled() -> bool:
    return (GCS_ENABLED or BATCH_GCS_ENABLED) and bool(BUCKET)


def is_live_gcs_enabled() -> bool:
    return GCS_ENABLED and bool(BUCKET)


def is_batch_gcs_enabled() -> bool:
    return BATCH_GCS_ENABLED and bool(BUCKET)


def is_local_enabled() -> bool:
    return LOCAL_ENABLED and bool(LOCAL_ROOT)


def local_directory(subdir: str, nested_path: str = "") -> str:
    parts = [LOCAL_ROOT, subdir]
    if nested_path:
        parts.append(nested_path)
    return os.path.abspath(os.path.join(*parts))


def save_json_local(subdir: str, filename: str, payload: dict) -> Optional[str]:
    """Atomically save a SERP payload below LOCAL_ROOT and return its path."""
    if not is_local_enabled():
        return None

    relative_path = os.path.normpath(os.path.join(subdir, filename))
    if os.path.isabs(relative_path) or relative_path == ".." or relative_path.startswith(f"..{os.sep}"):
        logger.error("Refusing unsafe local SERP JSON path: %s", relative_path)
        return None

    destination = os.path.abspath(os.path.join(LOCAL_ROOT, relative_path))
    temp_path = f"{destination}.{uuid.uuid4().hex}.tmp"
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as file_handle:
            json.dump(payload, file_handle, indent=2, ensure_ascii=False, default=str)
            file_handle.write("\n")
        os.replace(temp_path, destination)
        logger.info("Saved SERP JSON locally -> %s", destination)
        return destination
    except Exception as e:
        _safe_remove(temp_path)
        logger.error("Failed to save local SERP JSON %s: %s", destination, e)
        return None


def object_uri(object_name: str) -> str:
    return f"gs://{BUCKET}/{object_name}"


def _full_object_name(subdir: str, filename: str) -> str:
    parts = [p for p in (PREFIX, subdir, filename) if p]
    return "/".join(parts)


def _clean_env() -> dict:
    """Return an environment with proxy vars stripped (they break gcloud/curl here)."""
    env = dict(os.environ)
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        env.pop(key, None)
    return env


def _detect_gcloud_bin() -> Optional[str]:
    global _gcloud_bin_cache
    if _gcloud_bin_cache:
        return _gcloud_bin_cache
    for candidate in _GCLOUD_CANDIDATES:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _gcloud_bin_cache = candidate
            return candidate
    found = shutil.which("gcloud")
    if found:
        _gcloud_bin_cache = found
    return _gcloud_bin_cache


def _get_impersonated_token() -> Optional[str]:
    """Mint (and cache) a short-lived impersonated access token via gcloud."""
    now = time.time()
    with _token_lock:
        if _token_cache["value"] and now < _token_cache["expires_at"]:
            return _token_cache["value"]

        gcloud = _detect_gcloud_bin()
        if not gcloud:
            logger.error("gcloud CLI not found; cannot mint impersonated token.")
            return None

        cmd = [gcloud, "auth", "print-access-token"]
        if IMPERSONATE_SA:
            cmd.append(f"--impersonate-service-account={IMPERSONATE_SA}")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=UPLOAD_TIMEOUT,
                env=_clean_env(),
            )
        except Exception as e:
            logger.error("gcloud token subprocess failed: %s", e)
            return None

        if proc.returncode != 0:
            logger.error("gcloud token error: %s", (proc.stderr or "").strip()[:400])
            return None

        token = (proc.stdout or "").strip()
        if not token:
            logger.error("gcloud returned an empty access token.")
            return None

        _token_cache["value"] = token
        _token_cache["expires_at"] = now + TOKEN_TTL_SEC
        return token


def _invalidate_token() -> None:
    """Drop the cached impersonated token so the next call re-mints a fresh one.

    Called when an upload is rejected for an auth reason (expired/invalid token
    or gcloud re-auth needed) so we never keep hammering GCS with a dead token.
    """
    with _token_lock:
        _token_cache["value"] = None
        _token_cache["expires_at"] = 0.0


def _upload_via_vip(object_name: str, data: bytes) -> Tuple[bool, str]:
    token = _get_impersonated_token()
    if not token:
        return False, "no impersonated token"

    curl = shutil.which("curl") or "/usr/bin/curl"
    quoted = urllib.parse.quote(object_name, safe="")
    url = (
        f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o"
        f"?uploadType=media&name={quoted}"
    )
    cmd = [
        curl, "-sS", "--noproxy", "*", "--max-time", str(UPLOAD_TIMEOUT),
        "--resolve", f"storage.googleapis.com:443:{RESTRICTED_VIP}",
        "-X", "POST", url,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-w", "\n__HTTPCODE__%{http_code}",
        "--data-binary", "@-",
    ]
    try:
        proc = subprocess.run(
            cmd, input=data, capture_output=True, timeout=UPLOAD_TIMEOUT + 5, env=_clean_env()
        )
    except Exception as e:
        return False, f"curl failed: {e}"

    out = (proc.stdout or b"").decode("utf-8", "replace")
    code = ""
    if "__HTTPCODE__" in out:
        out, code = out.rsplit("__HTTPCODE__", 1)
        code = code.strip()
    if proc.returncode != 0:
        return False, f"curl exit {proc.returncode}: {(proc.stderr or b'').decode('utf-8','replace')[:300]}"
    if code == "200":
        return True, "ok"
    return False, f"HTTP {code}: {out.strip()[:300]}"


def _upload_via_native(object_name: str, data: bytes) -> Tuple[bool, str]:
    try:
        from google.cloud import storage  # lazy import; only needed in-perimeter
    except Exception as e:
        return False, f"google-cloud-storage unavailable: {e}"
    try:
        client = storage.Client()
        blob = client.bucket(BUCKET).blob(object_name)
        blob.upload_from_string(data, content_type="application/json")
        return True, "ok"
    except Exception as e:
        return False, f"native upload failed: {e}"


def _do_upload(object_name: str, data: bytes) -> Tuple[bool, str]:
    """Dispatch an upload to the configured transport."""
    if GCS_MODE == "native":
        return _upload_via_native(object_name, data)
    return _upload_via_vip(object_name, data)


_AUTH_FAILURE_MARKERS = (
    "no impersonated token", "http 401", "http 403",
    "reauth", "unauthor", "invalid_grant", "permission",
)


def _is_auth_failure(detail: str) -> bool:
    """Whether a failure detail indicates an auth/token problem (vs. transient)."""
    d = (detail or "").lower()
    return any(marker in d for marker in _AUTH_FAILURE_MARKERS)


# ---------------------------------------------------------------------------
# Pending-upload buffer: failed uploads are persisted to disk and retried by a
# background flusher so nothing is lost while gcloud auth / the network is down.
# ---------------------------------------------------------------------------

def _pending_count() -> int:
    try:
        return sum(1 for f in os.listdir(PENDING_DIR) if f.endswith(".json"))
    except FileNotFoundError:
        return 0
    except Exception:
        return 0


def _has_pending() -> bool:
    return _pending_count() > 0


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Could not remove pending file %s: %s", path, e)


def _enqueue_pending(object_name: str, data: bytes) -> bool:
    """Persist a failed upload to the on-disk retry queue. Returns True if buffered."""
    if not PENDING_ENABLED:
        return False
    try:
        os.makedirs(PENDING_DIR, exist_ok=True)
        envelope = {
            "object_name": object_name,
            "created_at": time.time(),
            "data_b64": base64.b64encode(data).decode("ascii"),
        }
        token = uuid.uuid4().hex
        tmp = os.path.join(PENDING_DIR, f".{token}.tmp")
        final = os.path.join(PENDING_DIR, f"{token}.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(envelope, f)
        os.replace(tmp, final)  # atomic
        return True
    except Exception as e:
        logger.error("Failed to buffer pending upload for %s: %s", object_name, e)
        return False


def _flush_pending() -> Tuple[int, int]:
    """Retry every buffered upload. Returns (flushed, remaining).

    Stops early on the first auth failure (re-auth needed) so we don't spin the
    gcloud subprocess once per queued item; the remainder stays buffered.
    """
    if not (PENDING_ENABLED and is_enabled()):
        return (0, _pending_count())
    if not _flush_lock.acquire(blocking=False):
        return (0, _pending_count())  # another flush is already running
    try:
        try:
            files = sorted(f for f in os.listdir(PENDING_DIR) if f.endswith(".json"))
        except FileNotFoundError:
            return (0, 0)

        flushed = 0
        for name in files:
            path = os.path.join(PENDING_DIR, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    env = json.load(f)
                object_name = env["object_name"]
                data = base64.b64decode(env["data_b64"])
            except Exception as e:
                logger.error("Corrupt pending file %s (%s) — removing.", name, e)
                _safe_remove(path)
                continue

            ok, detail = _do_upload(object_name, data)
            if ok:
                _safe_remove(path)
                flushed += 1
                logger.info("Flushed buffered SERP JSON -> %s", object_uri(object_name))
                continue

            if _is_auth_failure(detail):
                _invalidate_token()
                remaining = _pending_count()
                logger.warning(
                    "Pending flush paused — %s upload(s) still buffered "
                    "(gcloud re-auth required): %s", remaining, detail,
                )
                return (flushed, remaining)

            logger.warning("Retry failed for %s: %s (left buffered).", object_name, detail)

        remaining = _pending_count()
        if flushed:
            logger.info("Pending flush complete: %s uploaded, %s remaining.", flushed, remaining)
        return (flushed, remaining)
    finally:
        _flush_lock.release()


def _flusher_loop() -> None:
    while True:
        time.sleep(FLUSH_INTERVAL_SEC)
        try:
            if _has_pending():
                _flush_pending()
        except Exception as e:
            logger.error("Pending flusher error: %s", e)


def _ensure_flusher() -> None:
    """Start the background flusher thread once (idempotent)."""
    global _flusher_started
    if not PENDING_ENABLED:
        return
    with _flusher_lock:
        if _flusher_started:
            return
        threading.Thread(
            target=_flusher_loop, name="gcs-pending-flusher", daemon=True
        ).start()
        _flusher_started = True


def flush_pending_async() -> None:
    """Kick off a one-off non-blocking flush when a backlog exists."""
    if not (PENDING_ENABLED and _has_pending()):
        return
    threading.Thread(
        target=_flush_pending, name="gcs-pending-flush-once", daemon=True
    ).start()


def pending_status() -> dict:
    """Small snapshot for health/diagnostics."""
    return {
        "pending_enabled": PENDING_ENABLED,
        "pending_dir": PENDING_DIR,
        "pending_count": _pending_count(),
        "gcs_mode": GCS_MODE,
        "live_gcs_enabled": is_live_gcs_enabled(),
        "batch_gcs_enabled": is_batch_gcs_enabled(),
        "local_enabled": is_local_enabled(),
        "local_dir": LOCAL_ROOT,
    }


def upload_json(subdir: str, filename: str, payload: dict) -> Optional[str]:
    """
    Serialize `payload` and upload it to gs://BUCKET/PREFIX/subdir/filename.
    Returns the gs:// URI on success, or None on failure / when disabled.

    On failure the payload is buffered to disk (.pending_gcs/) and retried by
    the background flusher, so data is never lost while auth/network is down.
    """
    if not is_enabled():
        return None

    _ensure_flusher()

    object_name = _full_object_name(subdir, filename)
    try:
        data = json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    except Exception as e:
        logger.error("Failed to serialize payload for %s: %s", object_name, e)
        return None

    ok, detail = _do_upload(object_name, data)

    if ok:
        uri = object_uri(object_name)
        logger.info("Uploaded SERP JSON -> %s", uri)
        # Auth/network is healthy again — opportunistically drain any backlog.
        flush_pending_async()
        return uri

    # Failure: on an auth problem, invalidate the token so the next attempt
    # re-mints; buffer the payload to disk so it is retried (never lost).
    if _is_auth_failure(detail):
        _invalidate_token()
    queued = _enqueue_pending(object_name, data)
    logger.error(
        "GCS upload failed for %s (%s): %s%s",
        object_name, GCS_MODE, detail,
        " — buffered for retry" if queued else " — NOT buffered",
    )
    return None


def delete_object(object_name: str) -> bool:
    """Best-effort delete (used by self-test). Only supports impersonate_vip mode."""
    token = _get_impersonated_token()
    if not token:
        return False
    curl = shutil.which("curl") or "/usr/bin/curl"
    quoted = urllib.parse.quote(object_name, safe="")
    url = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/{quoted}"
    cmd = [
        curl, "-sS", "--noproxy", "*", "--max-time", str(UPLOAD_TIMEOUT),
        "--resolve", f"storage.googleapis.com:443:{RESTRICTED_VIP}",
        "-X", "DELETE", url,
        "-H", f"Authorization: Bearer {token}",
        "-o", "/dev/null", "-w", "%{http_code}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=UPLOAD_TIMEOUT + 5, env=_clean_env())
        return proc.stdout.strip() in ("200", "204")
    except Exception:
        return False


# On import, resume flushing any uploads buffered by a previous run as soon as
# auth/network becomes available again (no-op when disabled or nothing pending).
if PENDING_ENABLED and is_enabled():
    _ensure_flusher()
