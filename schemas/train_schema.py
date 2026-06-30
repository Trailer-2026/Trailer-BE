from datetime import datetime

from pydantic import BaseModel, Field


class TrainSearchResponse(BaseModel):
    """열차 검색 결과 항목 (가는편/오는편 탭의 열차 1편)."""

    train_no: str = Field(..., description="열차 번호")
    grade: str = Field(..., description="열차 등급 (KTX, ITX-새마을, 무궁화호, SRT 등)")
    dep_station: str = Field(..., description="출발역명")
    arr_station: str = Field(..., description="도착역명")
    dep_time: datetime = Field(..., description="출발 일시")
    arr_time: datetime = Field(..., description="도착 일시")
    duration_minutes: int = Field(..., description="소요시간(분)")
    fare: int | None = Field(None, description="어른 운임(원). 미제공 시 null")
