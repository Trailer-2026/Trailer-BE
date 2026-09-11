"""추천 파이프라인 오케스트레이션 — 1~5단계를 엮어 Course 후보를 만든다."""

from datetime import datetime, timedelta

from core.enums import THEME_LABELS, Theme
from recommend import clustering, routing, scheduling
from recommend.types import Cluster, ScoredPlace
from schemas.recommend_schema import Course, DayPlan, RecommendedPlace, SearchCriteria

# 사용자가 셋 중 하나를 고르는 코스 후보 수
_NUM_COURSES = 3
# 하루 최대 관광지 수(식사 제외 — 식사는 scheduling._MEALS 만큼 따로 얹힌다)
_MAX_PER_DAY = 3
# 하루 식사 수(점심·저녁). 작업셋의 식당 몫을 정한다.
_MEALS_PER_DAY = len(scheduling._MEALS)
_LABELS = ["A", "B", "C"]  # 코스 수(_NUM_COURSES)와 zip이라 그만큼만 쓰인다


def working_set(scored: list[ScoredPlace], themes: list[Theme] | None, k: int) -> list[ScoredPlace]:
    """코스 조립에 실제로 쓰이는 상위 후보 집합(작업셋).

    관광지 _NUM_COURSES × k × _MAX_PER_DAY + 식당 _NUM_COURSES × k × _MEALS_PER_DAY. 몫을 나누는
    이유: 합쳐서 뽑으면 FOOD 테마에서 식당이 관광지 자리를 차지해 하루 관광지가 모자라고, 모자란
    만큼 다른 코스 관광지를 빌려 와 코스끼리 겹친다. 식당이 없는 검색은 식당 몫이 0이라 예전과 같다.
    recommend_service가 이 집합에만 운영시간을 조회(detailIntro2)해 호출 수를 코스에 배정될 장소로 제한한다.
    운영시간 조회(recommend_service._attach_hours)와 코스 생성(build_courses)이 **반드시
    같은 집합**을 쓰도록 하는 단일 진입점. 다중 테마면 테마 쿼터 때문에 원점수 상위 N개와
    달라질 수 있어(차순위가 코스에 섞임), 조회 대상을 이 함수로 통일해야 미조회 후보가 코스에
    들어가는 것을 막는다.
    """
    attrs = [p for p in scored if p.content_type_id != scheduling._MEAL_CT]
    meals = [p for p in scored if p.content_type_id == scheduling._MEAL_CT]
    # 관광지 쿼터에서 FOOD는 뺀다(FOOD는 식당(39)으로만 채워지는 테마라 관광지 쪽엔 몫이 없다).
    picked = _select_working(attrs, set(themes or []) - {Theme.FOOD}, _NUM_COURSES * k * _MAX_PER_DAY)
    picked += meals[:_NUM_COURSES * k * _MEALS_PER_DAY]  # scored는 점수순이라 상위 식당
    picked.sort(key=lambda p: p.score, reverse=True)      # 버킷 인터리브는 점수 랭크 기준
    return picked


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
    고루 갖되 장소는 달라짐). 각 코스: kmeans(k=일수, 관광지 기준) → 하루 관광지 3곳 + 식사 2끼 캡
    → NN+2-opt → 마지막 day 출발지 복귀. origin은 현지 기준점(도착지) 좌표.
    """
    if not scored or k < 1:
        return []

    selected = set(criteria.themes or [])

    # 코스 3개 × 일수 × (관광지 3 + 식당 2) 만큼의 상위 후보를 작업셋으로 (다중 테마면 테마별 균형).
    # _attach_hours(운영시간 조회)와 동일 집합을 보장하려 working_set 단일 진입점 사용.
    working = working_set(scored, criteria.themes, k)
    # 점수 랭크 인터리브 → 서로 다른 3개 버킷 (A: 0,3,6.. / B: 1,4,7.. / C: 2,5,8..)
    # 슬라이스 스텝(::3)이라 한 장소는 정확히 한 버킷에만 들어가 코스 간 겹침 0.
    # 각 코스가 상위권을 번갈아 나눠 가져 셋 다 품질이 고르게 유지된다(상위권 한 코스 독식 방지).
    # **관광지와 식당을 따로 인터리브한다** — 섞인 점수순을 한 번에 나누면 식당 랭크가 어디 걸리느냐에
    # 따라 한 코스는 관광지가 모자라고 다른 코스는 남는다. 모자란 코스는 attraction_pool에서 남의
    # 관광지를 빌려 와 코스끼리 겹친다(working_set이 몫을 나눈 이유가 그대로 되살아난다).
    attrs_w = [p for p in working if p.content_type_id != scheduling._MEAL_CT]
    meals_w = [p for p in working if p.content_type_id == scheduling._MEAL_CT]
    buckets = [attrs_w[i::_NUM_COURSES] + meals_w[i::_NUM_COURSES] for i in range(_NUM_COURSES)]

    # 중간 날이 식당만이라 2끼(2곳)에 그칠 때 보충할 비-식당 관광지 풀. working(운영시간
    # 부착 작업셋)에서만 뽑아 hours 일관성을 유지한다. 코스 간 중복은 허용하되(관광지 희소)
    # 최소화한다 — 풀이 세 버킷 전체라 보충분은 항상 다른 코스의 관광지다. 그래서 (1) 모자란
    # 만큼만 보충하고 (2) 앞 코스가 이미 쓴 곳은 뒤로 미룬다(used_elsewhere). 실측(역 6곳 ×
    # 테마 3조합)에서 cap 만큼 늘 보충하던 때는 코스 셋이 평균 4.6곳을 공유했다.
    attraction_pool = [p for p in working if p.content_type_id != scheduling._MEAL_CT]

    courses: list[Course] = []
    used_elsewhere: set[int] = set()
    for label, bucket in zip(_LABELS, buckets):
        if not bucket:
            continue
        # 날짜 묶기는 관광지로만 한다. 식당까지 섞어 균형을 맞추면 관광지가 한 날에 4곳, 다른 날에
        # 2곳으로 쏠려 모자란 날이 남의 코스 관광지를 빌려 온다(코스 간 겹침). 식당은 가장 가까운
        # 날에 붙인다 — 한 날에 몰려도 scheduling이 2끼까지만 쓴다. 식당뿐이면(FOOD 단독) 식당으로 묶는다.
        attrs = [p for p in bucket if p.content_type_id != scheduling._MEAL_CT]
        clusters = clustering.kmeans_by_geo(attrs or bucket, k)
        clusters = [cl for cl in clusters if cl.members]
        if not clusters:
            continue
        if attrs:
            for m in bucket:
                if m.content_type_id == scheduling._MEAL_CT:
                    min(clusters, key=lambda c: routing.haversine(m.lat, m.lng, *c.centroid)).members.append(m)
        # 하루 방문지 상한은 _assemble이 날짜별로 적용(첫날/마지막날은 열차 시각 기반).
        course = _assemble(
            label, clusters, criteria, origin, selected,
            first_cap, last_cap, day_windows, attraction_pool, used_elsewhere,
        )
        if course.days:
            courses.append(course)
            used_elsewhere.update(rp.place_idx for d in course.days for rp in d.places)
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
    attraction_pool: list[ScoredPlace] | None = None,
    used_elsewhere: set[int] | frozenset[int] = frozenset(),
) -> Course:
    """정해진 군집(Day)들을 하나의 Course로 조립한다.

    Day 순서(_order_days) → 하루 방문지 상한 컷 → Day 안 시각 스케줄링(운영시간 반영) → 방문 시각 배정.
    하루 상한은 기본 _MAX_PER_DAY이나, 첫날(도착일)·마지막날(귀가일)은 열차 도착/출발 시각에서
    구한 first_cap/last_cap으로 더 줄인다(오후 도착이면 덜, 오전 귀가면 거의 안 채움).
    day_windows(그 날 관광 가능 시간대)가 있으면 scheduling이 관광지 운영시간에 맞춰 순서를 정하고
    운영시간 밖인 곳은 차순위 후보로 대체한다. 운영시간 정보가 없는 날은 기존 동선(NN+2-opt) 순서.
    attraction_pool(비-식당 관광지)이 있으면 중간 날 관광지가 cap에 못 미칠 때 근처 관광지를
    보충한다(첫날/마지막날은 미적용). cap은 관광지만 세고 식사(최대 2끼)는 따로 얹힌다.
    """
    ordered = _order_days(clusters, origin)
    go = _parse_ymd(criteria.go_date)
    n = len(ordered)

    days: list[DayPlan] = []
    total_score = 0.0
    used_in_course: set[int] = set()  # 이 코스에서 이미 배치된 place_idx(관광지 중복 보충 방지)
    # 코스 전체 날의 '원래 클러스터 멤버' idx. 뒤 날이 소유한 관광지를 앞 날이 빌려가 중복되는 걸
    # 막으려면 처리 순서와 무관하게 native 멤버 전부를 보충 풀에서 제외해야 한다.
    native_ids = {p.place_idx for cl in ordered for p in cl.members}
    for idx, cl in enumerate(ordered):
        # 하루 상한: 기본 _MAX_PER_DAY, 첫날/마지막날만 열차 시각 기반 cap으로 축소.
        cap = _MAX_PER_DAY
        if idx == 0 and first_cap is not None:
            cap = min(cap, first_cap)
        if idx == n - 1 and last_cap is not None:  # 당일치기(n==1)면 first_cap과 함께 둘 다 반영
            cap = min(cap, last_cap)
        window = day_windows[idx] if day_windows and idx < len(day_windows) else None
        # 그 날 요일(휴무 판정용) — go_date가 있어야 계산 가능.
        weekday = (go + timedelta(days=idx)).weekday() if go else None
        # 클러스터 전체를 점수순으로 넘겨 운영시간에 안 맞는 곳을 차순위로 대체할 여지를 준다.
        candidates = sorted(cl.members, key=lambda p: p.score, reverse=True)
        # 이 날에 비-식당 관광지를 보충해 슬롯을 더 채울지 판정한다(식당만이면 2끼로 끝나므로).
        #   - 첫날(idx 0): 항상 제외(도착일이라 원래 짧음).
        #   - 중간 날(idx < n-1): 채운다.
        #   - 마지막 날(idx == n-1): last_cap이 None일 때만 채운다.
        # 보통 왕복은 마지막 날이 '귀가 열차 타는 날'이라 last_cap이 숫자다(→ 안 채움). 하지만
        # 숙박경유 코스는 도시별로 쪼개 _assemble이 '도시 구간'마다 돌아가는데, 앞 도시(먼저 묵는
        # 도시) 구간의 마지막 날은 여행 마지막 날이 아니라 '그 밤 자고 다음날 이동'하는 종일 관광
        # 날이라 열차 제약이 없다 → last_cap=None. 이런 날만 마지막 위치여도 채운다.
        fillable = idx > 0 and (idx < n - 1 or last_cap is None)
        scheduled = scheduling.schedule_day(
            candidates, cap, window, weekday, origin=origin, is_last=(idx == n - 1)
        )
        # 모자란 만큼만 보충한다 — 자기 후보로 **실제 배치된** 관광지가 cap 에 못 미치는 수.
        # 휴무만 세면 안 된다: schedule_day는 마감·하루 끝 전에 관람이 안 끝나는 곳도 건너뛰어,
        # 휴무 없는 관광지 3곳을 갖고도 2곳만 배치된 날이 보충 없이 남는다.
        # 항상 cap 만큼 넣으면 scheduling 이 동선순으로 섞어 보충분이 자기 관광지를 밀어내고,
        # 그 보충분은 다른 코스의 관광지라 코스 셋이 서로 닮아 간다.
        placed = sum(1 for p, _ in scheduled if p.content_type_id != scheduling._MEAL_CT)
        need = cap - placed
        if fillable and attraction_pool and need > 0:
            # 다른 날이 이미 소유(native)하거나 이미 배치된 관광지는 제외 → 코스 내 중복 방지.
            # 앞 코스가 쓴 곳(used_elsewhere)은 뒤로 — 남는 게 그것뿐일 때만 재사용한다.
            extras = sorted(
                (p for p in attraction_pool
                 if p.place_idx not in used_in_course and p.place_idx not in native_ids),
                key=lambda p: (p.place_idx in used_elsewhere, routing.haversine(p.lat, p.lng, *cl.centroid)),
            )
            refilled = scheduling.schedule_day(
                candidates + extras[:need], cap, window, weekday, origin=origin, is_last=(idx == n - 1)
            ) if extras else []
            # 관광지가 실제로 늘 때만 바꾼다. 보충분이 동선 순서를 바꿔 자기 관광지를 밀어내고 그 자리를
            # 차지하면 수는 그대로인데 코스 간 중복만 는다(오프라인 스냅샷 재배치 69회 중 2회).
            if sum(1 for p, _ in refilled if p.content_type_id != scheduling._MEAL_CT) > placed:
                scheduled = refilled
        used_in_course.update(p.place_idx for p, _ in scheduled)
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
        image_url=p.image_url,
        open_time=routing.hhmm(p.open_hour),
        close_time=routing.hhmm(p.close_hour, closing=True),
        visit_time=routing.hhmm(arrive_hour),
        content_type_id=p.content_type_id,
    )


def _reason(p: ScoredPlace, selected: set[Theme], arrive_hour: float | None = None) -> str:
    matched = [t for t in p.themes if t in selected] or p.themes
    tags = " ".join(f"#{THEME_LABELS.get(t, t.value)}" for t in matched[:3])
    base = f"{tags} 취향과 일치 (선호도 {p.score:.2f})" if selected else f"{tags} 인기 추천지"
    if arrive_hour is None:
        return base
    # 방문 예정 시각을 붙이고, 운영시간이 파악된 곳은 함께 표기(마감 전 방문 안내).
    if p.open_hour is not None or p.close_hour is not None:
        win = f"{routing.hhmm(p.open_hour) or '?'}~{routing.hhmm(p.close_hour, closing=True) or '?'}"
        return f"{base} · {routing.hhmm(arrive_hour)} 방문 (운영 {win})"
    return f"{base} · {routing.hhmm(arrive_hour)} 방문"


def _parse_ymd(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%d") if s else None
    except (TypeError, ValueError):
        return None


def _fmt_ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")
