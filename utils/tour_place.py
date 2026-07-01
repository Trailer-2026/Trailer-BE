"""TourAPI 실시간 호출로 추천지·숙소 후보를 만든다(런타임 데이터 소스).

공모전 정책상 관광데이터는 DB 스냅샷이 아니라 매 요청 실시간 호출로 받아야 한다.
역(station)은 관광데이터가 아니므로 DB를 그대로 쓴다.
"""
import logging
import math
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

# 한 시도가 이 정도(대각 퍼짐, km) 이상이면 부분권으로 분리한다.
# 큰 도(경북·강원 등)의 해안권(포항·강릉)이 내륙 중심에 묻혀 도착역 후보에서 빠지는 것을 막는다.
_SUBCLUSTER_KM = 60.0
_MAX_SUBCLUSTERS = 3


@dataclass
class LivePlace:
    """실시간 조회한 추천지. scoring/pipeline이 기대하는 Place 속성과 동일 모양."""

    place_idx: int          # TourAPI contentid를 정수로
    name: str
    region: str | None
    lat: float
    lng: float
    themes: list[Theme]
    image_url: str | None
    content_id: str


def _ctypes_for(themes: list[Theme]) -> set[int]:
    """선택 테마들이 필요로 하는 contentTypeId 집합(테마 미선택이면 기본 3종).

    여러 테마의 유형이 겹치므로 set으로 중복 호출을 없앤다(예: NATURE·OCEAN 모두 12).
    """
    if not themes:
        return set(_DEFAULT_CTYPES)
    out: set[int] = set()
    for t in themes:
        out.update(_THEME_CTYPES.get(t, ()))
    return out


def _to_live(item: dict) -> LivePlace | None:
    """TourAPI 응답 항목 1건 → LivePlace. 좌표·테마·contentid 중 하나라도 없으면 None(스킵).

    좌표는 TourAPI 규약대로 mapx=경도, mapy=위도로 뒤집어 읽는다(주의: x/y와 lat/lng 반대).
    """
    try:
        lng = float(item.get("mapx") or 0)
        lat = float(item.get("mapy") or 0)
    except (TypeError, ValueError):
        return None
    if not lat or not lng:
        return None
    ct = item.get("contenttypeid")
    themes = tour_category.themes_for(ct, item.get("cat1"), item.get("cat2"), item.get("cat3"))
    if not themes:  # 8개 테마 어디에도 안 걸리는 항목(레포츠 등)은 버린다
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
    """좌표 반경 내 추천지를 실시간 조회(콘텐츠 유형별 병렬). 선택 테마와 겹치는 것만, 중복 제거.

    유형(contentType)마다 별도 API 호출이라 스레드로 병렬 처리한다. 한 항목이 여러 유형
    조회에 중복 등장할 수 있어 content_id를 키로 dict에 담아 마지막 값으로 dedup한다.
    """
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
            # 선택 테마가 있으면 교집합이 없는 항목은 제외(예: FOOD만 골랐는데 딸려온 자연관광지).
            # 테마 미선택이면 selected가 비어 이 필터를 건너뛴다.
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


@dataclass
class AreaScan:
    """시도 area 1곳의 라이브 스캔 결과 — 도착지 후보 점수화(recommend.destination) 입력."""

    area_code: int
    centroid: tuple[float, float]
    theme_counts: dict[Theme, int]   # 테마별 장소 수(themeVector)
    total: int                       # 좌표 보유 장소 총수


def _area_items(area: int, ct: int) -> list[dict]:
    try:
        items, _ = tour_api.area_based_list(
            area_code=area, content_type_id=ct, num_of_rows=50, arrange="O",
        )
        return items
    except Exception as e:
        logger.warning("TourAPI 지역기반(area=%s, ct=%s) 실패: %s", area, ct, e)
        return []


def scan_area_profiles(themes: list[Theme]) -> list[AreaScan]:
    """도착지 미지정 시 AI 자동 선택용 — 시도별 테마분포를 실시간 스캔(병렬).

    선택 테마(없으면 기본)의 contentType들로 각 area를 조회한다. 큰 도는 한 중심으로 뭉치면
    해안권(포항·강릉)이 내륙 평균에 묻히므로, 점들을 지리적으로 부분권(해안/내륙 등)으로
    분리해 **각 부분권을 별도 후보**로 낸다. recommend.destination이 이 분포로 도착지를 점수화한다.
    """
    ctypes = sorted(_ctypes_for(themes))
    jobs = [(area, ct) for area in _AREA_CODES for ct in ctypes]
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda j: _area_items(j[0], j[1]), jobs))

    # area별로 (위도, 경도, 테마들) 점을 모은다.
    agg: dict[int, list] = {}
    for (area, _ct), items in zip(jobs, results):
        pts = agg.setdefault(area, [])
        for it in items:
            lp = _to_live(it)
            if not lp:
                continue
            pts.append((lp.lat, lp.lng, tuple(lp.themes)))

    profiles: list[AreaScan] = []
    for area, pts in agg.items():
        if not pts:
            continue
        for cluster in _split_clusters(pts):   # 부분권 분리(퍼짐 작으면 1개 그대로)
            if not cluster:
                continue
            centroid = (
                sum(p[0] for p in cluster) / len(cluster),
                sum(p[1] for p in cluster) / len(cluster),
            )
            counts: dict[Theme, int] = {}
            for _, _, themes_t in cluster:
                for t in themes_t:
                    counts[t] = counts.get(t, 0) + 1
            profiles.append(AreaScan(area, centroid, counts, len(cluster)))
    return profiles


def _split_clusters(pts: list) -> list[list]:
    """시도 내 점들을 지리적 부분권으로 분리. 퍼짐이 작으면 1개 그대로."""
    k = _cluster_k(pts)
    if k <= 1:
        return [pts]
    return [g for g in _kmeans_geo(pts, k) if g]


def _cluster_k(pts: list) -> int:
    """점들의 대각 퍼짐(km)을 보고 부분권 수를 정한다(작으면 1, 넓으면 최대 _MAX_SUBCLUSTERS)."""
    lats = [p[0] for p in pts]
    lngs = [p[1] for p in pts]
    # 위경도 span을 km로 환산: 위도 1°≈111km, 경도 1°≈88km(한국 위도 ~36°의 cos 보정값).
    diag_km = math.hypot((max(lats) - min(lats)) * 111.0, (max(lngs) - min(lngs)) * 88.0)
    return max(1, min(_MAX_SUBCLUSTERS, round(diag_km / _SUBCLUSTER_KM)))


def _kmeans_geo(pts: list, k: int, iters: int = 12) -> list[list]:
    """결정적 k-means(위경도). 정렬 시드라 같은 입력 → 같은 분할(비결정 요소 없음)."""
    ordered = sorted(pts)
    centers = [ordered[i * len(ordered) // k][:2] for i in range(k)]
    groups: list[list] = [[] for _ in range(k)]
    for _ in range(iters):
        groups = [[] for _ in range(k)]
        for p in pts:
            j = min(range(k), key=lambda c: (p[0] - centers[c][0]) ** 2 + (p[1] - centers[c][1]) ** 2)
            groups[j].append(p)
        new = []
        for i, g in enumerate(groups):
            if g:
                new.append((sum(x for x, _, _ in g) / len(g), sum(y for _, y, _ in g) / len(g)))
            else:
                new.append(centers[i])
        if new == centers:
            break
        centers = new
    return groups
