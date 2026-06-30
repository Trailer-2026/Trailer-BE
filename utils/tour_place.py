"""TourAPI 실시간 호출로 추천지·숙소 후보를 만든다(런타임 데이터 소스).

공모전 정책상 관광데이터는 DB 스냅샷이 아니라 매 요청 실시간 호출로 받아야 한다.
역(station)은 관광데이터가 아니므로 DB를 그대로 쓴다.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from core.enums import Theme
from schemas.recommend_schema import Lodging
from utils import tour_api, tour_category

logger = logging.getLogger(__name__)

# 테마 → 조회할 contentTypeId 후보 (관광지12·문화14·축제15·쇼핑38·음식39)
_THEME_CTYPES = {
    Theme.NATURE: (12,), Theme.OCEAN: (12,), Theme.HISTORY: (12,),
    Theme.HEALING: (12,), Theme.THEME_PARK: (12,),
    Theme.CITY: (12, 38), Theme.CULTURE: (14, 15), Theme.FOOD: (39,),
}
_DEFAULT_CTYPES = (12, 14, 39)   # 테마 미선택 시 기본 조회
_LODGING_CT = 32
_RADIUS_M = 20000                # locationBasedList2 최대 반경(20km)
_AREA_CODES = [1, 2, 3, 4, 5, 6, 7, 8, 31, 32, 33, 34, 35, 36, 37, 38, 39]  # 시도


@dataclass
class LivePlace:
    """실시간 조회한 추천지. scoring/pipeline이 기대하는 Place 속성과 동일 모양."""

    place_idx: int          # TourAPI contentid를 정수로
    name: str
    region: str | None
    lat: float
    lng: float
    themes: list[Theme]
    avg_stay_min: int
    image_url: str | None
    content_id: str


def _ctypes_for(themes: list[Theme]) -> set[int]:
    if not themes:
        return set(_DEFAULT_CTYPES)
    out: set[int] = set()
    for t in themes:
        out.update(_THEME_CTYPES.get(t, ()))
    return out


def _to_live(item: dict) -> LivePlace | None:
    try:
        lng = float(item.get("mapx") or 0)
        lat = float(item.get("mapy") or 0)
    except (TypeError, ValueError):
        return None
    if not lat or not lng:
        return None
    ct = item.get("contenttypeid")
    themes = tour_category.themes_for(ct, item.get("cat1"), item.get("cat2"), item.get("cat3"))
    if not themes:
        return None
    cid = str(item.get("contentid") or "")
    if not cid.isdigit():
        return None
    return LivePlace(
        place_idx=int(cid),
        name=(item.get("title") or "")[:255],
        region=(item.get("addr1") or None),
        lat=lat,
        lng=lng,
        themes=themes,
        avg_stay_min=tour_category.stay_minutes(ct, themes),
        image_url=(item.get("firstimage") or None),
        content_id=cid,
    )


def _location_items(lat, lng, radius_m, ct, per_type) -> list[dict]:
    try:
        items, _ = tour_api.location_based_list(
            lat=lat, lng=lng, radius_m=radius_m, content_type_id=ct,
            num_of_rows=per_type, arrange="E",
        )
        return items
    except Exception as e:
        logger.warning("TourAPI 위치기반(ct=%s) 실패: %s", ct, e)
        return []


def live_places(lat: float, lng: float, themes: list[Theme], radius_m: int = _RADIUS_M,
                per_type: int = 100) -> list[LivePlace]:
    """좌표 반경 내 추천지를 실시간 조회(콘텐츠 유형별 병렬). 선택 테마와 겹치는 것만, 중복 제거."""
    selected = set(themes or [])
    ctypes = _ctypes_for(themes)
    out: dict[str, LivePlace] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(ctypes))) as ex:
        batches = ex.map(lambda ct: _location_items(lat, lng, radius_m, ct, per_type), ctypes)
    for items in batches:
        for it in items:
            lp = _to_live(it)
            if not lp:
                continue
            if selected and not (set(lp.themes) & selected):
                continue
            out[lp.content_id] = lp
    return list(out.values())


def nearest_lodging(lat: float, lng: float, radius_m: int = _RADIUS_M) -> Lodging | None:
    """좌표에서 가장 가까운 숙소 1곳을 실시간 조회한다(거리순 1건)."""
    try:
        items, _ = tour_api.location_based_list(
            lat=lat, lng=lng, radius_m=radius_m, content_type_id=_LODGING_CT,
            num_of_rows=1, arrange="E",
        )
    except Exception as e:
        logger.warning("TourAPI 숙박 위치기반 실패: %s", e)
        return None
    if not items:
        return None
    it = items[0]
    try:
        lng2 = float(it.get("mapx") or 0)
        lat2 = float(it.get("mapy") or 0)
    except (TypeError, ValueError):
        return None
    if not lat2 or not lng2:
        return None
    return Lodging(
        name=(it.get("title") or "")[:255],
        lodging_type=tour_category.LODGING_TYPE.get(it.get("cat3") or ""),
        region=(it.get("addr1") or None),
        lat=lat2, lng=lng2,
        tel=(it.get("tel") or None),
        image_url=(it.get("firstimage") or None),
    )


def _scan_area(area: int, primary: int, selected: set) -> tuple[int, tuple[float, float]] | None:
    try:
        items, _ = tour_api.area_based_list(
            area_code=area, content_type_id=primary, num_of_rows=50, arrange="O",
        )
    except Exception:
        return None
    pts = []
    for it in items:
        lp = _to_live(it)
        if not lp:
            continue
        if selected and not (set(lp.themes) & selected):
            continue
        pts.append((lp.lat, lp.lng))
    if not pts:
        return None
    return len(pts), (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def pick_dense_anchor(themes: list[Theme]) -> tuple[float, float] | None:
    """도착지 미지정 시 AI 자동 선택 — 시도별 실시간 조회(병렬)로 테마 후보가 가장 많은 지역 중심."""
    primary = sorted(_ctypes_for(themes))[0]  # 대표 콘텐츠 유형 1개로 지역 스캔
    selected = set(themes or [])
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda a: _scan_area(a, primary, selected), _AREA_CODES))
    best, best_n = None, -1
    for r in results:
        if r and r[0] > best_n:
            best_n, best = r[0], r[1]
    return best
