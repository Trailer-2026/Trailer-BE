from datetime import datetime

from pydantic import BaseModel, Field

from schemas.place_schema import PlaceBase


class StopoverPlace(PlaceBase):
    """경유역 인근(역 근처) 추천 관광지 1곳 — 경유 체류시간 동안 둘러볼 곳.

    표시 공통 필드는 PlaceBase 상속. 경유 맥락에 맞춰 아래 3개만 기본값·설명을 달리 정의한다.
    """

    preference_score: float = Field(
        0.0,
        description="선택 테마와의 선호도(0~1). 목적지 코스와 같은 점수식(테마 가중 코사인 + 이미지 품질)으로 산정",
    )
    reason: str = Field("경유역 근처 추천지", description="추천 이유 (목적지 코스와 같은 형식)")
    visit_time: str | None = Field(
        None,
        description="경유 체류시간 내 예상 방문 시각 (HH:MM). 운영시간을 반영해 배정. "
                    "체류시간 동안 문 여는 시간이 없으면 null",
    )


class RouteTrain(BaseModel):
    """경로에 포함된 탑승 열차 1편."""

    train_no: str = Field(..., description="열차 번호")
    grade: str = Field(..., description="열차 등급 (KTX, ITX-새마을, 무궁화호 등)")
    dep_station: str = Field(..., description="출발역명")
    arr_station: str = Field(..., description="도착역명")
    dep_time: datetime = Field(..., description="출발 일시")
    arr_time: datetime = Field(..., description="도착 일시")
    duration_minutes: int = Field(..., description="소요시간(분)")
    fare: int | None = Field(None, description="어른 1인 편도 운임(원, TAGO API adultcharge). 좌석등급·할인 미반영, 미제공 시 null")
    stop_station_count: int | None = Field(
        None,
        description="이 열차가 탑승구간(출발~도착)에서 정차하는 역 수(출발·도착역 포함). "
                    "정차역 데이터 없으면 null(예: SRT, 임시열차)",
    )
    stop_stations: list[str] | None = Field(
        None,
        description="탑승구간 정차역명 순서(출발~도착 포함). 데이터 없으면 null",
    )


class RouteCandidate(BaseModel):
    """추천 경로 후보 1개 (직통 또는 경유, 왕복 기준)."""

    route_type: str = Field(..., description="직통 | 경유")
    path: str = Field(..., description="경로 (예: 서울→대전→부산)")
    via_station_idx: int | None = Field(None, description="경유역 station_idx. 직통이면 null")
    via_nights: int = Field(0, description="경유역에서 묵는 박 수. 0=당일치기 경유(같은 날 2~6h 체류), 1+=경유역 숙박 후 다음날 이동")
    go_trains: list[RouteTrain] = Field(..., description="가는 편 탑승 열차(경유는 2편)")
    stay_minutes: int | None = Field(None, description="경유지 체류시간(분). 직통이면 null")
    back_trains: list[RouteTrain] = Field(..., description="오는 편 탑승 열차")
    total_travel_minutes: int = Field(..., description="총 이동시간(분, 체류 제외)")
    total_fare: int | None = Field(None, description="어른 1인 왕복 총 운임(원, 전 구간 편도 요금 합). 인원수·할인 미반영, 한 구간이라도 요금 없으면 null")
    # route_service는 기차 그래프만 다루므로 이 필드를 비운 채 반환하고,
    # 관광 데이터를 아는 recommend_service(_enrich_stopovers)가 나중에 채운다(레이어 경계).
    stopover_places: list[StopoverPlace] = Field(
        default_factory=list,
        description="경유역 인근(역 근처) 추천 관광지. 당일치기 경유(via_nights=0) 경로에 채워지고"
                    "(지정·자동 경유 공통), 직통이거나 숙박 경유(관광이 코스 날에 편입)면 빈 목록.",
    )
    note: str | None = Field(None, description="비고(예: 직통 열차 없음)")
