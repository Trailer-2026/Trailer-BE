from datetime import datetime

from pydantic import BaseModel, Field


class RouteTrain(BaseModel):
    """경로에 포함된 탑승 열차 1편."""

    train_no: str = Field(..., description="열차 번호")
    grade: str = Field(..., description="열차 등급 (KTX, ITX-새마을, 무궁화호 등)")
    dep_station: str = Field(..., description="출발역명")
    arr_station: str = Field(..., description="도착역명")
    dep_time: datetime = Field(..., description="출발 일시")
    arr_time: datetime = Field(..., description="도착 일시")
    duration_minutes: int = Field(..., description="소요시간(분)")
    fare: int | None = Field(None, description="어른 운임(원). 미제공 시 null")


class RouteCandidate(BaseModel):
    """추천 경로 후보 1개 (직통 또는 경유, 왕복 기준)."""

    route_type: str = Field(..., description="직통 | 경유")
    path: str = Field(..., description="경로 (예: 서울→대전→부산)")
    go_trains: list[RouteTrain] = Field(..., description="가는 편 탑승 열차(경유는 2편)")
    stay_minutes: int | None = Field(None, description="경유지 체류시간(분). 직통이면 null")
    back_trains: list[RouteTrain] = Field(..., description="오는 편 탑승 열차")
    total_travel_minutes: int = Field(..., description="총 이동시간(분, 체류 제외)")
    total_fare: int | None = Field(None, description="총 운임(원). 일부 미제공 시 null")
    note: str | None = Field(None, description="비고(예: 직통 열차 없음)")
