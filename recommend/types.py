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


@dataclass
class Cluster:
    """Day 클러스터 1개 (k-means 결과)."""

    day_no: int
    members: list[ScoredPlace] = field(default_factory=list)
    centroid: tuple[float, float] = (0.0, 0.0)  # (lat, lng)


@dataclass
class Tour:
    """Day 내부 방문 순서 (Nearest Neighbor + 2-opt 결과)."""

    order: list[ScoredPlace] = field(default_factory=list)
    # 열린 경로 총 거리(km). 코스 소요시간이 아니라 순서 최적화·거리 판단용(체류/이동시간 휴리스틱은 제거됨).
    length_km: float = 0.0
