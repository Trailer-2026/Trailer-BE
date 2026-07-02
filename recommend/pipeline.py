"""추천 파이프라인 오케스트레이션 — 1~5단계를 엮어 Course 후보를 만든다."""

from datetime import datetime, timedelta

from core.enums import THEME_LABELS, Theme
from recommend import clustering, routing, scheduling
from recommend.types import Cluster, ScoredPlace
from schemas.recommend_schema import Course, DayPlan, RecommendedPlace, SearchCriteria

# 사용자가 셋 중 하나를 고르는 코스 후보 수
_NUM_COURSES = 3
# 하루 최대 방문지 수(그 이상은 현실적으로 소화 불가)
_MAX_PER_DAY = 3
_LABELS = ["A", "B", "C", "D", "E"]


def max_working(k: int) -> int:
    """일수 k일 때 코스 조립에 실제로 쓰이는 상위 후보 수(작업셋 상한).

    recommend_service가 이 수만큼의 상위 후보에 대해서만 운영시간을 조회(detailIntro2)해
    호출 수를 코스에 배정될 장소들로 제한한다.
    """
    return _NUM_COURSES * k * _MAX_PER_DAY


def build_courses(
    scored: list[ScoredPlace],
    criteria: SearchCriteria,
    k: int,
    origin: tuple[float, float],
    first_cap: int | None = None,
    last_cap: int | None = None,
    day_windows: list[tuple[float, float]] | None = None,
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
    # 슬라이스 스텝(::3)이라 한 장소는 정확히 한 버킷에만 들어가 코스 간 겹침 0.
    # 각 코스가 상위권을 번갈아 나눠 가져 셋 다 품질이 고르게 유지된다(상위권 한 코스 독식 방지).
    buckets = [working[i::_NUM_COURSES] for i in range(_NUM_COURSES)]

    courses: list[Course] = []
    for label, bucket in zip(_LABELS, buckets):
        if not bucket:
            continue
        clusters = clustering.kmeans_by_geo(bucket, k)
        clusters = [cl for cl in clusters if cl.members]
        if not clusters:
            continue
        # 하루 방문지 상한은 _assemble이 날짜별로 적용(첫날/마지막날은 열차 시각 기반).
        course = _assemble(
            label, clusters, criteria, origin, selected, first_cap, last_cap, day_windows
        )
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
    per = max(1, n // len(selected))       # 테마당 쿼터(총 n을 테마 수로 균등 분배)
    remaining = {t: per for t in selected}  # 테마별 남은 쿼터 (0이 되면 그 테마는 마감)
    picked: list[ScoredPlace] = []
    seen: set[int] = set()
    for p in scored:  # scored는 점수 내림차순 전제 → 각 테마 내에서 상위부터 채워진다
        if len(picked) >= n:
            break
        matched = [t for t in p.themes if t in remaining]
        # 매칭 테마 중 하나라도 쿼터가 남아야 채택(모두 마감된 테마뿐이면 이번엔 건너뜀)
        if matched and any(remaining[t] > 0 for t in matched):
            picked.append(p)
            seen.add(p.place_idx)
            # 여러 테마를 만족하는 장소는 해당 테마 쿼터를 동시 차감(한 곳이 여러 몫을 대신함)
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
    """출발지에서 가까운 군집부터 방문하도록 Day 순서를 NN으로 정한다.

    현재 위치에서 센트로이드가 가장 가까운 군집을 매번 골라 이어붙이는 그리디(NN).
    전역 최적해를 보장하지 않음 (속도와 trade-off)
    """
    remaining = clusters[:]
    cur = origin  # 첫 Day는 출발지(도착역)에서 가장 가까운 군집부터 시작
    ordered: list[Cluster] = []
    while remaining:
        # 현재 위치 기준 센트로이드가 가장 가까운 군집을 다음 Day로 선택(그리디)
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
    first_cap: int | None = None,
    last_cap: int | None = None,
    day_windows: list[tuple[float, float]] | None = None,
) -> Course:
    """정해진 군집(Day)들을 하나의 Course로 조립한다.

    Day 순서(_order_days) → 하루 방문지 상한 컷 → Day 안 시각 스케줄링(운영시간 반영) → 방문 시각 배정.
    하루 상한은 기본 _MAX_PER_DAY이나, 첫날(도착일)·마지막날(귀가일)은 열차 도착/출발 시각에서
    구한 first_cap/last_cap으로 더 줄인다(오후 도착이면 덜, 오전 귀가면 거의 안 채움).
    day_windows(그 날 관광 가능 시간대)가 있으면 scheduling이 관광지 운영시간에 맞춰 순서를 정하고
    운영시간 밖인 곳은 차순위 후보로 대체한다. 운영시간 정보가 없는 날은 기존 동선(NN+2-opt) 순서.
    """
    ordered = _order_days(clusters, origin)
    go = _parse_ymd(criteria.go_date)
    n = len(ordered)

    days: list[DayPlan] = []
    total_score = 0.0
    for idx, cl in enumerate(ordered):
        # 하루 상한: 기본 _MAX_PER_DAY, 첫날/마지막날만 열차 시각 기반 cap으로 축소.
        cap = _MAX_PER_DAY
        if idx == 0 and first_cap is not None:
            cap = min(first_cap, _MAX_PER_DAY)
        if idx == n - 1 and last_cap is not None:  # 당일치기(n==1)면 last_cap이 우선
            cap = min(last_cap, _MAX_PER_DAY)
        window = day_windows[idx] if day_windows and idx < len(day_windows) else None
        # 그 날 요일(휴무 판정용) — go_date가 있어야 계산 가능.
        weekday = (go + timedelta(days=idx)).weekday() if go else None
        # 클러스터 전체를 점수순으로 넘겨 운영시간에 안 맞는 곳을 차순위로 대체할 여지를 준다.
        candidates = sorted(cl.members, key=lambda p: p.score, reverse=True)
        scheduled = scheduling.schedule_day(
            candidates, cap, window, weekday, origin=origin, is_last=(idx == n - 1)
        )
        total_score += sum(p.score for p, _ in scheduled)
        days.append(
            DayPlan(
                day_no=idx + 1,
                date=_fmt_ymd(go + timedelta(days=idx)) if go else None,
                places=[_to_reco(p, selected, arrive) for p, arrive in scheduled],
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


def _to_reco(
    p: ScoredPlace, selected: set[Theme], arrive_hour: float | None = None
) -> RecommendedPlace:
    return RecommendedPlace(
        place_idx=p.place_idx,
        name=p.name,
        region=p.region,
        lat=p.lat,
        lng=p.lng,
        themes=p.themes,
        preference_score=round(p.score, 4),
        reason=_reason(p, selected, arrive_hour),
        open_time=_hhmm(p.open_hour),
        close_time=_hhmm(p.close_hour),
        visit_time=_hhmm(arrive_hour),
    )


def _reason(p: ScoredPlace, selected: set[Theme], arrive_hour: float | None = None) -> str:
    matched = [t for t in p.themes if t in selected] or p.themes
    tags = " ".join(f"#{THEME_LABELS.get(t, t.value)}" for t in matched[:3])
    base = f"{tags} 취향과 일치 (선호도 {p.score:.2f})" if selected else f"{tags} 인기 추천지"
    if arrive_hour is None:
        return base
    # 방문 예정 시각을 붙이고, 운영시간이 파악된 곳은 함께 표기(마감 전 방문 안내).
    if p.open_hour is not None or p.close_hour is not None:
        win = f"{_hhmm(p.open_hour) or '?'}~{_hhmm(p.close_hour) or '?'}"
        return f"{base} · {_hhmm(arrive_hour)} 방문 (운영 {win})"
    return f"{base} · {_hhmm(arrive_hour)} 방문"


def _hhmm(hour: float | None) -> str | None:
    """시각(float 시간) → 'HH:MM'. None이면 None. 자정 넘김(≥24)은 다음날 시각으로 표기."""
    if hour is None:
        return None
    total = int(round(hour * 60))
    hh, mm = divmod(total, 60)
    hh %= 24  # 24:00·26:00 등은 00:00·02:00로 표기
    return f"{hh:02d}:{mm:02d}"


def _parse_ymd(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%d") if s else None
    except (TypeError, ValueError):
        return None


def _fmt_ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")
