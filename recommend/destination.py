"""도착지(지역) 후보 점수화·다양성 선택 — 도착역 미지정 시 'theme + party' 기준 자동 추천.

순수 계산 모듈(DB·네트워크 비의존). 지역별 연령적합/그룹친화 실데이터가 없어
**테마 → 연령/그룹 적합 휴리스틱 상수표**로 도출한다(예: 어떤 지역에 THEME_PARK 장소가
많으면 어린이 적합도↑). 표 값은 상식 기반 휴리스틱이며 상수로 빼 튜닝 가능하다.

흐름: 라이브 스캔으로 받은 area별 테마분포(AreaProfile) → 점수화(theme/age/access - group)
→ 권역(province) 다양성 필터 → 상위 top_k 도착지 후보.
"""

from dataclasses import dataclass

from core.enums import Theme
from recommend.routing import haversine
from schemas.recommend_schema import Party

# ── 가중치(튜닝 가능) ────────────────────────────────────────────
WEIGHT_THEME = 0.5
WEIGHT_AGE = 0.3
WEIGHT_ACCESS = 0.2
# 어린이 동반이면 연령적합 비중↑·접근성 비중↓ (아이 동반은 연령 적합도가 더 중요)
WEIGHT_AGE_CHILD = 0.4
WEIGHT_ACCESS_CHILD = 0.1
WEIGHT_GROUP_PENALTY = 0.1   # 그룹 페널티 최대 감점(약하게 반영)
GROUP_LARGE = 5              # 총인원이 이 값 이상이면 그룹 친화도 페널티 검토

# 도착지(area) 중심과 매핑된 도착역의 최대 허용 거리(km). 이보다 멀면 철도로 닿기 어려운
# 지역(바다 건너 섬 등, 예: 제주)으로 보고 후보에서 제외한다. 기차 여행 플랫폼 제약.
MAX_STATION_GAP_KM = 60.0

# ── 테마 → 연령대 적합도(0~1). 실데이터 없음 → 휴리스틱 ──────────
#                       (성인,  청소년, 어린이)
_AGE_SUIT: dict[Theme, tuple[float, float, float]] = {
    Theme.NATURE:     (0.8, 0.6, 0.5),
    Theme.OCEAN:      (0.8, 0.8, 0.7),
    Theme.HISTORY:    (0.9, 0.6, 0.4),
    Theme.CITY:       (0.8, 0.9, 0.6),
    Theme.HEALING:    (0.9, 0.4, 0.4),
    Theme.FOOD:       (0.9, 0.8, 0.6),
    Theme.CULTURE:    (0.9, 0.7, 0.5),
    Theme.THEME_PARK: (0.7, 1.0, 1.0),
}
# ── 테마 → 그룹 친화도(0~1). 큰 단체에 무난한 정도. 휴리스틱 ─────
_GROUP_FRIENDLY: dict[Theme, float] = {
    Theme.NATURE: 0.7, Theme.OCEAN: 0.8, Theme.HISTORY: 0.7, Theme.CITY: 0.9,
    Theme.HEALING: 0.5, Theme.FOOD: 0.8, Theme.CULTURE: 0.7, Theme.THEME_PARK: 0.9,
}

# 도시간 철도 평균 속도(km/h) — 거리→이동시간 추정용(실제 시간표 연동 자리, 추후 인터페이스).
_RAIL_KMH = 120.0
# nights → 적정 편도 거리(km). 당일/1박은 가깝게, 길수록 멀리 허용.
_IDEAL_KM = {0: 120.0, 1: 180.0, 2: 320.0, 3: 450.0}
_IDEAL_KM_LONG = 600.0   # 4박 이상


@dataclass
class AreaProfile:
    """도착지 후보 1곳(시도 area)의 라이브 프로파일 + 메타.

    theme_counts/centroid/total은 라이브 스캔이 채우고, station/province는 서비스가
    채운다(역 매핑·권역). score 이하는 rank_and_diversify가 채운다.
    """

    area_code: int
    centroid: tuple[float, float]
    theme_counts: dict[Theme, int]
    total: int
    station: object | None = None      # 매핑된 도착역(서비스가 nearest_major로 채움)
    province: str | None = None        # 다양성 키(관리 본부 등)
    score: float = 0.0
    theme_fit: float = 0.0
    age_fit: float = 0.0
    access_fit: float = 0.0
    distance_km: float = 0.0


def rank_and_diversify(
    profiles: list[AreaProfile],
    themes: list[Theme],
    party: Party,
    origin: tuple[float, float],
    nights: int,
    max_travel_minutes: int | None = None,
    top_k: int = 3,
) -> list[AreaProfile]:
    """후보 area들을 점수화 → 권역 다양성 필터 → 상위 top_k 도착지 반환.

    finalScore = WEIGHT_THEME*theme_fit + wAge*age_fit + WEIGHT_ACCESS*access_fit - group_penalty.
    (어린이 동반 시 wAge↑·wAccess↓.) 이동거리 상한(maxAllowed) 초과 후보는 제외한다.
    """
    # 어린이 동반이면 연령적합↑·접근성↓로 가중치를 바꿔 끼운다(테마 상수 WEIGHT_*_CHILD).
    w_age = WEIGHT_AGE_CHILD if party.child > 0 else WEIGHT_AGE
    w_access = WEIGHT_ACCESS_CHILD if party.child > 0 else WEIGHT_ACCESS
    limit = _max_distance(nights, max_travel_minutes)  # 이 거리를 넘는 후보는 아예 탈락

    scored: list[AreaProfile] = []
    for p in profiles:
        # 관광지 0곳이거나 역 매핑 실패(station None)면 도착지가 될 수 없어 스킵
        if p.total <= 0 or p.station is None:
            continue
        p.distance_km = haversine(origin[0], origin[1], p.centroid[0], p.centroid[1])
        if p.distance_km > limit:  # 너무 멀면(당일치기에 부적합 등) 제외
            continue
        sh = _shares(p.theme_counts, p.total)
        p.theme_fit = _theme_fit(sh, themes)
        p.age_fit = _age_fit(sh, party)
        p.access_fit = _access_fit(p.distance_km, nights)
        p.score = (
            WEIGHT_THEME * p.theme_fit
            + w_age * p.age_fit
            + w_access * p.access_fit
            - _group_penalty(sh, party)
        )
        scored.append(p)

    scored.sort(key=lambda x: x.score, reverse=True)
    return _diversify(scored, top_k)


def _shares(theme_counts: dict[Theme, int], total: int) -> dict[Theme, float]:
    """테마별 비중(themeVector 정규화). total이 0이면 빈 dict."""
    if total <= 0:
        return {}
    return {t: c / total for t, c in theme_counts.items()}


def _theme_fit(shares: dict[Theme, float], themes: list[Theme]) -> float:
    """선택 테마들이 고루 분포할수록 1(정규화 기하평균). 테마 미선택이면 1.0.

    m×(∏ p_t)^(1/m), p_t=선택 테마 내 비중(합=1), m=선택 테마 수. 한 테마라도 비면 0,
    모두 균형이면 1. 단순 합과 달리, live_places가 선택 테마만 걸러온 Phase B에서도
    '바다·미식 둘 다 풍부한가'를 변별한다(한쪽만 많은 곳은 감점).
    """
    if not themes:
        return 1.0
    sel = [shares.get(t, 0.0) for t in themes]  # 선택 테마별 지역 내 비중
    tot = sum(sel)
    if tot <= 0.0:  # 선택 테마가 지역에 하나도 없음 → 부적합
        return 0.0
    # 선택 테마 안에서 다시 정규화(합=1)해 '선택 테마들끼리의 균형'만 본다.
    # 기하평균이라 한 테마라도 비중 0이면 곱이 0 → 전체 0(균형 붕괴에 민감).
    m = len(themes)
    prod = 1.0
    for s in sel:
        prod *= s / tot
    return m * (prod ** (1.0 / m))  # ×m: 완전 균형(각 1/m)일 때 1이 되도록 스케일 복원


def _age_fit(shares: dict[Theme, float], party: Party) -> float:
    """지역 테마분포 × 연령적합표를 party 인원비율로 가중평균(0~1). 인원 미입력이면 성인 기준."""
    suit = [0.0, 0.0, 0.0]   # 성인, 청소년, 어린이
    for t, s in shares.items():
        a = _AGE_SUIT.get(t)
        if a:
            suit[0] += s * a[0]
            suit[1] += s * a[1]
            suit[2] += s * a[2]
    counts = (party.adult, party.youth, party.child)
    total = sum(counts)
    if total == 0:
        return suit[0]
    return (counts[0] * suit[0] + counts[1] * suit[1] + counts[2] * suit[2]) / total


def _access_fit(distance_km: float, nights: int) -> float:
    """적정 거리(_IDEAL_KM)에 가까울수록 1, 너무 가깝거나 멀수록 0에 수렴."""
    ideal = _IDEAL_KM.get(nights, _IDEAL_KM_LONG)
    return max(0.0, 1.0 - abs(distance_km - ideal) / ideal)


def _group_penalty(shares: dict[Theme, float], party: Party) -> float:
    """총인원이 많은데 지역 그룹친화도가 낮으면 약한 감점."""
    total = party.adult + party.youth + party.child
    if total < GROUP_LARGE:
        return 0.0
    gf = sum(s * _GROUP_FRIENDLY.get(t, 0.6) for t, s in shares.items())
    return WEIGHT_GROUP_PENALTY * (1.0 - gf)


def _max_distance(nights: int, max_travel_minutes: int | None) -> float:
    """후보 허용 편도 거리 상한(km). 적정거리의 2배까지 허용, max_travel_minutes 있으면 추가 제한."""
    base = _IDEAL_KM.get(nights, _IDEAL_KM_LONG) * 2.0
    if max_travel_minutes:
        base = min(base, max_travel_minutes / 60.0 * _RAIL_KMH)
    return base


def _diversify(ranked: list[AreaProfile], top_k: int) -> list[AreaProfile]:
    """같은 권역(province)은 최대 1곳만 통과시켜 한 지역 쏠림을 막는다.

    권역 다양성만으로 top_k가 안 차면 점수 상위로 채운다.
    """
    picked: list[AreaProfile] = []
    seen_prov: set = set()
    for p in ranked:
        if p.province in seen_prov:
            continue
        picked.append(p)
        seen_prov.add(p.province)
        if len(picked) >= top_k:
            return picked

    chosen = {id(x) for x in picked}
    for p in ranked:
        if len(picked) >= top_k:
            break
        if id(p) not in chosen:
            picked.append(p)
    return picked
