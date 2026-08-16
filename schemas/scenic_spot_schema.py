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


class ScenicPlanRide(BaseModel):
    """시각표가 붙은 탑승 1건. 승차권 두 갈래 중 어디서 왔는지에 따라 채워지는 idx가 다르다."""

    travel_idx: Optional[int] = Field(
        None, description="FK 여행 — 추천 코스 승차권일 때만. 직접 입력 승차권은 여행에 묶이지 않아 null"
    )
    schedule_idx: Optional[int] = Field(
        None, description="FK 일정 — 추천 코스 승차권(schedule kind=train)에서 온 탑승"
    )
    ticket_idx: Optional[int] = Field(
        None, description="FK 승차권 — 직접 입력 승차권에서 온 탑승"
    )
    dep_station: str = Field(..., description="출발역명('역' 포함)", example="서울역")
    arr_station: str = Field(..., description="도착역명('역' 포함)", example="부산역")
    dep_at: datetime = Field(..., description="출발 일시(KST wall-clock)", example="2026-08-16T09:00:00")
    arr_at: datetime = Field(..., description="도착 일시(KST wall-clock)", example="2026-08-16T11:40:00")


class ScenicPlanItem(BaseModel):
    """지나갈 풍경 구간 1개 — 통과 예정 시각과 그 구간의 대표 관광지."""

    scenic_spot_idx: int = Field(..., description="대표 관광지 PK. 푸시 딥링크와 같은 값")
    name: Optional[str] = Field(None, description="대표 관광지 이름")
    category: str = Field(..., description="분류 (water | waterway | peak | natural_view)")
    side: Optional[str] = Field(None, description="진행 방향 기준 창밖 좌우 (left | right)")
    from_station: str = Field(..., description="구간 시작역('역' 포함)", example="오송역")
    to_station: str = Field(
        ..., description="구간 도착역('역' 포함). 알림 문구의 '지금 OO역 스팟을 지나고 있어요'",
        example="대전역",
    )
    eta: datetime = Field(
        ...,
        description="통과 예정 시각(KST wall-clock). 역 간 직선거리에 비례해 소요 시간을 나눈 "
                    "추정값이라 몇 분 오차가 있다. GPS 보정을 하면 그만큼 밀린 값이 내려간다",
        example="2026-08-16T09:51:00",
    )
    image_url: Optional[str] = Field(
        None, description="카테고리에 맞는 풍경 일러스트 URL(공개). 푸시 배너와 같은 그림. "
                          "버킷 설정이 없으면 null"
    )
    is_sent: bool = Field(
        ..., description="서버가 이 구간의 푸시를 이미 보냈는지. true면 앱이 다시 안내할 필요가 없다"
    )


class ScenicPlanResponse(BaseModel):
    based_at: datetime = Field(
        ..., description="응답 생성 시각(KST, ISO-8601)", example="2026-08-16T09:30:00+09:00"
    )
    ride: Optional[ScenicPlanRide] = Field(
        None, description="시각표의 대상 탑승. 타고 있는 열차가 없고 곧 출발할 열차도 없으면 null"
    )
    delay_minutes: int = Field(
        ...,
        description="마지막 GPS 보정으로 확인된 지연(분). 양수면 예정보다 늦게 가고 있다는 뜻. "
                    "보정한 적이 없으면 0",
    )
    items: List[ScenicPlanItem] = Field(
        ..., description="통과 예정 시각순 풍경 구간 목록. 대상 탑승이 없으면 빈 배열"
    )


class ScenicPlanCalibrateRequest(BaseModel):
    lat: float = Field(..., description="현재 위도", example=36.59683)
    lng: float = Field(..., description="현재 경도", example=127.33874)
