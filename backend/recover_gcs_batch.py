"""Recover an interrupted batch from an early XLSX export and per-part GCS JSON."""

import argparse
import json
import shutil
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

import gcs_storage
import na_parts_main as api


def _identity(values: dict) -> tuple[str, str, str, str]:
    return tuple(
        str(values.get(key, "")).strip().upper()
        for key in ("brand_part_number", "brand_name", "retailer_name", "mli")
    )


def _download_json(object_name: str, token: str) -> tuple[dict | None, str]:
    quoted = urllib.parse.quote(object_name, safe="")
    url = f"https://storage.googleapis.com/storage/v1/b/{gcs_storage.BUCKET}/o/{quoted}?alt=media"
    command = [
        shutil.which("curl") or "/usr/bin/curl",
        "-sS",
        "--noproxy",
        "*",
        "--max-time",
        str(gcs_storage.UPLOAD_TIMEOUT),
        "--resolve",
        f"storage.googleapis.com:443:{gcs_storage.RESTRICTED_VIP}",
        "-H",
        f"Authorization: Bearer {token}",
        "-w",
        "\n__HTTPCODE__%{http_code}",
        url,
    ]
    for _ in range(3):
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                timeout=gcs_storage.UPLOAD_TIMEOUT + 5,
                env=gcs_storage._clean_env(),
            )
        except Exception as error:
            detail = str(error)
            continue

        output = process.stdout.decode("utf-8", "replace")
        body, _, status = output.rpartition("__HTTPCODE__")
        status = status.strip()
        if process.returncode == 0 and status == "200":
            try:
                return json.loads(body), "ok"
            except json.JSONDecodeError as error:
                return None, f"invalid_json: {error}"
        detail = f"curl_{process.returncode}_http_{status or 'unknown'}"
    return None, detail


def _record_from_gcs(input_row: dict, payload: dict, columns: list[str]) -> dict:
    record = {column: "" for column in columns}
    for key in ("retailer_name", "brand_part_number", "brand_name", "mli"):
        if key in record:
            record[key] = str(input_row.get(key, "")).strip()
    if "part_price" in record:
        record["part_price"] = payload.get("part_price", "")

    result_fields = api._batch_result_fields(payload.get("result") or {})
    for column_name, result_key in api.BATCH_RESULT_COLUMN_MAP:
        if column_name in record:
            record[column_name] = result_fields[result_key]
    return record


def recover(args: argparse.Namespace) -> None:
    input_path = Path(args.input).expanduser().resolve()
    export_path = Path(args.existing_export).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_input = pd.read_excel(input_path, dtype=str).fillna("")
    batch_rows = api._build_single_retailer_batch_df(raw_input).reset_index(drop=True)
    exported = pd.read_excel(export_path).fillna("").reset_index(drop=True)

    if len(batch_rows) != len(raw_input):
        raise RuntimeError("Input contains invalid rows; original row positions cannot be preserved safely.")
    if not 0 <= args.processed_rows <= len(batch_rows):
        raise RuntimeError("processed_rows is outside the input row range.")
    if len(exported) > args.processed_rows:
        raise RuntimeError("Existing export has more rows than the completed stream boundary.")

    for index, exported_row in exported.iterrows():
        expected = _identity(batch_rows.iloc[index].to_dict())
        actual = _identity(exported_row.to_dict())
        if expected != actual:
            raise RuntimeError(f"Existing export no longer matches the input at row {index + 2}.")

    columns = list(exported.columns)
    records = exported.to_dict("records")
    statuses = ["browser_export"] * len(records)
    recovery_rows = batch_rows.iloc[len(exported):args.processed_rows]

    object_names: dict[str, str] = {}
    row_tokens: list[str] = []
    for _, row in recovery_rows.iterrows():
        token = api._sanitize_filename_token(str(row["brand_part_number"]), "part")
        row_tokens.append(token)
        object_names[token] = "/".join(
            part
            for part in (
                gcs_storage.PREFIX,
                gcs_storage.BATCH_SUBDIR,
                args.date_folder,
                f"{token}.json",
            )
            if part
        )

    token = gcs_storage._get_impersonated_token()
    if not token:
        raise RuntimeError("Could not obtain an impersonated GCP access token.")

    downloaded: dict[str, tuple[dict | None, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(_download_json, object_name, token): part_token
            for part_token, object_name in object_names.items()
        }
        for future in as_completed(futures):
            downloaded[futures[future]] = future.result()

    gaps = []
    for (_, row), part_token in zip(recovery_rows.iterrows(), row_tokens):
        input_record = row.to_dict()
        payload, detail = downloaded[part_token]
        if payload is None:
            record = _record_from_gcs(input_record, {}, columns)
            status = f"gcs_missing:{detail}"
        elif _identity(payload) != _identity(input_record):
            record = _record_from_gcs(input_record, {}, columns)
            status = "gcs_object_overwritten_by_same_filename"
        else:
            record = _record_from_gcs(input_record, payload, columns)
            status = "gcs_exact"
        records.append(record)
        statuses.append(status)
        if status != "gcs_exact":
            gaps.append({**input_record, "recovery_status": status})

    completed = pd.DataFrame(records, columns=columns)
    completed["recovery_status"] = statuses
    remaining = raw_input.iloc[args.processed_rows:].copy()
    remaining.insert(0, "original_row_number", range(args.processed_rows + 2, len(raw_input) + 2))

    stem = input_path.stem
    completed_path = output_dir / f"{stem}_recovered_{args.processed_rows}.xlsx"
    remaining_path = output_dir / f"{stem}_remaining_{len(remaining)}.xlsx"
    gaps_path = output_dir / f"{stem}_recovery_gaps_{len(gaps)}.xlsx"
    completed.to_excel(completed_path, index=False)
    remaining.to_excel(remaining_path, index=False)
    pd.DataFrame(gaps).to_excel(gaps_path, index=False)

    exact_count = sum(status in {"browser_export", "gcs_exact"} for status in statuses)
    print(f"Input rows: {len(batch_rows)}")
    print(f"Stream-completed rows: {args.processed_rows}")
    print(f"Exactly recovered rows: {exact_count}")
    print(f"Recovery gaps: {len(gaps)}")
    print(f"Unprocessed rows: {len(remaining)}")
    print(f"Recovered workbook: {completed_path}")
    print(f"Recovery gaps: {gaps_path}")
    print(f"Remaining workbook: {remaining_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Original batch XLSX")
    parser.add_argument("--existing-export", required=True, help="Early browser XLSX export")
    parser.add_argument("--processed-rows", required=True, type=int, help="Rows completed before disconnect")
    parser.add_argument("--date-folder", required=True, help="GCS batch date folder, e.g. 20260814")
    parser.add_argument("--output-dir", default="~/Downloads", help="Directory for recovered workbooks")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent GCS downloads")
    recover(parser.parse_args())


if __name__ == "__main__":
    main()