from pydantic import BaseModel, Field

from core.enums import Theme


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
