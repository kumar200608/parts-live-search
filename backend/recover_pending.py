"""Recover batch rows buffered locally in .pending_gcs/ into an Excel + CSV.

Reads every pending envelope (written when a GCS upload could not complete),
decodes the stored payload, and reconstructs the batch result rows. Purely
local: no network, no gcloud, no SerpApi credits.
"""
import base64
import glob
import json
import os
from datetime import datetime

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_DIR = os.path.join(BASE_DIR, ".pending_gcs")
OUT_DIR = os.path.dirname(BASE_DIR)  # part_pricing_ui_serp_na_v1/

COLUMNS = [
    "retailer_name", "brand_part_number", "brand_name", "mli",
    "part_price", "found", "error", "scraped_at", "source_object",
]

rows = []
files = sorted(glob.glob(os.path.join(PENDING_DIR, "*.json")))
for path in files:
    try:
        with open(path, "r", encoding="utf-8") as f:
            env = json.load(f)
        payload = json.loads(base64.b64decode(env["data_b64"]).decode("utf-8"))
    except Exception as e:
        print(f"  ! skip {os.path.basename(path)}: {e}")
        continue
    rows.append({
        "retailer_name":     payload.get("retailer_name", ""),
        "brand_part_number": payload.get("brand_part_number", ""),
        "brand_name":        payload.get("brand_name", ""),
        "mli":               payload.get("mli", ""),
        "part_price":        payload.get("part_price", ""),
        "found":             payload.get("found", False),
        "error":             payload.get("error", ""),
        "scraped_at":        payload.get("scraped_at", ""),
        "source_object":     env.get("object_name", ""),
    })

df = pd.DataFrame(rows, columns=COLUMNS)
df = df.sort_values("scraped_at").reset_index(drop=True)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
xlsx_path = os.path.join(OUT_DIR, f"recovered_last_batch_{stamp}.xlsx")
csv_path = os.path.join(OUT_DIR, f"recovered_last_batch_{stamp}.csv")
df.to_excel(xlsx_path, index=False)
df.to_csv(csv_path, index=False)

with_price = int((df["part_price"].astype(str).str.strip() != "").sum())
found_ct = int(df["found"].sum()) if "found" in df else 0
print(f"Recovered rows      : {len(df)}")
print(f"  with a price      : {with_price}")
print(f"  found=True        : {found_ct}")
print(f"Excel -> {xlsx_path}")
print(f"CSV   -> {csv_path}")
