from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class ScenicSpotResponse(BaseModel):
    name: Optional[str] = Field(None, description="관광지 이름")
    category: str = Field(..., description="분류 (water | waterway | peak | natural_view)")
    distance_m: float = Field(..., description="현재 좌표로부터의 거리(m)")
    side: Optional[str] = Field(None, description="진행 방향 기준 창밖 좌우 (left | right)")
    image_url: Optional[str] = Field(
        None,
        description="카테고리에 맞는 풍경 일러스트 URL(공개). 알림 화면 상단 풍경 카드와 "
                    "푸시 배너가 같은 그림을 쓴다. 카테고리별 그림이 준비되기 전까지는 "
                    "네 카테고리 모두 같은 파일이 내려간다. 버킷 설정이 없으면 null",
    )


class ScenicSpotNearbyResponse(BaseModel):
    based_at: datetime = Field(
        ...,
        description="조회 기준 시각(KST, ISO-8601). 예: 2026-07-11T09:00:00+09:00. "
                    "'오전 9:00 기준' 같은 표시 문구는 프론트가 이 값으로 포맷팅",
    )
    feature_count: int = Field(..., description="조회된 관광지 수")
    items: List[ScenicSpotResponse] = Field(..., description="구간에서 보이는 관광지 목록")
