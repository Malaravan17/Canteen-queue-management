from typing import Optional
from pydantic import BaseModel, Field

class ItemInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str = "Snacks"
    price: float = Field(ge=0)
    stock_quantity: int = Field(ge=0)
    prep_minutes: int = Field(default=3, ge=1, le=120)
    is_available: bool = True
    image_emoji: str = "🍽️"
    description: str = ""


class StockInput(BaseModel):
    stock_quantity: int = Field(ge=0)


class OrderLine(BaseModel):
    item_id: str
    quantity: int = Field(ge=1, le=100)


class OrderInput(BaseModel):
    customer_name: str = Field(default="Student", min_length=1, max_length=80)
    items: list[OrderLine]
    combo_id: Optional[str] = None


class StatusInput(BaseModel):
    status: str


class PaymentInput(BaseModel):
    method: str
    simulate_success: bool = True


class CounterInput(BaseModel):
    is_active: bool


class SettingsInput(BaseModel):
    peak_mode: bool
    auto_open_threshold: int = Field(default=5, ge=1, le=50)
    event_name: str = ""


class QRScanInput(BaseModel):
    name: str = Field(default="Customer", min_length=1, max_length=60)

