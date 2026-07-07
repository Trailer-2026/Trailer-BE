"""테마별 여행지 서비스 — 홈 화면 '테마별 여행지' 섹션 데이터를 만든다.

관광 데이터는 DB가 아니라 실시간 TourAPI(utils.tour_place)에서 온다. 테마 배너 문구는 서버가
소유하고, '다른 테마' 버튼은 theme 없이 재호출하면 서버가 랜덤 테마를 골라준다.
"""
import random

from core.enums import Theme
from schemas.place_schema import ThemedPlacesResponse, ThemePlaceCard
from utils import tour_place

_PLACES_LIMIT = 3  # 섹션에 내려줄 관광지 수 (홈 화면 카드 3개)

# 테마별 배너 문구(서버 소유). 큐레이션 카피라 프론트가 아닌 여기서 관리한다.
_THEME_TITLE = {
    Theme.NATURE: "아름다운 숲과 자연의 도시",
    Theme.OCEAN: "탁 트인 바다로 떠나는 여행",
    Theme.HISTORY: "시간을 거니는 역사 여행",
    Theme.CITY: "설렘 가득한 도시 여행",
    Theme.HEALING: "몸과 마음이 쉬어가는 힐링 여행",
    Theme.FOOD: "미식가를 위한 맛의 여정",
    Theme.CULTURE: "예술과 문화가 흐르는 여행",
    Theme.THEME_PARK: "온종일 즐거운 테마파크",
}


def _short_region(addr: str | None) -> str | None:
    """전체 주소 → '시도 시군구' 2토막 (홈 카드 표기용, 예: '강원특별자치도 강릉시'). 없으면 None."""
    if not addr:
        return None
    return " ".join(addr.split()[:2]) or None


def themed_places(theme: Theme | None = None) -> ThemedPlacesResponse:
    """한 테마의 배너 문구 + 전국 관광지 목록을 조립한다.

    theme 미지정이면 랜덤 테마를 고른다 — '다른 테마' 버튼은 파라미터 없이 재호출하면 매번
    다른(간혹 같을 수 있음) 테마가 나온다. 특정 테마를 고정해 받고 싶으면 theme를 넘긴다.
    """
    theme = theme or random.choice(list(Theme))
    places = tour_place.places_by_theme(theme, limit=_PLACES_LIMIT)
    cards = [
        ThemePlaceCard(
            name=p.name, region=_short_region(p.region), image_url=p.image_url,
        )
        for p in places
    ]
    return ThemedPlacesResponse(
        theme=theme,
        title=_THEME_TITLE[theme],
        # ponytail: 배너는 대표 관광지(첫 카드) 이미지로 대체. 큐레이션 배너 생기면 테마별 상수로 교체.
        banner_image_url=cards[0].image_url if cards else None,
        places=cards,
    )
