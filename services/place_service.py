"""테마별 여행지 서비스 — 홈 화면 '테마별 여행지' 섹션과 그 카드를 누른 상세 화면을 만든다.

관광 데이터는 DB가 아니라 실시간 TourAPI(utils.tour_place)에서 온다. 테마 배너 문구는 서버가
소유하고, '다른 테마' 버튼은 theme 없이 재호출하면 서버가 랜덤 테마를 골라준다.
상세 화면의 '가장 가까운 역'만은 관광 데이터가 아니라 카카오 로컬(utils.kakao_local)에서 온다.
"""
import logging
import math
import random

from core.enums import Theme
from core.exceptions.custom import ExternalServiceException, NotFoundException
from schemas.place_schema import (
    NearbyRestaurant,
    NearestStationInfo,
    PlaceDetailResponse,
    PlaceSearchResponse,
    PlaceSearchResult,
    ThemedPlacesResponse,
    ThemePlaceCard,
)
from utils import kakao_local, tour_place

logger = logging.getLogger(__name__)

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


def search_places(query: str) -> PlaceSearchResponse:
    """카카오 로컬 키워드 검색으로 장소 후보를 만든다(일정 '장소 추가' 검색창용).

    좌표(x=경도, y=위도)가 문자열이라 float 변환하고, 파싱 실패 항목은 건너뛴다.
    카카오 호출/키 실패는 502(ExternalServiceException)로 변환한다.
    """
    try:
        documents = kakao_local.search_keyword(query)
    except Exception as e:
        logger.warning("카카오 장소 검색 실패(query=%s): %s", query, e)
        raise ExternalServiceException("장소 검색에 실패했습니다.") from e

    results = []
    for d in documents:
        try:
            lat, lng = float(d["y"]), float(d["x"])
        except (KeyError, TypeError, ValueError):
            continue
        results.append(PlaceSearchResult(
            name=d.get("place_name") or "",
            address=d.get("road_address_name") or d.get("address_name") or None,
            category=d.get("category_group_name") or None,
            latitude=lat, longitude=lng,
        ))
    return PlaceSearchResponse(places=results)


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
            content_id=p.content_id, name=p.name,
            region=_short_region(p.region), image_url=p.image_url,
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


# ── 여행지 상세 ───────────────────────────────────────────────────────────────

_RESTAURANT_LIMIT = 6   # 상세 '가까운 맛집' 기본 개수(화면은 2×2로 4장, 여유분 포함)
_WALK_M_PER_MIN = 80.0  # 도보 속도 4.8km/h(지도앱 보행 안내가 쓰는 통상 속도)
_WALK_DETOUR = 1.2      # 직선거리 → 실제 보행 경로 보정(횡단보도·블록 우회 감안)
_WALK_MAX_M = 1500      # 걸어갈 만한 거리의 상한(≈23분). 역 검색 반경이자 노출 여부의 기준


def _headline(name: str, themes: list[Theme]) -> str:
    """상세 상단 큰 제목 — '{테마 카피}, {관광지명}'.

    카피는 홈 배너와 같은 _THEME_TITLE을 재사용한다(문구를 서버가 소유한다는 원칙도, 홈에서
    보던 테마 톤이 상세로 이어지는 것도 그대로 유지된다). 테마가 하나도 안 잡히면 이름만.
    """
    if not themes:
        return name
    return f"{_THEME_TITLE[themes[0]]}, {name}"


def _nearest_station(lat: float, lng: float) -> NearestStationInfo | None:
    """걸어갈 수 있는 가장 가까운 역 + 노출 문구. 도보권에 역이 없거나 조회 실패면 None.

    **도보권(_WALK_MAX_M)만 내려준다.** 그 밖의 역은 '안동역에서 14.6km'처럼 걸어갈 수 없는
    거리를 도보 안내 자리에 적는 셈이라 보여줄 값이 못 된다. 카카오에 그 반경을 그대로 넘기므로
    역이 없는 지역(제주·산간)은 응답이 비고, 앱은 이 줄을 숨긴다.

    도보 시간은 직선거리에 우회 보정을 곱해 올린 근사다 — 카카오도 보행자 경로 API는 주지 않고,
    1.5km 안에서는 근사 오차가 몇 분 수준이라 '몇 분 거리인지' 감을 주는 데엔 충분하다.

    부가 정보라 조회가 실패해도 상세는 그대로 내려가야 하므로 예외를 삼키고 None을 준다.
    """
    try:
        station = kakao_local.nearest_station(lat, lng, radius_m=_WALK_MAX_M)
    except Exception as e:
        logger.warning("카카오 최근접 역(%.5f,%.5f) 실패: %s", lat, lng, e)
        return None
    if station is None:
        return None

    walk = max(1, math.ceil(station.distance_m * _WALK_DETOUR / _WALK_M_PER_MIN))
    return NearestStationInfo(
        station_name=station.name,
        line=station.line,
        distance_m=station.distance_m,
        walk_minutes=walk,
        text=f"{station.name}에서 도보 {walk}~{walk + 1}분",
    )


def place_detail(content_id: str, restaurant_limit: int = _RESTAURANT_LIMIT) -> PlaceDetailResponse:
    """여행지 상세 — 지역 소개(사진·소개글·가장 가까운 역)와 가까운 맛집을 한 응답에 담는다.

    한 화면에 다 보이는 정보라 앱이 한 번만 호출하도록 합쳤다. 소개는 TourAPI 상세(본문·사진),
    맛집은 같은 좌표의 위치기반 음식점 조회, 역은 카카오 로컬에서 온다 — DB는 쓰지 않는다.

    소개를 못 받으면 화면이 성립하지 않으므로 404/502로 끊고, **맛집과 역은 부가 정보라 비어도
    상세는 그대로 내려간다**(맛집 조회 실패는 빈 배열, 역은 null).
    """
    try:
        detail = tour_place.fetch_detail(content_id)
    except Exception as e:
        logger.warning("TourAPI 여행지 상세(cid=%s) 실패: %s", content_id, e)
        raise ExternalServiceException("여행지 정보 조회에 실패했습니다.") from e

    if detail is None:
        raise NotFoundException("여행지를 찾을 수 없습니다.")

    foods = tour_place.nearby_restaurants(detail.lat, detail.lng, limit=restaurant_limit)
    return PlaceDetailResponse(
        content_id=detail.content_id,
        name=detail.name,
        headline=_headline(detail.name, detail.themes),
        themes=detail.themes,
        address=detail.address,
        latitude=detail.lat,
        longitude=detail.lng,
        images=detail.images,
        overview=detail.overview,
        tel=detail.tel,
        homepage=detail.homepage,
        nearest_station=_nearest_station(detail.lat, detail.lng),
        restaurants=[
            NearbyRestaurant(
                content_id=f.content_id, name=f.name, category=f.category,
                address=f.address, image_url=f.image_url, distance_m=f.distance_m,
                latitude=f.lat, longitude=f.lng,
            )
            for f in foods
        ],
    )
