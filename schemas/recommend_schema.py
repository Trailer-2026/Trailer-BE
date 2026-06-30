import re

from pydantic import BaseModel, Field, field_validator

from core.enums import Theme
from schemas.route_schema import RouteCandidate

# 스웨거 표시용 테마 한글 라벨 (사진 명세 기준)
_THEME_LABELS_FULL = {
    "NATURE": "산/자연",
    "OCEAN": "바다/해안",
    "HISTORY": "역사/유적",
    "CITY": "도시",
    "HEALING": "힐링/온천",
    "FOOD": "맛집/미식",
    "CULTURE": "문화/예술",
    "THEME_PARK": "테마파크",
}
_THEMES_DESC = "선택한 테마 태그(다중 선택 가능) — " + ", ".join(
    f"{t.value}({_THEME_LABELS_FULL[t.value]})" for t in Theme
)


class Party(BaseModel):
    """여행 인원 카운터."""

    adult: int = Field(0, ge=0, description="성인 수")
    youth: int = Field(0, ge=0, description="청소년 수")
    child: int = Field(0, ge=0, description="어린이 수")


class SearchCriteria(BaseModel):
    """AI 코스 추천 요청 조건 (승차권·인원·테마·추가조건 통합)."""

    origin_station_idx: int = Field(..., description="출발역 station_idx (기차 구간 시작·복귀)")
    dest_station_idx: int | None = Field(
        None,
        description="도착역 station_idx. 지정 시 그 지역에 코스를 앵커. 미지정(null)이면 AI가 "
                    "테마 고득점 밀집 지역을 자동 선택. N박N일 순환의 현지 기준점이 된다.",
    )
    round_trip: bool = Field(True, description="왕복 여부")
    go_date: str = Field(..., description="가는날 (YYYYMMDD)")
    go_time: str | None = Field(None, description="가는날 출발 희망 시각 (HH:MM)")
    back_date: str = Field(..., description="오는날 (YYYYMMDD)")
    back_time: str | None = Field(None, description="오는날 출발 희망 시각 (HH:MM)")
    party: Party = Field(default_factory=Party, description="여행 인원")
    themes: list[Theme] = Field(default_factory=list, description=_THEMES_DESC)
    max_travel_minutes: int | None = Field(None, description="최대 이동시간 예산(분). 선택")
    waypoint_place_idxs: list[int] | None = Field(None, description="경유지(필수 방문 place_idx). 선택")
    use_naeilpass: bool = Field(False, description="내일로 패스 사용 여부")

    @field_validator("go_date", "back_date")
    @classmethod
    def _norm_date(cls, v: str) -> str:
        """YYYYMMDD / YYYY-MM-DD 등 구분자 섞인 날짜를 YYYYMMDD로 정규화."""
        digits = re.sub(r"\D", "", v or "")
        if len(digits) != 8:
            raise ValueError("날짜는 YYYYMMDD 또는 YYYY-MM-DD 형식이어야 합니다.")
        return digits

    @field_validator("go_time", "back_time")
    @classmethod
    def _norm_time(cls, v: str | None) -> str | None:
        """HH:MM 형식이 아니면 None 처리(예: Swagger 기본값 'string')."""
        return v if v and re.match(r"^\d{1,2}:\d{2}$", v) else None


class RecommendedPlace(BaseModel):
    """추천 코스에 포함된 방문지 1곳 (코스 상세의 추천지 리스트)."""

    place_idx: int = Field(..., description="추천지 PK")
    name: str = Field(..., description="이름")
    region: str | None = Field(None, description="지역")
    lat: float = Field(..., description="위도")
    lng: float = Field(..., description="경도")
    themes: list[Theme] = Field(..., description="테마 태그")
    avg_stay_min: int = Field(..., description="평균 체류시간(분)")
    preference_score: float = Field(..., description="선호도 점수(가중 코사인 유사도)")
    reason: str = Field(..., description="추천 이유 한 줄 설명")


class Lodging(BaseModel):
    """숙소 (TourAPI 숙박)."""

    name: str = Field(..., description="숙소명")
    lodging_type: str | None = Field(None, description="유형(관광호텔/펜션/게스트하우스 등)")
    region: str | None = Field(None, description="주소")
    lat: float = Field(..., description="위도")
    lng: float = Field(..., description="경도")
    tel: str | None = Field(None, description="전화번호")
    image_url: str | None = Field(None, description="대표 이미지 URL")


class DayPlan(BaseModel):
    """하루 일정 (방문 순서대로 정렬된 추천지 + 그 날 밤 숙소)."""

    day_no: int = Field(..., description="여행 일자 (1=Day1)")
    date: str | None = Field(None, description="배정 날짜 (YYYYMMDD). 날짜 지정 전 null")
    places: list[RecommendedPlace] = Field(..., description="방문 순서대로의 추천지 목록")
    est_travel_minutes: int = Field(..., description="Day 내부 예상 이동시간(분)")
    lodging: Lodging | None = Field(
        None,
        description="그 날 밤 숙소. 지역 이동이 없으면 전날과 동일 숙소가 반복되고, "
                    "마지막 날(귀가일)은 null.",
    )


class Course(BaseModel):
    """추천 코스 후보 1개 (출발 → N박N일 → 출발지 순환)."""

    label: str = Field(..., description="코스 라벨 (A/B/C…)")
    origin_station_idx: int = Field(..., description="출발·복귀 역 station_idx")
    days: list[DayPlan] = Field(..., description="Day별 일정")
    total_preference_score: float = Field(..., description="코스 전체 선호도 점수 합")
    total_travel_minutes: int = Field(..., description="코스 전체 예상 이동시간(분)")
    is_round_trip_closed: bool = Field(..., description="마지막 day에서 출발지로 복귀(순환 닫힘) 여부")
    note: str | None = Field(None, description="비고")


class DestinationPlan(BaseModel):
    """도착지 1곳 + 출발↔도착지 왕복 기차 경로 + 코스 묶음.

    - 도착역 지정 시: courses에 코스 후보 A/B/C가 모두 담긴다.
    - 도착역 미지정(AI 자동) 시: 후보 도착지마다 코스 1개 + score(종합 점수).
    """

    destination_station_idx: int = Field(..., description="도착역 station_idx")
    destination_name: str = Field(..., description="도착역명")
    score: float | None = Field(None, description="AI 자동 선택 시 도착지 종합 점수(지정 시 null)")
    routes: list[RouteCandidate] = Field(..., description="출발↔이 도착지 왕복 기차 경로 후보")
    courses: list[Course] = Field(..., description="이 도착지 기준 코스(지정=A/B/C, 자동=1개)")
    note: str | None = Field(None, description="비고(예: 기차 경로 조회 실패, 현지 여행)")


class RecommendResponse(BaseModel):
    """AI 코스 추천 응답 — 도착지별 경로·코스 묶음 목록.

    - auto_selected=false(도착역 지정): destinations 길이 1, 그 안에 코스 A/B/C.
    - auto_selected=true(도착역 미지정): theme+party 기준으로 고른 서로 다른 권역의
      도착지 후보 최대 3곳, 각 코스 1개.
    """

    auto_selected: bool = Field(..., description="도착지를 AI가 자동 선택했는지 여부")
    destinations: list[DestinationPlan] = Field(..., description="도착지별 경로·코스 묶음")
    note: str | None = Field(None, description="비고(전체 수준 안내)")
