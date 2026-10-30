from typing import Optional
from pydantic import BaseModel, Field
from datetime import date
from pydantic import field_validator

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


class ClearOperationalDataInput(BaseModel):
    confirmation: str = Field(min_length=1, max_length=32)


class QRScanInput(BaseModel):
    name: str = Field(default="Customer", min_length=1, max_length=60)


class WastagePrepareItem(BaseModel):
    item_id: int = Field(gt=0)
    planned_prepared_quantity: int = Field(ge=0, le=100000)


class WastagePrepareInput(BaseModel):
    business_date: date
    is_holiday: bool = False
    items: list[WastagePrepareItem] = Field(min_length=1, max_length=200)


class WastageClosingItem(BaseModel):
    item_id: int = Field(gt=0)
    actual_sold_quantity: int = Field(ge=0, le=100000)
    actual_unsold_quantity: int = Field(ge=0, le=100000)


class WastageClosingInput(BaseModel):
    business_date: date
    items: list[WastageClosingItem] = Field(min_length=1, max_length=200)


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    session_id: Optional[str] = Field(default=None, min_length=8, max_length=100)
    order_id: Optional[str] = Field(default=None, min_length=1, max_length=20)
    tracking_token: Optional[str] = Field(default=None, min_length=16, max_length=128)

    @field_validator("message")
    @classmethod
    def message_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be empty.")
        return value

    @field_validator("session_id")
    @classmethod
    def session_id_format(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not all(char.isalnum() or char in "-_" for char in value):
            raise ValueError("Invalid session ID.")
        return value

    @field_validator("order_id")
    @classmethod
    def order_id_digits(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.isdigit():
            raise ValueError("Order ID must be numeric.")
        return value

