"""Daily preparation/closing workflow and prediction feature construction."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from fastapi import HTTPException
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from .database import connect, now
from ml.prediction_service import ModelUnavailableError, predict_unsold
from ml.train_model import FEATURES, CATEGORICAL, NUMERIC

REAL_MIN_ROWS = 56
REAL_MIN_DAYS = 28
REAL_MIN_ITEM_ROWS = 28


def _date_text(value: date) -> str:
    return value.isoformat()


def prepare_day(payload):
    business_date = _date_text(payload.business_date)
    ids = [entry.item_id for entry in payload.items]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "Each menu item can appear only once in a preparation submission.")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        results = []
        for entry in payload.items:
            item = con.execute("SELECT * FROM items WHERE id=?", (entry.item_id,)).fetchone()
            if not item:
                raise HTTPException(404, f"Menu item {entry.item_id} was not found.")
            existing = con.execute("SELECT * FROM daily_wastage_records WHERE business_date=? AND item_id=?", (business_date, entry.item_id)).fetchone()
            if existing and existing["closed_at"]:
                raise HTTPException(409, f"Closing data for {item['name']} on {business_date} is already recorded.")
            if existing:
                con.execute("""UPDATE daily_wastage_records SET item_name=?,category=?,unit_price_inr=?,
                    planned_prepared_quantity=?,is_holiday=?,prepared_at=?,predicted_sold_quantity=NULL,
                    predicted_unsold_quantity=NULL,prediction_mode=NULL WHERE id=?""",
                    (item["name"], item["category"], item["price"], entry.planned_prepared_quantity,
                     int(payload.is_holiday), now().isoformat(), existing["id"]))
                row_id = existing["id"]
            else:
                cur = con.execute("""INSERT INTO daily_wastage_records
                    (business_date,item_id,item_name,category,unit_of_measure,unit_price_inr,
                     planned_prepared_quantity,is_holiday,prepared_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (business_date, item["id"], item["name"], item["category"], "pieces", item["price"],
                     entry.planned_prepared_quantity, int(payload.is_holiday), now().isoformat()))
                row_id = cur.lastrowid
            results.append(dict(con.execute("SELECT * FROM daily_wastage_records WHERE id=?", (row_id,)).fetchone()))
    return results


def record_closing(payload):
    business_date = _date_text(payload.business_date)
    ids = [entry.item_id for entry in payload.items]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "Each menu item can appear only once in a closing submission.")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        results = []
        for entry in payload.items:
            row = con.execute("SELECT * FROM daily_wastage_records WHERE business_date=? AND item_id=?", (business_date, entry.item_id)).fetchone()
            if not row:
                raise HTTPException(404, f"No prepared quantity was recorded for item {entry.item_id} on {business_date}.")
            if row["closed_at"]:
                raise HTTPException(409, f"Closing data for {row['item_name']} on {business_date} is already recorded.")
            if entry.actual_sold_quantity + entry.actual_unsold_quantity != row["planned_prepared_quantity"]:
                raise HTTPException(422, f"Sold plus unsold quantity for {row['item_name']} must equal the prepared quantity ({row['planned_prepared_quantity']}).")
            timestamp = now().isoformat()
            con.execute("""UPDATE daily_wastage_records SET actual_sold_quantity=?,actual_unsold_quantity=?,closed_at=? WHERE id=?""",
                        (entry.actual_sold_quantity, entry.actual_unsold_quantity, timestamp, row["id"]))
            results.append(dict(con.execute("SELECT * FROM daily_wastage_records WHERE id=?", (row["id"],)).fetchone()))
    return results


def list_history(limit=100):
    with connect() as con:
        rows = con.execute("""SELECT * FROM daily_wastage_records WHERE closed_at IS NOT NULL
            ORDER BY business_date DESC,item_name LIMIT ?""", (max(1, min(int(limit), 500)),)).fetchall()
    return [dict(row) for row in rows]


def list_preparation(business_date: date):
    with connect() as con:
        rows = con.execute("""SELECT * FROM daily_wastage_records WHERE business_date=?
            ORDER BY item_name""", (_date_text(business_date),)).fetchall()
    return [dict(row) for row in rows]


def _history_rows(con, item_id: int, before_date: str):
    rows = con.execute("""SELECT business_date,item_name,category,unit_price_inr,is_holiday,
        planned_prepared_quantity,actual_sold_quantity,actual_unsold_quantity
        FROM daily_wastage_records WHERE item_id=? AND closed_at IS NOT NULL AND business_date<?
        ORDER BY business_date""", (item_id, before_date)).fetchall()
    return [dict(row) for row in rows]


def _features_for_day(item, plan, prediction_date: date, is_holiday: bool, prior_rows):
    cutoff7 = (prediction_date - timedelta(days=7)).isoformat()
    cutoff28 = (prediction_date - timedelta(days=28)).isoformat()
    last7 = [row["actual_sold_quantity"] for row in prior_rows if cutoff7 <= row["business_date"] < prediction_date.isoformat()]
    last28 = [row["actual_sold_quantity"] for row in prior_rows if cutoff28 <= row["business_date"] < prediction_date.isoformat()]
    previous_day = (prediction_date - timedelta(days=1)).isoformat()
    previous = next((row["actual_unsold_quantity"] for row in reversed(prior_rows) if row["business_date"] == previous_day), 0)
    return {
        "item_name": item["name"], "category": item["category"],
        "day_of_week": prediction_date.weekday(), "is_weekend": int(prediction_date.weekday() >= 5),
        "is_holiday": int(is_holiday), "month": prediction_date.month,
        "unit_price_inr": float(item["price"]), "planned_prepared_quantity": int(plan),
        "historical_avg_sales_7d": float(sum(last7) / len(last7)) if last7 else 0.0,
        "historical_avg_sales_28d": float(sum(last28) / len(last28)) if last28 else 0.0,
        "previous_day_unsold_quantity": int(previous or 0),
    }


def predict_for_date(prediction_date: date, is_holiday: bool | None = None):
    day = _date_text(prediction_date)
    with connect() as con:
        planned = con.execute("""SELECT d.*,i.name AS current_name,i.category AS current_category,
            i.price AS current_price,i.stock_quantity FROM daily_wastage_records d
            JOIN items i ON i.id=d.item_id WHERE d.business_date=? ORDER BY d.item_name""", (day,)).fetchall()
        if not planned:
            raise HTTPException(404, f"Record planned quantities for {day} before requesting a prediction.")
        records = []
        for row in planned:
            item = {"name": row["current_name"], "category": row["current_category"], "price": row["current_price"]}
            holiday_for_day = bool(row["is_holiday"] if is_holiday is None else is_holiday)
            con.execute("UPDATE daily_wastage_records SET is_holiday=? WHERE id=?", (int(holiday_for_day), row["id"]))
            features = _features_for_day(item, row["planned_prepared_quantity"], prediction_date,
                                         holiday_for_day, _history_rows(con, row["item_id"], day))
            records.append((row, item, features))
        # Real mode only activates after at least eight weeks worth of closed item-days.
        # Every feature row is recomputed from data strictly earlier than its target day.
        closed = con.execute("""SELECT * FROM daily_wastage_records WHERE closed_at IS NOT NULL
            AND business_date<? ORDER BY business_date,item_id""", (day,)).fetchall()
        closed_dicts = [dict(r) for r in closed]
        distinct_days = {r["business_date"] for r in closed_dicts}
        counts_by_item = {}
        dates_by_item = {}
        for hist in closed_dicts:
            counts_by_item[hist["item_id"]] = counts_by_item.get(hist["item_id"], 0) + 1
            dates_by_item.setdefault(hist["item_id"], []).append(hist["business_date"])
        has_item_history = all(
            counts_by_item.get(row["item_id"], 0) >= REAL_MIN_ITEM_ROWS
            and (date.fromisoformat(max(dates_by_item.get(row["item_id"], [day])))
                 - date.fromisoformat(min(dates_by_item.get(row["item_id"], [day])))).days >= REAL_MIN_DAYS - 1
            for row in planned
        )
        real_mode = len(closed_dicts) >= REAL_MIN_ROWS and len(distinct_days) >= REAL_MIN_DAYS and has_item_history
        real_pipeline = None
        if real_mode:
            train_features = []
            train_target = []
            by_item = {}
            for hist in closed_dicts:
                hdate = date.fromisoformat(hist["business_date"])
                prior = by_item.get(hist["item_id"], [])
                item = {"name": hist["item_name"], "category": hist["category"], "price": hist["unit_price_inr"]}
                train_features.append(_features_for_day(item, hist["planned_prepared_quantity"], hdate,
                    bool(hist["is_holiday"]), prior))
                train_target.append(hist["actual_unsold_quantity"])
                by_item.setdefault(hist["item_id"], []).append(hist)
            pre = ColumnTransformer([
                ("categories", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL),
                ("numbers", "passthrough", NUMERIC),
            ])
            real_pipeline = Pipeline([
                ("preprocessing", pre), ("regressor", RandomForestRegressor(n_estimators=200, min_samples_leaf=2, random_state=42, n_jobs=-1)),
            ]).fit(pd.DataFrame(train_features, columns=FEATURES), train_target)

        results = []
        for row, item, features in records:
            if real_pipeline is not None:
                prediction = float(real_pipeline.predict(pd.DataFrame([features], columns=FEATURES))[0])
                unsold = min(row["planned_prepared_quantity"], max(0, int(round(prediction))))
                modeled = {"predicted_unsold_quantity": unsold,
                           "predicted_sold_quantity": row["planned_prepared_quantity"] - unsold,
                           "wastage_percentage": round(100 * unsold / row["planned_prepared_quantity"], 2) if row["planned_prepared_quantity"] else 0.0,
                           "prediction_mode": "real_data"}
            else:
                try:
                    modeled = predict_unsold(features, row["planned_prepared_quantity"])
                except ModelUnavailableError as exc:
                    raise HTTPException(503, str(exc)) from exc
            con.execute("""UPDATE daily_wastage_records SET predicted_sold_quantity=?,predicted_unsold_quantity=?,
                prediction_mode=? WHERE id=?""", (modeled["predicted_sold_quantity"], modeled["predicted_unsold_quantity"], modeled["prediction_mode"], row["id"]))
            results.append({
                "item_id": row["item_id"], "item_name": item["name"],
                "planned_prepared_quantity": row["planned_prepared_quantity"],
                "predicted_sold_quantity": modeled["predicted_sold_quantity"],
                "predicted_unsold_quantity": modeled["predicted_unsold_quantity"],
                "wastage_percentage": modeled["wastage_percentage"],
                "unit_of_measure": row["unit_of_measure"], "prediction_date": day,
                "prediction_mode": modeled["prediction_mode"],
                "high_wastage_warning": modeled["wastage_percentage"] >= 25,
            })
    mode = "real_data" if real_mode else "synthetic_demo"
    return {"prediction_date": day, "prediction_mode": mode,
            "warning": ("Demonstration mode: trained on supplied synthetic data; predictions are not validated for this canteen." if mode == "synthetic_demo" else None),
            "total_predicted_wastage": sum(row["predicted_unsold_quantity"] for row in results),
            "predictions": results}


def get_predictions(prediction_date: date):
    with connect() as con:
        rows = con.execute("""SELECT * FROM daily_wastage_records WHERE business_date=?
            AND predicted_unsold_quantity IS NOT NULL ORDER BY item_name""", (_date_text(prediction_date),)).fetchall()
    predictions = []
    for row in rows:
        prepared = row["planned_prepared_quantity"]
        unsold = row["predicted_unsold_quantity"]
        pct = round(100 * unsold / prepared, 2) if prepared else 0.0
        predictions.append({"item_id": row["item_id"], "item_name": row["item_name"],
            "planned_prepared_quantity": prepared, "predicted_sold_quantity": row["predicted_sold_quantity"],
            "predicted_unsold_quantity": unsold, "wastage_percentage": pct,
            "unit_of_measure": row["unit_of_measure"], "prediction_date": row["business_date"],
            "prediction_mode": row["prediction_mode"], "high_wastage_warning": pct >= 25})
    return {"prediction_date": _date_text(prediction_date),
            "prediction_mode": rows[0]["prediction_mode"] if rows else None,
            "total_predicted_wastage": sum(row["predicted_unsold_quantity"] for row in predictions),
            "predictions": predictions}

