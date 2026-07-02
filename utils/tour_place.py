"""TourAPI 실시간 호출로 추천지·숙소 후보를 만든다(런타임 데이터 소스).

공모전 정책상 관광데이터는 DB 스냅샷이 아니라 매 요청 실시간 호출로 받아야 한다.
역(station)은 관광데이터가 아니므로 DB를 그대로 쓴다.
"""
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

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
_LODGING_POOL = 20               # 숙소 후보 풀 크기(거리순으로 받아 품질 신호로 재선별)
# TourAPI엔 평점·가격이 없어 '숙소 종류'를 품질 프록시로 쓴다(낮을수록 우선). 미등록 종류는 중간(2).
_LODGING_RANK = {
    "관광호텔": 0, "콘도미니엄": 0, "한옥": 0,
    "펜션": 1, "게스트하우스": 1, "유스호스텔": 1, "서비스드레지던스": 1,
    "홈스테이": 2, "민박": 3, "모텔": 3,
}
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
    content_type_id: int | None = None  # detailIntro2 운영시간 조회·유형별 필드 선택에 사용


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
    ctid = int(ct) if ct is not None and str(ct).isdigit() else None
    return LivePlace(
        place_idx=int(cid),
        name=(item.get("title") or "")[:255],
        region=(item.get("addr1") or None),
        lat=lat,
        lng=lng,
        themes=themes,
        image_url=(item.get("firstimage") or None),
        content_id=cid,
        content_type_id=ctid,
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


# ── 운영시간(오픈/마감·휴무요일) ─────────────────────────────────────────────
# detailIntro2는 유형(contentTypeId)마다 시간/휴무 필드명이 다르다. (시간필드, 휴무필드).
_HOURS_FIELDS = {
    12: ("usetime", "restdate"),            # 관광지
    14: ("usetimeculture", "restdateculture"),  # 문화시설
    15: ("playtime", None),                 # 축제·공연·행사(행사 시간)
    28: ("usetimeleports", "restdateleports"),  # 레포츠
    38: ("opentime", "restdateshopping"),   # 쇼핑
    39: ("opentimefood", "restdatefood"),   # 음식점
}
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_TAG_RE = re.compile(r"<[^>]+>")
_WEEKDAYS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
# 특정 주차만 쉬는 표현(격주/첫째주 등)은 요일 단위로 환원하면 과도하게 막으므로 무시한다.
_IRREGULAR_REST = ("첫째", "둘째", "셋째", "넷째", "다섯째", "마지막", "격주")
_ALWAYS_OPEN = ("24시간", "24시", "상시", "연중무휴", "무휴", "연중", "365")


@dataclass
class Hours:
    """관광지 1곳의 운영시간. 미상 필드는 None(=시간 제약 없음으로 취급)."""

    open_hour: float | None = None      # 예: 9.5 = 09:30
    close_hour: float | None = None     # 자정 넘김은 +24(예: 26.0 = 익일 02:00)
    closed_weekdays: tuple[int, ...] = field(default_factory=tuple)  # 월0~일6


def _to_hour(hm: tuple[str, str]) -> float:
    return int(hm[0]) + int(hm[1]) / 60


def _parse_hours(s: str | None) -> tuple[float | None, float | None]:
    """운영시간 자유텍스트 → (open, close). 파싱 불가/미상이면 (None, None).

    'HH:MM~HH:MM'류에서 앞의 두 시각을 오픈/마감으로 본다(여러 계절 표기는 첫 구간만).
    '24시간'·'상시'·'연중무휴'만 있으면 (0,24). 시각이 하나뿐이면 오픈만 잡는다.
    """
    if not s:
        return None, None
    txt = _TAG_RE.sub(" ", str(s))
    times = _TIME_RE.findall(txt)
    if len(times) >= 2:
        o, c = _to_hour(times[0]), _to_hour(times[1])
        if c <= o:          # 자정 넘어가는 영업(예: 18:00~02:00) → 다음날로
            c += 24.0
        return o, c
    if any(k in txt for k in _ALWAYS_OPEN):
        return 0.0, 24.0
    if len(times) == 1:
        return _to_hour(times[0]), None
    return None, None


def _parse_closed_weekdays(s: str | None) -> tuple[int, ...]:
    """휴무 자유텍스트 → 매주 쉬는 요일 집합(월0~일6). 연중무휴/불규칙 휴무는 빈 튜플."""
    if not s:
        return ()
    txt = _TAG_RE.sub(" ", str(s))
    if any(k in txt for k in _ALWAYS_OPEN) or "없" in txt:
        return ()
    if any(k in txt for k in _IRREGULAR_REST):  # 격주·첫째주 등은 요일 고정 휴무가 아님
        return ()
    days = set()
    for ch, idx in _WEEKDAYS.items():
        # '월요일' 또는 '매주 월'처럼 요일이 명시된 경우만(주소 등 오탐 방지)
        if re.search(ch + r"\s*요일", txt) or re.search(r"매주[^가-힣]*" + ch, txt):
            days.add(idx)
    return tuple(sorted(days))


def _fetch_hours_one(cid: str, ctype: int | None) -> tuple[str, Hours]:
    tf, rf = _HOURS_FIELDS.get(ctype or 0, (None, None))
    if not tf:
        return cid, Hours()
    try:
        item = tour_api.detail_intro(content_id=cid, content_type_id=ctype)
    except Exception as e:
        logger.warning("TourAPI 운영시간(cid=%s, ct=%s) 실패: %s", cid, ctype, e)
        return cid, Hours()
    o, c = _parse_hours(item.get(tf))
    wd = _parse_closed_weekdays(item.get(rf)) if rf else ()
    return cid, Hours(o, c, wd)


def fetch_hours(refs: list[tuple[str, int | None]]) -> dict[str, Hours]:
    """(content_id, content_type_id) 목록의 운영시간을 detailIntro2로 병렬 조회.

    content_id → Hours 매핑 반환. 조회 실패·미상은 빈 Hours(시간 제약 없음)로 둔다.
    코스에 실제 배정될 후보만 넘겨 호출 수를 제한하는 것은 호출부(recommend_service) 책임.
    """
    if not refs:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(refs))) as ex:
        results = ex.map(lambda r: _fetch_hours_one(r[0], r[1]), refs)
    return dict(results)


def _to_lodging(it: dict) -> Lodging | None:
    """TourAPI 숙박 항목 1건 → Lodging. 좌표 없으면 None(스킵)."""
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


def nearest_lodging(lat: float, lng: float, radius_m: int = _RADIUS_M) -> Lodging | None:
    """좌표 근처 숙소 후보 중 [종류 좋고 → 사진 있고 → 가까운] 순 1위 1곳을 실시간 조회한다.

    평점·가격은 TourAPI에 없어, 숙소 종류(_LODGING_RANK)·대표사진 유무를 품질 프록시로 쓴다.
    거리순(arrange=E)으로 후보 풀을 받아 그 안에서 품질 신호로 재선별한다.
    """
    try:
        items, _ = tour_api.location_based_list(
            lat=lat, lng=lng, radius_m=radius_m, content_type_id=_LODGING_CT,
            num_of_rows=_LODGING_POOL, arrange="E",
        )
    except Exception as e:
        logger.warning("TourAPI 숙박 위치기반 실패: %s", e)
        return None

    best_key, best = None, None
    for i, it in enumerate(items):
        lg = _to_lodging(it)
        if lg is None:
            continue
        # (종류 우선순위, 사진 없음, 거리 근사) 오름차순 1위. dist 없으면 조회 순서(i)로 대체.
        try:
            dist = float(it.get("dist") or i)
        except (TypeError, ValueError):
            dist = float(i)
        key = (_LODGING_RANK.get(lg.lodging_type, 2), lg.image_url is None, dist)
        if best_key is None or key < best_key:
            best_key, best = key, lg
    return best


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
