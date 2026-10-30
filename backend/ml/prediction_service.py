"""Safe, bounded wastage prediction backed by the saved preprocessing pipeline."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from .train_model import FEATURES, MODEL_PATH


class ModelUnavailableError(RuntimeError):
    pass


def predict_unsold(features: dict[str, Any], prepared: int, model_path: str | Path = MODEL_PATH) -> dict:
    if prepared < 0:
        raise ValueError("Planned prepared quantity must be non-negative.")
    path = Path(model_path)
    if not path.is_file():
        raise ModelUnavailableError(f"Food wastage model file is missing: {path}. Train it with backend/ml/train_model.py.")
    artifact = joblib.load(path)
    missing = [name for name in FEATURES if name not in features]
    if missing:
        raise ValueError(f"Missing prediction features: {', '.join(missing)}")
    row = {name: features[name] for name in FEATURES}
    estimate = float(artifact["pipeline"].predict(pd.DataFrame([row], columns=FEATURES))[0])
    # Never allow negative or greater-than-prepared quantities into the API/UI.
    unsold = min(prepared, max(0, int(round(estimate))))
    return {
        "predicted_unsold_quantity": unsold,
        "predicted_sold_quantity": prepared - unsold,
        "wastage_percentage": round((100 * unsold / prepared) if prepared else 0.0, 2),
        "prediction_mode": artifact.get("mode", "synthetic_demo"),
        "model_metrics": artifact.get("metrics", {}),
    }


def feature_importance(model_path: str | Path = MODEL_PATH) -> list[dict]:
    path = Path(model_path)
    if not path.is_file():
        raise ModelUnavailableError(f"Food wastage model file is missing: {path}")
    artifact = joblib.load(path)
    return artifact.get("metrics", {}).get("feature_importance", [])

