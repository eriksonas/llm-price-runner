from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


class ModelEntry(BaseModel):
    id: str
    provider: str
    model: str
    model_id: str
    categories: List[str]
    input_usd_per_1m: float
    output_usd_per_1m: float
    context_window_k: int
    primary_region: str
    has_eu_endpoint: bool
    eu_latency_ms: int
    quality_score: float
    pricing_url: str
    docs_url: str
    notes: str
    is_new: bool
    value_score: Optional[float] = None
    eu_value_score: Optional[float] = None


class PriceUpdate(BaseModel):
    id: str = Field(max_length=200)
    # ge=0: some free-tier endpoints legitimately price at $0. The upper
    # bound is a sanity cap, not a real price — no endpoint charges
    # anywhere near $100k/1M tokens.
    input_usd_per_1m: float = Field(ge=0, le=100_000)
    output_usd_per_1m: float = Field(ge=0, le=100_000)
    notes: Optional[str] = Field(default=None, max_length=500)


class PriceHistoryEntry(BaseModel):
    model_id: str
    input_usd_per_1m: float
    output_usd_per_1m: float
    recorded_at: datetime
