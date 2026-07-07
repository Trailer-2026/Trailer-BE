from pydantic import BaseModel, Field

from core.enums import Theme


class PlaceBase(BaseModel):
    """추천 장소 표시 공통 필드 — RecommendedPlace(코스 방문지)·StopoverPlace(경유역 관광지) 공유.

    두 타입은 preference_score/reason/visit_time만 맥락별로 다르게 재정의하고 나머지는 이 베이스를 상속한다.
    (recommend_schema→route_schema 단방향 의존이라, 순환을 피하려 공통 베이스를 leaf 모듈인 여기 둔다.)
    _stopover_to_place가 둘을 변환하므로 표시 필드는 항상 호환돼야 한다 — 그 계약을 여기서 강제한다.
    """

    place_idx: int = Field(..., description="추천지 PK")
    name: str = Field(..., description="이름")
    region: str | None = Field(None, description="지역")
    lat: float = Field(..., description="위도")
    lng: float = Field(..., description="경도")
    themes: list[Theme] = Field(..., description="테마 태그")
    image_url: str | None = Field(None, description="대표 이미지 URL")
    open_time: str | None = Field(None, description="운영 시작 시각 (HH:MM). 미상이면 null")
    close_time: str | None = Field(None, description="운영 종료 시각 (HH:MM). 미상이면 null")


class ThemePlaceCard(BaseModel):
    """홈 '테마별 여행지' 리스트의 관광지 1곳 — 카드에 보이는 필드만."""

    name: str = Field(..., description="관광지명")
    region: str | None = Field(None, description="지역 시도·시군구. 없으면 null")
    image_url: str | None = Field(None, description="대표 이미지 URL")


class ThemedPlacesResponse(BaseModel):
    """홈 '테마별 여행지' 섹션 — 한 테마의 배너 문구 + 전국 관광지 목록."""

    theme: Theme = Field(..., description="이번에 표시된 테마 키 (서버가 랜덤 선택했을 수 있음)")
    title: str = Field(..., description="배너 문구 (예: '아름다운 숲과 자연의 도시')")
    banner_image_url: str | None = Field(None, description="배너 이미지 (대표 관광지 이미지에서 파생). 없으면 null")
    places: list[ThemePlaceCard] = Field(..., description="이 테마의 전국 관광지 목록")
