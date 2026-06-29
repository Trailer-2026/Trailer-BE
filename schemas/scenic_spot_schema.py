from typing import Optional, List
from pydantic import BaseModel, Field


class ScenicSpotResponse(BaseModel):
    name: Optional[str] = Field(None, description="관광지 이름")
    category: str = Field(..., description="분류 (water | waterway | peak | natural_view)")
    distance_m: float = Field(..., description="현재 좌표로부터의 거리(m)")
    side: Optional[str] = Field(None, description="진행 방향 기준 창밖 좌우 (left | right)")


class ScenicSpotNearbyResponse(BaseModel):
    feature_count: int = Field(..., description="조회된 관광지 수")
    items: List[ScenicSpotResponse] = Field(..., description="구간에서 보이는 관광지 목록")
