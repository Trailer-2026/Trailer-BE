"""추천 엔진 내부 작업용 dataclass (ORM/Pydantic 비의존, 순수 값 객체)."""

from dataclasses import dataclass, field

from core.enums import Theme


@dataclass
class ScoredPlace:
    """점수화 단계 결과 1건. 클러스터링·라우팅의 기본 단위.

    RecommendedPlace 조립에 필요한 표시 필드(name/region/themes/image)도 함께 들고
    다녀 파이프라인이 DB 재조회 없이 코스를 완성한다.
    """

    place_idx: int
    name: str
    region: str | None
    lat: float
    lng: float
    themes: list[Theme]
    score: float
    image_url: str | None = None
    # 운영시간(오픈/마감·휴무요일) — recommend_service가 detailIntro2로 채워 넣는다.
    # 순수 엔진(scheduling)이 하루 방문 시각을 배정할 때 쓴다. 미상이면 None(=시간 제약 없음).
    content_type_id: int | None = None
    open_hour: float | None = None      # 예: 9.5 = 09:30
    close_hour: float | None = None     # 자정 넘김은 +24(예: 26.0 = 익일 02:00)
    closed_weekdays: tuple[int, ...] = ()  # 매주 쉬는 요일(월0~일6)


@dataclass
class Cluster:
    """Day 클러스터 1개 (k-means 결과)."""

    day_no: int
    members: list[ScoredPlace] = field(default_factory=list)
    centroid: tuple[float, float] = (0.0, 0.0)  # (lat, lng)
