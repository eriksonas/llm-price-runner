from pydantic import BaseModel
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
    id: str
    input_usd_per_1m: float
    output_usd_per_1m: float
    notes: Optional[str] = None


class PriceHistoryEntry(BaseModel):
    model_id: str
    input_usd_per_1m: float
    output_usd_per_1m: float
    recorded_at: datetime
