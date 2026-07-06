import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from core.enums import Theme
# recommend_schema → route_schema 단방향 의존. 그래서 경유 관광지 타입(StopoverPlace)은
# RecommendedPlace를 재사용하지 못하고(역방향=순환) route_schema에 독립 정의돼 있다.
from schemas.route_schema import RouteTrain

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
    via_station_idx: int | None = Field(
        None,
        description="경유역 station_idx. 지정 시 가는편 기차가 이 역을 2~6시간 관광 체류로 경유하는 "
                    "경로만 제공한다(출발·도착역과 같으면 무시). 0 또는 미지정이면 경유 없음"
                    "(도착역만 지정한 자동 경유 추천으로 동작). 선택",
    )
    use_naeilpass: bool = Field(False, description="내일로 패스 사용 여부")
    page: int = Field(
        0, ge=0,
        description="추천 다시받기 페이지(0=최초). 1씩 올리면 이전과 겹치지 않는 다음 플랜 묶음을 "
                    "반환한다. 관광지가 부족한 목적지는 페이지가 커지면 앞 결과와 겹치거나 반복될 수 있다.",
    )

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

    @field_validator("via_station_idx")
    @classmethod
    def _norm_via(cls, v: int | None) -> int | None:
        """0 이하(Swagger 기본값 0 포함)는 '경유 미지정'으로 정규화."""
        return v if v and v > 0 else None


class RecommendedPlace(BaseModel):
    """추천 코스에 포함된 방문지 1곳 (코스 상세의 추천지 리스트)."""

    place_idx: int = Field(..., description="추천지 PK")
    name: str = Field(..., description="이름")
    region: str | None = Field(None, description="지역")
    lat: float = Field(..., description="위도")
    lng: float = Field(..., description="경도")
    themes: list[Theme] = Field(..., description="테마 태그")
    preference_score: float = Field(..., description="선호도 점수(가중 코사인 유사도)")
    reason: str = Field(..., description="추천 이유 한 줄 설명")
    image_url: str | None = Field(None, description="대표 이미지 URL")
    open_time: str | None = Field(None, description="운영 시작 시각 (HH:MM). 미상이면 null")
    close_time: str | None = Field(None, description="운영 종료 시각 (HH:MM). 미상이면 null")
    visit_time: str | None = Field(
        None, description="예상 방문 시각 (HH:MM). 운영시간을 반영해 배정된 방문 순서상의 시각"
    )


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
    lodging: Lodging | None = Field(
        None,
        description="그 날 밤 숙소. 지역 이동이 없으면 전날과 동일 숙소가 반복되고, "
                    "마지막 날(귀가일)은 null.",
    )


class Course(BaseModel):
    """추천 코스 후보 1개 (출발 → N박N일 → 출발지 순환).

    엔진(pipeline) 내부 산출물 — 응답에는 직접 노출되지 않고 Itinerary로 병합된다.
    """

    label: str = Field(..., description="코스 라벨 (A/B/C…)")
    origin_station_idx: int = Field(..., description="출발·복귀 역 station_idx")
    days: list[DayPlan] = Field(..., description="Day별 일정")
    total_preference_score: float = Field(..., description="코스 전체 선호도 점수 합")
    is_round_trip_closed: bool = Field(..., description="마지막 day에서 출발지로 복귀(순환 닫힘) 여부")
    note: str | None = Field(None, description="비고")


class ItinerarySegment(BaseModel):
    """여정의 시간순 세그먼트 1개. kind로 종류를 구분하고 해당 필드만 채운다.

    - kind="train": train(탑승 열차)
    - kind="visit": place(방문 관광지 — 경유역·목적지 공통)
    - kind="lodging": lodging(그 날 밤 숙소)
    """

    kind: str = Field(..., description="세그먼트 종류: train | visit | lodging")
    day_no: int = Field(..., description="여행 일자 (1=Day1)")
    start_time: datetime | None = Field(None, description="시작 일시(열차 출발/방문 시작). 미상이면 null")
    end_time: datetime | None = Field(None, description="종료 일시(열차 도착/방문 종료). 미상이면 null")
    train: RouteTrain | None = Field(None, description="kind=train일 때 탑승 열차")
    place: RecommendedPlace | None = Field(None, description="kind=visit일 때 방문 관광지")
    lodging: Lodging | None = Field(None, description="kind=lodging일 때 숙소")


class Itinerary(BaseModel):
    """통합 여정 1개 = 특정 기차 경로 + 대표 코스를 시간순으로 엮은 것.

    가는 기차 → (경유 관광) → 목적지 관광·숙소 → 오는 기차가 하나의 segments 타임라인.
    경로별로 하나씩 제공(직통/경유A/경유B…). 기차 없는 현지 여행이면 route_type="현지".
    """

    plan_id: str | None = Field(
        None, description="이 플랜을 저장(POST /api/travels)할 때 쓰는 id. 서버 캐시 키(TTL)."
    )
    plan_label: str | None = Field(None, description="플랜 슬롯 라벨 (A/B/C…). 카드 '플랜 A' 칩용")
    title: str | None = Field(
        None, description="플랜 카드 제목 (대표 명소 기준, 예: '부산 중앙공원 코스'). 방문지가 없으면 null"
    )
    label: str = Field(..., description="여정 라벨 (경로 표기, 예: 서울→대전→부산)")
    route_type: str = Field(..., description="직통 | 경유 | 현지")
    via_station_idx: int | None = Field(None, description="경유역 station_idx. 직통/현지면 null")
    main_themes: list[Theme] = Field(
        default_factory=list,
        description="이 여정의 대표 테마(방문지 테마 최빈 상위 2개). 플랜 카드 '메인 테마' 표기용. 방문지가 없으면 빈 목록",
    )
    cover_image_url: str | None = Field(
        None, description="플랜 카드 대표 이미지(선호도 최고 방문지 이미지). 이미지 있는 방문지가 없으면 null"
    )
    segments: list[ItinerarySegment] = Field(..., description="시간순 세그먼트(기차·방문·숙소 통합)")
    total_preference_score: float = Field(..., description="코스 전체 선호도 점수 합")
    total_travel_minutes: int = Field(..., description="총 기차 이동시간(분, 체류 제외)")
    total_fare: int | None = Field(None, description="어른 1인 왕복 총 운임(원). 한 구간이라도 미제공이면 null")
    is_round_trip_closed: bool = Field(..., description="마지막 day에서 출발지로 복귀(순환 닫힘) 여부")
    note: str | None = Field(None, description="비고(예: 직통 열차 없음, 기차 경로 조회 실패)")


class DestinationPlan(BaseModel):
    """도착지 1곳 + 그 도착지로 가는 통합 여정 목록.

    itineraries에 경로별 여정(직통/경유…)이 담긴다. 각 여정은 기차+관광+숙소를 하나의
    시간순 타임라인으로 통합한다(도착역 미지정 자동 선택 시 score도 함께).
    """

    destination_station_idx: int = Field(..., description="도착역 station_idx")
    destination_name: str = Field(..., description="도착역명")
    score: float | None = Field(None, description="AI 자동 선택 시 도착지 종합 점수(지정 시 null)")
    itineraries: list[Itinerary] = Field(..., description="경로별 통합 여정(기차+관광+숙소)")
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
