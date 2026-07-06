"""Day 내부 시각 스케줄링 — 식사(식당)는 식사 시간대에, 관광지는 그 사이에 배치한다.

핵심 규칙:
- 식당(content_type_id=39)은 '식사'로 보고 점심(~12시)·저녁(~18시) 앵커에만 놓는다(하루 최대 2끼).
  → 밥 먹고 또 밥 먹는(식당 연속) 일정을 막는다. 식당만 있어도 하루 2끼까지만.
- 관광지는 동선(지리 NN+2-opt) 순으로, 체류(_DWELL_H)와 장소 간 이동시간(_travel_h)을 반영해
  채운다. 가까운 장소는 촘촘히, 먼 장소는 이동시간만큼 더 벌어져 하루에 덜 들어간다.
- 운영시간(오픈/마감·휴무요일)은 소프트 제약: 개점 전이면 미루고, 마감/창을 넘으면 그 곳을
  건너뛰고 차순위로 대체한다.

순수 계산 모듈(네트워크·DB 비의존). 운영시간은 ScoredPlace에 이미 채워져 들어온다.
비결정 요소 없음(같은 입력 → 같은 스케줄).
"""

from recommend import routing
from recommend.types import ScoredPlace

# 장소 1곳 체류(관람/식사) 소요(h). 표시용 방문 종료시각(itinerary._DWELL_H)과 반드시 일치.
# 예전엔 여기에 이동시간까지 뭉뚱그린 2.5였으나, 이제 이동은 _travel_h로 분리한다.
_DWELL_H = 2.0
# day_window가 없을 때의 기본 관광 가능 시간대(9~21시).
_DAY_START = 9.0
_DAY_END = 21.0
_EPS = 1e-9

# 장소 간 이동시간(h): 직선거리 × 우회계수 ÷ 평속. 인접해도 최소 이동/준비 시간은 둔다.
# 직선거리라 도로거리 대비 낙관적이므로 _DETOUR로 보정(정밀 도로시간은 라우팅 API 자리).
_SPEED_KMH = 30.0   # 도보·대중교통 혼합 보수적 평속
_DETOUR = 1.3       # 직선→실제 경로 보정 계수
_MIN_MOVE_H = 0.25  # 인접해도 최소 15분(하차·도보·준비)


def _travel_h(a: ScoredPlace, b: ScoredPlace) -> float:
    """두 장소 간 이동 추정 시간(h). 가까울수록 작아 코스가 촘촘해진다."""
    km = routing.haversine(a.lat, a.lng, b.lat, b.lng) * _DETOUR
    return max(_MIN_MOVE_H, km / _SPEED_KMH)

# 식당으로 보는 TourAPI contentTypeId(음식점).
_MEAL_CT = 39
# 식사 앵커: (목표 방문시각, 허용 시작 하한, 허용 시작 상한). 점심·저녁 두 끼.
_MEALS = ((12.0, 11.0, 14.0), (18.0, 17.0, 20.0))


def _is_meal(p: ScoredPlace) -> bool:
    return p.content_type_id == _MEAL_CT


def schedule_day(
    candidates: list[ScoredPlace],
    cap: int,
    window: tuple[float, float] | None,
    weekday: int | None,
    *,
    origin: tuple[float, float] | None = None,
    is_last: bool = False,
) -> list[tuple[ScoredPlace, float]]:
    """cap개 이하의 방문지를 식사 시간·운영시간에 맞춰 고르고 방문 시각을 배정한다.

    candidates: 그 날 클러스터 멤버(점수 내림차순). cap보다 많이 받아 대체 후보로 쓴다.
    cap: 그 날 방문지 상한(열차 시각 기반 first/last cap 포함). 0이면 빈 일정.
    window: (start_h, end_h) 그 날 관광 가능 시간대. None이면 기본(9~21).
    weekday: 그 날 요일(월0~일6) 또는 None. 후보의 휴무요일이면 그 날은 제외(하드).
    반환: [(ScoredPlace, arrive_hour)] 방문 순서(시각순)대로. 빈 리스트 가능.
    """
    if cap <= 0 or not candidates:
        return []
    start, end = window if window else (_DAY_START, _DAY_END)
    # 그 날 문 닫은 곳은 후보에서 제외(휴무일 방문 불가).
    pool = [c for c in candidates if weekday is None or weekday not in c.closed_weekdays]
    if not pool:
        return []

    meals = [c for c in pool if _is_meal(c)]          # 점수순 유지
    attrs = [c for c in pool if not _is_meal(c)]
    # 관광지는 동선(지리) 순으로 방문(이동 최소화). 마지막 날은 출발지 복귀로 마무리.
    if attrs:
        attrs = routing.two_opt(routing.nearest_neighbor(attrs))
        if is_last and origin is not None:
            attrs = routing.close_cycle(attrs, origin)

    plan: list[tuple[ScoredPlace, float]] = []
    busy: list[tuple[float, float]] = []  # 이미 점유된 시간 구간들

    # 1) 식사: 점심·저녁 앵커에 최고점 식당을 개점시각 지켜 배치(최대 2끼).
    for target, lo, hi in _MEALS:
        if len(plan) >= cap or not meals:
            break
        picked = _place_meal(meals, target, max(lo, start), min(hi, end), start, end)
        if picked is None:
            continue
        m, arrive = picked
        plan.append((m, arrive))
        busy.append((arrive, arrive + _DWELL_H))
        meals.remove(m)

    # 2) 관광지: 동선 순으로, 장소 사이 '이동시간'을 반영해 배치한다(가까울수록 촘촘, 멀수록 시간↑).
    cursor = start   # 다음 이동을 시작할 수 있는 시각(직전 장소 관람 종료 시각)
    prev = None      # 직전 배치 관광지(이동시간 기준). 식사 뒤엔 None으로 리셋(식사↔관광 이동은 별도).
    ai = 0
    while len(plan) < cap and ai < len(attrs):
        p = attrs[ai]
        ai += 1
        move = _travel_h(prev, p) if prev is not None else 0.0
        arrive = max(cursor + move, _open_of(p, start))
        # 도착~관람종료가 식사 점유와 겹치면 식사 끝으로 미루고 이 장소 재시도(이동 기준 리셋).
        jump = _busy_end_after(arrive, busy)
        if jump is not None:
            cursor = jump
            prev = None
            ai -= 1
            continue
        # 마감/하루 끝 전에 관람이 끝나야 채택. 아니면 이 곳 건너뜀(cursor 유지).
        if arrive + _DWELL_H > min(_close_of(p, end), end) + _EPS:
            continue
        plan.append((p, arrive))
        busy.append((arrive, arrive + _DWELL_H))
        cursor = arrive + _DWELL_H
        prev = p

    plan.sort(key=lambda x: x[1])  # 시각순
    return plan


def _place_meal(meals, target, lo, hi, start, end):
    """식사 앵커(target, [lo,hi] 창) 안에서 개점시각 지켜 방문 가능한 최고점 식당을 고른다.

    도착=max(목표, 하루시작, 개점). 그 시각이 창[lo,hi] 안이고 마감/하루끝 전에 끝나야 채택.
    """
    for m in meals:
        arrive = max(target, start, _open_of(m, target))
        if arrive < lo - _EPS or arrive > hi + _EPS:
            continue
        if arrive + _DWELL_H > min(_close_of(m, end), end) + _EPS:
            continue
        return m, arrive
    return None


def _busy_end_after(t: float, busy: list) -> float | None:
    """시각 t에서 시작하는 슬롯이 점유 구간과 겹치면 그 겹치는 구간의 끝을 반환, 없으면 None."""
    ends = [e for (s, e) in busy if t < e - _EPS and s < t + _DWELL_H - _EPS]
    return max(ends) if ends else None


def _open_of(p: ScoredPlace, default: float) -> float:
    return p.open_hour if p.open_hour is not None else default


def _close_of(p: ScoredPlace, default: float) -> float:
    return p.close_hour if p.close_hour is not None else default


def _selfcheck() -> None:
    """스케줄 셀프체크 — 식당은 식사시간대·최대 2끼, 식당 연속 없음. python -m recommend.scheduling."""
    def place(idx, ct, score, lat=35.0, lng=129.0, oh=None, ch=None):
        return ScoredPlace(place_idx=idx, name=f"p{idx}", region="x", lat=lat, lng=lng,
                           themes=[], score=score, content_type_id=ct, open_hour=oh, close_hour=ch)

    # 식당만 6곳(FOOD 단독) → 점심·저녁 2끼만, 3연속 없음.
    only_food = [place(i, 39, 1.0 - i * 0.01) for i in range(6)]
    r = schedule_day(only_food, cap=3, window=(9.0, 21.0), weekday=None)
    assert len(r) == 2, f"식당만이면 2끼여야: {len(r)}"
    times = sorted(t for _, t in r)
    assert 11.0 <= times[0] <= 14.0 and 17.0 <= times[1] <= 20.0, f"점심·저녁 시간대: {times}"

    # 식당3 + 관광지3 → 식당은 2끼, 나머지는 관광지, 식당끼리 안 붙음.
    mixed = [place(i, 39, 1.0 - i * 0.01) for i in range(3)] + \
            [place(100 + i, 12, 0.9 - i * 0.01, lat=35.1 + i * 0.01) for i in range(3)]
    r2 = schedule_day(mixed, cap=4, window=(9.0, 21.0), weekday=None)
    meal_count = sum(1 for p, _ in r2 if _is_meal(p))
    assert meal_count <= 2, f"식당 최대 2끼: {meal_count}"
    kinds = [_is_meal(p) for p, _ in r2]  # 시각순
    assert not any(kinds[i] and kinds[i + 1] for i in range(len(kinds) - 1)), f"식당 연속 금지: {kinds}"

    # 관광지만 → 기존처럼 동선순 채움(넉넉한 창이면 3곳 다).
    only_attr = [place(200 + i, 12, 0.9 - i * 0.01, lat=35.0 + i * 0.02) for i in range(3)]
    r3 = schedule_day(only_attr, cap=3, window=(9.0, 21.0), weekday=None)
    assert len(r3) == 3, f"관광지 3곳 다 배치: {len(r3)}"

    # 이동시간 반영: 같은 개수·같은 창이라도 '가까운' 관광지가 '먼' 관광지보다 더 많이 들어간다.
    close = [place(300 + i, 12, 0.9, lat=35.0 + i * 0.001) for i in range(3)]   # ~0.1km 간격
    far = [place(400 + i, 12, 0.9, lat=35.0 + i * 0.5) for i in range(3)]       # ~55km 간격
    rc = schedule_day(close, cap=3, window=(9.0, 14.0), weekday=None)           # 짧은 5h 창
    rf = schedule_day(far, cap=3, window=(9.0, 14.0), weekday=None)
    assert len(rc) > len(rf), f"가까우면 더 많이: close={len(rc)} far={len(rf)}"
    # 배치된 방문은 시각순이고 관람 구간이 안 겹친다(이동 간격 확보).
    ts = [t for _, t in rc]
    assert all(ts[i] + _DWELL_H <= ts[i + 1] + _EPS for i in range(len(ts) - 1)), ts
    print(f"scheduling selfcheck OK (close={len(rc)} > far={len(rf)})")


if __name__ == "__main__":
    _selfcheck()
