from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = None
    sku: str = Field(..., min_length=1, max_length=64)
    price: Decimal = Field(..., gt=0, decimal_places=2)
    stock_quantity: int = Field(0, ge=0)
    is_available: bool = True


class ProductUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = None
    price: Decimal | None = Field(None, gt=0, decimal_places=2)
    is_available: bool | None = None


class StockAdjustment(BaseModel):
    delta: int = Field(..., description="Positive to add stock, negative to remove.")


class ProductResponse(BaseModel):
    id: str
    name: str
    description: str | None
    sku: str
    price: Decimal
    stock_quantity: int
    is_available: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
