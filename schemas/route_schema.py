from datetime import datetime

from pydantic import BaseModel, Field

from core.enums import Theme


class StopoverPlace(BaseModel):
    """경유역 인근(역 근처) 추천 관광지 1곳 — 경유 체류시간 동안 둘러볼 곳."""

    place_idx: int = Field(..., description="추천지 PK")
    name: str = Field(..., description="이름")
    region: str | None = Field(None, description="지역")
    lat: float = Field(..., description="위도")
    lng: float = Field(..., description="경도")
    themes: list[Theme] = Field(..., description="테마 태그")
    image_url: str | None = Field(None, description="대표 이미지 URL")
    open_time: str | None = Field(None, description="운영 시작 시각 (HH:MM). 미상이면 null")
    close_time: str | None = Field(None, description="운영 종료 시각 (HH:MM). 미상이면 null")
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


class RouteCandidate(BaseModel):
    """추천 경로 후보 1개 (직통 또는 경유, 왕복 기준)."""

    route_type: str = Field(..., description="직통 | 경유")
    path: str = Field(..., description="경로 (예: 서울→대전→부산)")
    via_station_idx: int | None = Field(None, description="경유역 station_idx. 직통이면 null")
    go_trains: list[RouteTrain] = Field(..., description="가는 편 탑승 열차(경유는 2편)")
    stay_minutes: int | None = Field(None, description="경유지 체류시간(분). 직통이면 null")
    back_trains: list[RouteTrain] = Field(..., description="오는 편 탑승 열차")
    total_travel_minutes: int = Field(..., description="총 이동시간(분, 체류 제외)")
    total_fare: int | None = Field(None, description="어른 1인 왕복 총 운임(원, 전 구간 편도 요금 합). 인원수·할인 미반영, 한 구간이라도 요금 없으면 null")
    # route_service는 기차 그래프만 다루므로 이 필드를 비운 채 반환하고,
    # 관광 데이터를 아는 recommend_service(_enrich_stopovers)가 나중에 채운다(레이어 경계).
    stopover_places: list[StopoverPlace] = Field(
        default_factory=list,
        description="경유역 인근(역 근처) 추천 관광지. 사용자가 경유역을 지정한 경유 경로에만 채워지고, "
                    "직통이거나 자동 경유면 빈 목록.",
    )
    note: str | None = Field(None, description="비고(예: 직통 열차 없음)")
