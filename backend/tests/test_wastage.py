from datetime import date, timedelta
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import canteen.database as database
from main import app
from ml import prediction_service, train_model


def tiny_dataset(path, days=10):
    rows = []
    start = date(2025, 1, 1)
    for offset in range(days):
        day = start + timedelta(days=offset)
        for name, category, price in [("Tea", "Drinks", 12), ("Dosa", "Breakfast", 45)]:
            rows.append({
                "date": day.isoformat(), "item_name": name, "category": category,
                "unit_of_measure": "pieces", "day_of_week": day.weekday(),
                "is_weekend": int(day.weekday() >= 5), "is_holiday": 0,
                "month": day.month, "unit_price_inr": price,
                "planned_prepared_quantity": 20,
                "historical_avg_sales_7d": 10 + offset / 2,
                "historical_avg_sales_28d": 9 + offset / 3,
                "previous_day_unsold_quantity": offset % 3,
                "actual_unsold_quantity": min(20, (offset + price) % 5),
            })
    pd.DataFrame(rows).to_csv(path, index=False)


def test_dataset_load_and_chronological_training(tmp_path):
    csv_path = tmp_path / "train.csv"
    model_path = tmp_path / "model.pkl"
    tiny_dataset(csv_path, days=10)
    loaded = train_model.load_dataset(csv_path)
    assert len(loaded) == 20
    assert train_model.TARGET not in train_model.FEATURES
    metrics = train_model.train_and_save(csv_path, model_path)
    assert metrics["test_start_date"] > metrics["train_end_date"]
    assert metrics["test_rows"] > 0
    assert model_path.exists()


def test_prediction_is_bounded_and_handles_negative_output(tmp_path, monkeypatch):
    class NegativePipeline:
        def predict(self, rows):
            return [-7]

    model_path = tmp_path / "placeholder.pkl"
    model_path.write_bytes(b"exists")
    monkeypatch.setattr(prediction_service.joblib, "load", lambda _: {"pipeline": NegativePipeline(), "mode": "synthetic_demo", "metrics": {}})
    result = prediction_service.predict_unsold({name: "x" for name in train_model.FEATURES}, 5, model_path)
    assert result["predicted_unsold_quantity"] == 0
    assert result["predicted_sold_quantity"] == 5
    assert result["wastage_percentage"] == 0


def test_missing_model_file_has_clear_error(tmp_path):
    with pytest.raises(prediction_service.ModelUnavailableError, match="missing"):
        prediction_service.predict_unsold({}, 5, tmp_path / "missing.pkl")


def test_prepare_predict_close_and_history_api(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "canteen.db")
    with TestClient(app) as client:
        menu = client.get("/api/items").json()
        item = menu[0]
        day = "2098-04-20"
        prepared = client.post("/api/wastage/prepare", json={
            "business_date": day, "is_holiday": False,
            "items": [{"item_id": item["id"], "planned_prepared_quantity": 18}],
        })
        assert prepared.status_code == 200
        prediction = client.post(f"/api/wastage/predict?business_date={day}")
        assert prediction.status_code == 200
        forecast = prediction.json()["predictions"][0]
        assert 0 <= forecast["predicted_unsold_quantity"] <= 18
        assert forecast["predicted_sold_quantity"] + forecast["predicted_unsold_quantity"] == 18
        assert forecast["prediction_mode"] == "synthetic_demo"
        closing = client.post("/api/wastage/record-closing", json={
            "business_date": day,
            "items": [{"item_id": item["id"], "actual_sold_quantity": 15, "actual_unsold_quantity": 3}],
        })
        assert closing.status_code == 200
        assert len(client.get("/api/wastage/history").json()) == 1
        assert client.get(f"/api/wastage/preparation?business_date={day}").json()[0]["closed_at"]
        assert client.get(f"/api/wastage/predictions?business_date={day}").json()["predictions"]


def test_closing_validation_and_immutability(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "canteen.db")
    with TestClient(app) as client:
        item = client.get("/api/items").json()[0]
        body = {"business_date": "2098-04-21", "items": [{"item_id": item["id"], "planned_prepared_quantity": 10}]}
        assert client.post("/api/wastage/prepare", json=body).status_code == 200
        over = {"business_date": body["business_date"], "items": [{"item_id": item["id"], "actual_sold_quantity": 8, "actual_unsold_quantity": 3}]}
        assert client.post("/api/wastage/record-closing", json=over).status_code == 422
        valid = {"business_date": body["business_date"], "items": [{"item_id": item["id"], "actual_sold_quantity": 8, "actual_unsold_quantity": 2}]}
        assert client.post("/api/wastage/record-closing", json=valid).status_code == 200
        assert client.post("/api/wastage/record-closing", json=valid).status_code == 409

