from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, Query

from .schemas import WastageClosingInput, WastagePrepareInput
from .services import require_admin
from .wastage_service import get_predictions, list_history, list_preparation, predict_for_date, prepare_day, record_closing

router = APIRouter()

def _today_utc():
    return datetime.now(timezone.utc).date()


@router.post("/api/wastage/predict")
def predict_wastage(business_date: date = Query(default_factory=_today_utc),
                    is_holiday: Optional[bool] = None,
                    x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    return predict_for_date(business_date, is_holiday)


@router.get("/api/wastage/predictions")
def read_predictions(business_date: date = Query(default_factory=_today_utc),
                     x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    return get_predictions(business_date)


@router.get("/api/wastage/preparation")
def read_preparation(business_date: date = Query(default_factory=_today_utc),
                     x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    return list_preparation(business_date)


@router.post("/api/wastage/prepare")
def prepare_wastage(payload: WastagePrepareInput, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    return {"business_date": payload.business_date.isoformat(), "items": prepare_day(payload)}


@router.post("/api/wastage/record-closing")
def record_wastage_closing(payload: WastageClosingInput, x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    return {"business_date": payload.business_date.isoformat(), "items": record_closing(payload)}


@router.get("/api/wastage/history")
def read_wastage_history(limit: int = Query(default=100, ge=1, le=500),
                         x_admin_key: Optional[str] = Header(default=None)):
    require_admin(x_admin_key)
    return list_history(limit)
