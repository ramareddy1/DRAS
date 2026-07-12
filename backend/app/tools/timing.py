"""Date coercion + timing-delta statistics."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def coerce_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", utc=False)


def timing_stats(deltas_days: List[float]) -> Optional[Dict[str, Any]]:
    if not deltas_days:
        return None
    arr = np.array(deltas_days)
    mean = float(np.mean(arr))
    std = float(np.std(arr))
    outliers = int(np.sum(np.abs(arr - mean) > 2 * std)) if std > 0 else 0
    return {
        "mean_days": round(mean, 2),
        "std_days": round(std, 2),
        "min_days": round(float(np.min(arr)), 2),
        "max_days": round(float(np.max(arr)), 2),
        "outliers": outliers,
        "sample_size": int(len(arr)),
    }
