"""File ingestion: robust CSV/XLSX reading.

Pure function over bytes → DataFrame. Handles UTF-8 / UTF-8-BOM / Latin-1
fallback, strips whitespace from headers and string cells, restores NaN for
empty / "nan" / "None" strings.
"""
from __future__ import annotations

import io
from typing import Any, Dict

import pandas as pd


def read_table(data: bytes, filename: str) -> pd.DataFrame:
    name = filename.lower()
    if name.endswith(".xlsx") or name.endswith(".xls") or name.endswith(".xlsm"):
        df = pd.read_excel(io.BytesIO(data))
    else:
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                df = pd.read_csv(io.BytesIO(data), encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("Could not decode file with utf-8 or latin-1.")

    df.columns = [str(c).strip() for c in df.columns]
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].astype(str).str.strip()
            df[c] = df[c].replace({"": None, "nan": None, "NaN": None, "None": None})
    return df


def preview(df: pd.DataFrame, n: int = 5) -> Dict[str, Any]:
    rows = df.head(n).fillna("").to_dict(orient="records")
    return {
        "columns": list(df.columns),
        "rows": rows,
        "row_count": int(len(df)),
    }
