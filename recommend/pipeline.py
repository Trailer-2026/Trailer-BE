"""추천 파이프라인 오케스트레이션 — 1~5단계를 엮어 Course 후보를 만든다."""

from datetime import datetime, timedelta

from core.enums import THEME_LABELS, Theme
from recommend import clustering, routing
from recommend.types import Cluster, ScoredPlace
from schemas.recommend_schema import Course, DayPlan, RecommendedPlace, SearchCriteria

# 사용자가 셋 중 하나를 고르는 코스 후보 수
_NUM_COURSES = 3
# 하루 최대 방문지 수(그 이상은 현실적으로 소화 불가)
_MAX_PER_DAY = 3
_LABELS = ["A", "B", "C", "D", "E"]


def build_courses(
    scored: list[ScoredPlace],
    criteria: SearchCriteria,
    k: int,
    origin: tuple[float, float],
) -> list[Course]:
    """점수화된 추천지로부터 서로 다른 코스 후보 3개(A/B/C)를 생성한다.

    다중 테마는 scoring 단계(가중 코사인)에서 이미 반영된 score 순위를 사용한다.
    코스 3개는 점수 랭크를 인터리브해 '겹치지 않는' 풀로 나눠 만든다(각 코스가 상위권을
    고루 갖되 장소는 달라짐). 각 코스: kmeans(k=일수) → 하루 최대 3곳 캡
    → NN+2-opt → 마지막 day 출발지 복귀. origin은 현지 기준점(도착지) 좌표.
    """
    if not scored or k < 1:
        return []

    selected = set(criteria.themes or [])

    # 코스 3개 × 일수 × 하루 3곳 만큼의 상위 후보를 작업셋으로 (다중 테마면 테마별 균형)
    working = _select_working(scored, selected, _NUM_COURSES * k * _MAX_PER_DAY)
    # 점수 랭크 인터리브 → 서로 다른 3개 버킷 (A: 0,3,6.. / B: 1,4,7.. / C: 2,5,8..)
    buckets = [working[i::_NUM_COURSES] for i in range(_NUM_COURSES)]

    courses: list[Course] = []
    for label, bucket in zip(_LABELS, buckets):
        if not bucket:
            continue
        clusters = clustering.kmeans_by_geo(bucket, k)
        # 하루 최대 _MAX_PER_DAY곳으로 제한(선호도 상위만 남김)
        for cl in clusters:
            if len(cl.members) > _MAX_PER_DAY:
                cl.members = sorted(cl.members, key=lambda p: p.score, reverse=True)[:_MAX_PER_DAY]
        clusters = [cl for cl in clusters if cl.members]
        if not clusters:
            continue
        course = _assemble(label, clusters, criteria, origin, selected)
        if course.days:
            courses.append(course)
    return courses


def _select_working(scored: list[ScoredPlace], selected: set[Theme], n: int) -> list[ScoredPlace]:
    """작업셋 선정. 다중 테마면 테마별 쿼터로 균형 있게 뽑아 한 테마 쏠림을 막는다.

    선택 테마가 0~1개면 점수 상위 n개. 2개 이상이면 테마당 약 n/테마수 만큼을 점수순으로
    배정(한 장소가 여러 테마를 만족하면 동시 차감)하고, 부족분은 점수 상위로 채운 뒤 점수순 정렬.
    """
    if len(selected) <= 1:
        return scored[:n]
    per = max(1, n // len(selected))
    remaining = {t: per for t in selected}
    picked: list[ScoredPlace] = []
    seen: set[int] = set()
    for p in scored:
        if len(picked) >= n:
            break
        matched = [t for t in p.themes if t in remaining]
        if matched and any(remaining[t] > 0 for t in matched):
            picked.append(p)
            seen.add(p.place_idx)
            for t in matched:
                remaining[t] = max(0, remaining[t] - 1)
    for p in scored:  # 부족분은 점수 상위로 채움
        if len(picked) >= n:
            break
        if p.place_idx not in seen:
            picked.append(p)
            seen.add(p.place_idx)
    picked.sort(key=lambda p: p.score, reverse=True)
    return picked


def _order_days(clusters: list[Cluster], origin: tuple[float, float]) -> list[Cluster]:
    """출발지에서 가까운 군집부터 방문하도록 Day 순서를 NN으로 정한다."""
    remaining = clusters[:]
    cur = origin
    ordered: list[Cluster] = []
    while remaining:
        nxt = min(remaining, key=lambda c: routing.haversine(cur[0], cur[1], *c.centroid))
        remaining.remove(nxt)
        ordered.append(nxt)
        cur = nxt.centroid
    return ordered


def _assemble(
    label: str,
    clusters: list[Cluster],
    criteria: SearchCriteria,
    origin: tuple[float, float],
    selected: set[Theme],
) -> Course:
    ordered = _order_days(clusters, origin)
    go = _parse_ymd(criteria.go_date)

    days: list[DayPlan] = []
    total_score = 0.0
    for idx, cl in enumerate(ordered):
        route = routing.two_opt(routing.nearest_neighbor(cl.members))
        if idx == len(ordered) - 1:  # 마지막 day → 출발지 복귀 방향
            route = routing.close_cycle(route, origin)
        total_score += sum(p.score for p in route)
        days.append(
            DayPlan(
                day_no=idx + 1,
                date=_fmt_ymd(go + timedelta(days=idx)) if go else None,
                places=[_to_reco(p, selected) for p in route],
            )
        )

    return Course(
        label=label,
        origin_station_idx=criteria.origin_station_idx,
        days=days,
        total_preference_score=round(total_score, 4),
        is_round_trip_closed=bool(criteria.round_trip),
        note=None,
    )


def _to_reco(p: ScoredPlace, selected: set[Theme]) -> RecommendedPlace:
    return RecommendedPlace(
        place_idx=p.place_idx,
        name=p.name,
        region=p.region,
        lat=p.lat,
        lng=p.lng,
        themes=p.themes,
        preference_score=round(p.score, 4),
        reason=_reason(p, selected),
    )


def _reason(p: ScoredPlace, selected: set[Theme]) -> str:
    matched = [t for t in p.themes if t in selected] or p.themes
    tags = " ".join(f"#{THEME_LABELS.get(t, t.value)}" for t in matched[:3])
    if selected:
        return f"{tags} 취향과 일치 (선호도 {p.score:.2f})"
    return f"{tags} 인기 추천지"


def _parse_ymd(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%d") if s else None
    except (TypeError, ValueError):
        return None


def _fmt_ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")
