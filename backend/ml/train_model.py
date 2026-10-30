"""Train/evaluate the food wastage Random Forest.

Usage (from backend): python -m ml.train_model PATH_TO_CSV
The chronological holdout metrics describe synthetic data, not canteen quality.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import json

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

SEED = 42
TARGET = "actual_unsold_quantity"
CATEGORICAL = ["item_name", "category"]
NUMERIC = [
    "day_of_week", "is_weekend", "is_holiday", "month", "unit_price_inr",
    "planned_prepared_quantity", "historical_avg_sales_7d", "historical_avg_sales_28d",
    "previous_day_unsold_quantity",
]
FEATURES = CATEGORICAL + NUMERIC
REQUIRED = ["date", *FEATURES, TARGET]
MODEL_DIR = Path(__file__).resolve().parent / "models"
MODEL_PATH = MODEL_DIR / "food_wastage_model.pkl"


def load_dataset(csv_path: str | Path) -> pd.DataFrame:
    """Load and validate training data without ever using the target as a feature."""
    df = pd.read_csv(csv_path)
    missing_columns = sorted(set(REQUIRED) - set(df.columns))
    if missing_columns:
        raise ValueError(f"Dataset is missing required columns: {', '.join(missing_columns)}")
    df = df[REQUIRED].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    if df["date"].isna().any():
        raise ValueError("Dataset contains invalid dates.")
    if df[FEATURES + [TARGET]].isna().any().any():
        raise ValueError("Dataset has missing values in features or target.")
    for col in NUMERIC + [TARGET]:
        df[col] = pd.to_numeric(df[col], errors="raise")
        if not np.isfinite(df[col]).all():
            raise ValueError(f"Dataset column {col} contains non-finite values.")
    if (df["planned_prepared_quantity"] < 0).any() or (df[TARGET] < 0).any():
        raise ValueError("Prepared and unsold quantities must be non-negative.")
    if (df[TARGET] > df["planned_prepared_quantity"]).any():
        raise ValueError("Unsold quantity cannot exceed prepared quantity.")
    if not df["day_of_week"].between(0, 6).all() or not df["month"].between(1, 12).all():
        raise ValueError("Day-of-week or month values are outside valid ranges.")
    if not df["is_weekend"].isin([0, 1]).all() or not df["is_holiday"].isin([0, 1]).all():
        raise ValueError("Weekend and holiday flags must be 0 or 1.")
    if df.duplicated(["date", "item_name"]).any():
        raise ValueError("Dataset contains duplicate date/item rows.")
    return df.sort_values(["date", "item_name"]).reset_index(drop=True)


def make_pipeline() -> Pipeline:
    preprocessing = ColumnTransformer([
        ("categories", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL),
        ("numbers", "passthrough", NUMERIC),
    ])
    return Pipeline([
        ("preprocessing", preprocessing),
        ("regressor", RandomForestRegressor(
            n_estimators=300, min_samples_leaf=2, random_state=SEED, n_jobs=-1,
        )),
    ])


def train_and_save(csv_path: str | Path, model_path: str | Path = MODEL_PATH) -> dict:
    df = load_dataset(csv_path)
    unique_dates = np.array(sorted(df["date"].unique()))
    if len(unique_dates) < 2:
        raise ValueError("At least two distinct dates are required for a chronological split.")
    split_index = max(1, min(len(unique_dates) - 1, int(len(unique_dates) * 0.8)))
    cutoff = pd.Timestamp(unique_dates[split_index])
    train = df[df.date < cutoff]
    test = df[df.date >= cutoff]
    if train.empty or test.empty:
        raise ValueError("Chronological train/test split produced an empty partition.")

    evaluation_pipeline = make_pipeline()
    evaluation_pipeline.fit(train[FEATURES], train[TARGET])
    predictions = np.clip(evaluation_pipeline.predict(test[FEATURES]), 0, test["planned_prepared_quantity"].to_numpy())
    metrics = {
        "algorithm": "RandomForestRegressor",
        "random_seed": SEED,
        "target": TARGET,
        "features": FEATURES,
        "training_rows": int(len(train)),
        "test_rows": int(len(test)),
        "train_end_date": train.date.max().date().isoformat(),
        "test_start_date": test.date.min().date().isoformat(),
        "mae": float(mean_absolute_error(test[TARGET], predictions)),
        "rmse": float(np.sqrt(mean_squared_error(test[TARGET], predictions))),
        "r2": float(r2_score(test[TARGET], predictions)),
        "test_target_zero_rows": int((test[TARGET] == 0).sum()),
        "test_target_nonzero_rows": int((test[TARGET] != 0).sum()),
        "test_target_std": float(test[TARGET].std()),
        "synthetic_target_equals_previous_day_unsold_rows": int((df[TARGET] == df["previous_day_unsold_quantity"]).sum()),
        "rows": int(len(df)),
        "synthetic_data_caveat": "Holdout scores describe the supplied synthetic dataset only; target exactly matches previous-day unsold on many rows.",
    }

    # Deploy a final pipeline trained on all supplied synthetic rows. Evaluation above
    # remains a strict date holdout and is not reused for model selection.
    final_pipeline = make_pipeline().fit(df[FEATURES], df[TARGET])
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "pipeline": final_pipeline,
        "features": FEATURES,
        "target": TARGET,
        "mode": "synthetic_demo",
        "trained_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": metrics,
    }
    joblib.dump(artifact, model_path)
    metrics_path = model_path.with_name("training_metrics.json")
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    preprocessing = final_pipeline.named_steps["preprocessing"]
    transformed_names = preprocessing.get_feature_names_out()
    importance = final_pipeline.named_steps["regressor"].feature_importances_
    metrics["feature_importance"] = sorted(
        [{"feature": str(name), "importance": float(value)} for name, value in zip(transformed_names, importance)],
        key=lambda entry: entry["importance"], reverse=True,
    )
    # Save/importances into both human-readable metrics and model metadata.
    artifact["metrics"] = metrics
    joblib.dump(artifact, model_path)
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="Path to smart_canteen_food_wastage_dataset.csv")
    parser.add_argument("--model-path", default=str(MODEL_PATH))
    args = parser.parse_args()
    print(json.dumps(train_and_save(args.csv_path, args.model_path), indent=2))


if __name__ == "__main__":
    main()
