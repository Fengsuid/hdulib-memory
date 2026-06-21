from datetime import datetime

from pydantic import BaseModel, Field


class BookingResult(BaseModel):
    """预订结果模型"""

    success: bool
    user: str
    seat_info: str
    room_name: str | None = None
    floor_id: str | None = None
    floor_name: str | None = None
    seat_number: str | None = None
    booking_time: str | None = None
    begin_timestamp: int | None = None
    duration: str | None = None
    duration_hours: float | None = None
    attempt: int | None = None
    attempts: int | None = None
    message: str | None = None
    error: str | None = None


class MemoryRecord(BaseModel):
    """图书馆记忆记录模型"""

    record_id: str
    source: str = "local"
    booking_id: str | None = None
    user: str
    seat_info: str
    room_name: str | None = None
    floor_id: str | None = None
    floor_name: str | None = None
    seat_number: str | None = None
    begin_time: datetime
    end_time: datetime | None = None
    duration_hours: float = Field(..., ge=0)
    status: str = "success"
    created_at: datetime
    raw: dict = Field(default_factory=dict)
