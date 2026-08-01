"""기차 경로 추천 엔진 v3.

출발/도착/가는날/오는날만으로 직통·환승·경유 왕복 경로를 생성한다.
- 노드: station DB (좌표·등급)  - 엣지: 구간 API(train_api, (역,날짜) 캐시)

연결성(환승)과 관광(경유)을 분리한다:
  · 직통        : 1열차
  · 환승 연결   : 직통이 없을 때 철도 거점에서 갈아타기 → 연결성 확보
  · 관광 경유   : 중간역에 길게(2h~6h) 체류 → 관광. 사용자가 경유역(via_station_idx)을
                 지정하면 그 역을 첫 경유 후보로 보장하고, 없으면 지리적 중간역을 자동 선정.

**경유의 각 다리에도 환승이 적용된다**(_leg_options). 직통만 보던 시절엔 '강릉→부산'처럼
직통이 없는 구간이 끼면 경유가 통째로 사라지고 직통 왕복만 남았다. 대신 연결만 되면 아무
경로나 성립하므로 경유 후보에 세 겹의 제동을 건다(main에는 적용 안 함 — 리턴 보장이라):
  · _via_groups   : 출발역·도착역 클러스터를 환승 거점에서 제외 → 되돌아가기 금지
                    ("서울 출발 → 강릉 → (서울) → 부산"처럼 이미 지나온 곳으로 되돌아갔다 다시 나감)
  · _via_travel_ok: 총 이동시간을 직통 대비 배수로 제한 → 과도한 우회 차단
  · _via_hours_ok : 심야 출발·새벽 도착 열차가 낀 후보 탈락 → "동대구 00:28 출발" 같은 것 방지
지정 경유가 끝내 성립하지 않으면 사유를 main.note에 남기고 자동 중간역 경유로 폴백한다
(경유 카드 0장 → 직통 복제로 A/B/C가 전부 같아지던 문제 방지).

환승 거점은 "클러스터"로 둔다. 서울·용산·청량리는 한 도시의 다른 터미널이므로
한 그룹으로 묶고 역간 이동 버퍼를 준다(강릉→서울 도착, 용산→여수 출발 같은 환승).
가는편/오는편 둘 다 직통이 없으면 환승으로 잇는다.

무조건 리턴 보장: 가는편 기본 왕복(직통 or 환승)을 항상 첫 번째로 반환한다.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt

from sqlalchemy.orm import Session

from core.exceptions.custom import (
    BadRequestException,
    ExternalServiceException,
    NotFoundException,
)
from databases.daos import station_dao
from databases.models.station import Station
from schemas.route_schema import RouteCandidate, RouteTrain
from utils import train_api

logger = logging.getLogger(__name__)

# 내일로패스 미적용(제외): SRT만. 내일로는 KTX 좌석 + 일반열차(ITX-마음/청춘/새마을·새마을·무궁화·
# 누리로) 좌석·입석·자유석까지 커버하므로 KTX 계열도 포함한다. SRT는 코레일이 아닌 SR 운영이라 불가.
_NAIL_EXCLUDED_PREFIX = ("SRT",)


def _nail_eligible(grade: str) -> bool:
    return not grade.startswith(_NAIL_EXCLUDED_PREFIX)


# 관광 경유(지리적 중간역에 체류)
MIN_STAY = timedelta(hours=2)
MAX_STAY = timedelta(hours=6)
MAX_DETOUR_RATIO = 1.4
MAX_CANDIDATES = 6
# 숙박 경유에서 '다음 이동 열차'를 몇 시 이후 것으로 잡을지(경유역 출발/귀가 다리 공통).
# 체크아웃·아침 후 이동하는 현실적 시각. 너무 이르면(새벽 첫차) 비현실적이라 09:00 기준.
_NEXT_DAY_START = "09:00"
# 경유역은 출발·도착 양쪽에서 충분히 떨어져야 의미가 있다(서울→용산→타지역처럼 사실상 같은 도시인 역을 경유로 세지 않기 위함).
# 각 구간이 직선거리의 이 비율 이상, 그리고 최소 이 절대거리(km) 이상이어야 후보로 인정한다.
MIN_LEG_RATIO = 0.2
MIN_LEG_KM = 25.0
# 경유 다리(출발→경유, 경유→도착)의 첫 다리를 이른 순으로 몇 편까지 시도할지.
# 가장 이른 편 하나만 보면 그 편 기준 체류 창(2~6h)에 연결편이 없을 때 경유가 통째로 사라진다.
VIA_LEG_TRIES = 3
# 경유 총 이동시간 상한 — 기본 왕복(main) 대비 이 배수 또는 +슬랙(분) 중 큰 쪽까지 허용.
# 경유 다리에 환승을 허용하면 "서울→강릉→서울→부산"처럼 되돌아가는 연결도 성립하므로,
# 직통 대비 상한으로 비현실적인 우회를 걸러낸다.
MAX_VIA_TRAVEL_RATIO = 1.8
MIN_VIA_TRAVEL_SLACK = 120
# 경유 경로의 심야 이동 금지 — 환승을 허용하면 대기 끝에 "00:28 출발" 같은 새벽 열차가 낀다.
# 출발은 [NIGHT_END, NIGHT_START) 안이어야 하고, 도착도 새벽(NIGHT_LATE_ARR~NIGHT_END)이면 안 된다.
# 기본 왕복(main)엔 적용하지 않는다 — 그건 무조건 리턴 보장이라 시각으로 거르면 안 된다.
NIGHT_START = 23
NIGHT_END = 5
NIGHT_LATE_ARR = 1

# 환승 연결(철도 거점에서 갈아타기)
TRANSFER_MIN = timedelta(minutes=20)   # 같은 역 환승 최소 시간
CLUSTER_MOVE = timedelta(minutes=40)   # 같은 도시의 다른 기차역으로 갈아탈 때 최소 40분의 이동 여유를 확보(예: 서울역↔용산역) 열차가 서울 중심이라 필요함
TRANSFER_MAX = timedelta(hours=5)      # 최대 환승 대기(거점간 열차가 하루 몇 편뿐이라 넉넉히)
# 환승 거점 클러스터. 한 그룹 안의 역들은 도시 내 다른 터미널로 보고 상호 환승 허용.
# 한국 철도는 서울·용산 중심 방사형이라 지리로 자르지 않고 전부 시도. DB에 없으면 무시.
TRANSFER_GROUPS = [
    ["서울역", "용산역", "청량리역"],   # 수도권
    ["동대구역", "대구역"],
    ["광주송정역", "광주역"],
    ["대전역"],
    ["서대전역"],
    ["오송역"],
    ["익산역"],
]
_EARTH_KM = 6371


def _haversine(a: Station, b: Station) -> float:
    lat1, lon1, lat2, lon2 = map(radians, [a.latitude, a.longitude, b.latitude, b.longitude])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_KM * asin(sqrt(h))


def _has_coords(s: Station) -> bool:
    return s.latitude is not None and s.longitude is not None


def _parse_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y%m%d")
    except ValueError:
        raise BadRequestException("날짜는 YYYYMMDD 형식이어야 합니다.")


def _start_dt(d: datetime, time: str | None) -> datetime:
    try:
        t = datetime.strptime(time or "09:00", "%H:%M").time()
    except ValueError:
        raise BadRequestException("시간은 HH:MM 형식이어야 합니다.")
    # 사용자 희망 출발시각도 KST-aware로 맞춘다(열차 시각이 aware라 naive면 비교 시 TypeError).
    return datetime.combine(d.date(), t, tzinfo=train_api.KST)


def _station(db: Session, idx: int, label: str) -> Station:
    s = station_dao.get_by_idx(db, idx)
    if not s:
        raise NotFoundException(f"{label} 역을 찾을 수 없습니다.")
    if not s.nat_code:
        raise BadRequestException(f"{s.station_name}은(는) 경로 탐색을 지원하지 않습니다.")
    return s


def _legs(dep_nat: str, arr_nat: str, ymd: str, nail_pass: bool = False) -> tuple:
    try:
        trains = train_api.fetch_trains(dep_nat, arr_nat, ymd)
    except Exception as e:
        logger.warning("구간 API 실패 %s->%s %s: %s", dep_nat, arr_nat, ymd, e)
        raise ExternalServiceException("열차 시간표 조회에 실패했습니다.")
    if nail_pass:
        trains = tuple(t for t in trains if _nail_eligible(t["grade"]))
    return trains


def _safe_fetch(dep_nat: str, arr_nat: str, ymd: str) -> None:
    try:
        train_api.fetch_trains(dep_nat, arr_nat, ymd)  # lru_cache 워밍
    except Exception:
        pass


def _prefetch_segments(dep, arr, groups, stops, go_date, back_date) -> None:
    """이번 추천에 필요한 모든 (출발,도착,날짜) 구간을 병렬로 미리 받아 캐시를 데운다.

    이후 _journey/_via_pair의 순차 호출은 전부 캐시 히트가 되어 빨라진다.
    fetch_trains는 등급 필터 전 원본이라 내일로 여부와 무관하게 공유된다.
    """
    pairs = {
        (dep.nat_code, arr.nat_code, go_date),
        (arr.nat_code, dep.nat_code, back_date),
    }
    members = {s.station_idx: s for g in groups for s in g}.values()  # 전 그룹 거점역 중복 제거
    for m in members:
        if m.station_idx in (dep.station_idx, arr.station_idx) or not m.nat_code:
            continue
        # 환승 거점 m: 가는편(dep→m→arr)·오는편(arr→m→dep) 양방향 4구간을 모두 데운다.
        pairs.add((dep.nat_code, m.nat_code, go_date))
        pairs.add((m.nat_code, arr.nat_code, go_date))
        pairs.add((arr.nat_code, m.nat_code, back_date))
        pairs.add((m.nat_code, dep.nat_code, back_date))
    for c in stops:
        # 관광 경유 후보 c: 가는편(dep→c→arr)·오는편(arr→c→dep) 양방향 경유를 모두 데운다.
        pairs.add((dep.nat_code, c.nat_code, go_date))
        pairs.add((c.nat_code, arr.nat_code, go_date))
        pairs.add((arr.nat_code, c.nat_code, back_date))
        pairs.add((c.nat_code, dep.nat_code, back_date))
    with ThreadPoolExecutor(max_workers=min(16, len(pairs))) as ex:
        list(ex.map(lambda p: _safe_fetch(*p), pairs))


def _earliest(trains: tuple, not_before: datetime) -> dict | None:
    # not_before(직전 열차 도착 + 환승/체류 최소간격) 이후 출발하는 가장 이른 열차 1편.
    # 경로를 늘 "가능한 한 빨리" 잇기 위해 최소 대기 열차를 고른다.
    cand = [t for t in trains if t["dep_time"] >= not_before]
    return min(cand, key=lambda t: t["dep_time"]) if cand else None


def _to_train(d: dict) -> RouteTrain:
    return RouteTrain(
        train_no=d["train_no"],
        grade=d["grade"],
        dep_station=d["dep_station"],
        arr_station=d["arr_station"],
        dep_time=d["dep_time"],
        arr_time=d["arr_time"],
        duration_minutes=int((d["arr_time"] - d["dep_time"]).total_seconds() // 60),
        fare=d["fare"],
    )


def _via_pair(dep, arr, via, ymd, start, min_gap, max_gap, groups, nail_pass=False):
    """출발→경유→도착 2구간 연결(관광 체류용). 간격 만족 시 (legs1, legs2).

    각 '다리'는 직통 1편 또는 환승 2편(_leg_options)이라 반환값은 열차 dict의 **목록**이다.
    직통이 없는 구간(예: 강릉→부산)도 환승으로 이어져 경유가 통째로 탈락하지 않는다.
    첫 다리는 이른 순 VIA_LEG_TRIES편까지 시도해, 그 편 기준 체류 창에 연결편이 없으면
    다음 편으로 넘어간다(가장 이른 편 하나만 보고 포기하던 문제 해결).

    min_gap: 둘째 다리는 첫 다리 도착 + min_gap 이후 출발이어야 함(관광 체류 하한).
    max_gap: 첫 다리 도착~둘째 다리 출발 간격이 max_gap을 넘으면 버림(체류 상한 초과).
    관광 경유(_stopover)는 이 gap을 MIN_STAY~MAX_STAY(2~6h)로 넘겨 "체류 성립" 판정에 재사용한다.
    """
    for legs1 in _leg_options(dep, via, ymd, start, groups, nail_pass, VIA_LEG_TRIES):
        via_arr = legs1[-1]["arr_time"]
        opts2 = _leg_options(via, arr, ymd, via_arr + min_gap, groups, nail_pass)
        if not opts2:
            return None  # 경유→도착 구간 자체가 없음 — 첫 다리를 바꿔도 소용없다
        legs2 = opts2[0]
        if legs2[0]["dep_time"] - via_arr > max_gap:
            continue     # 이 첫 다리로는 체류가 너무 길어짐 → 더 늦은 첫 다리로 재시도
        return legs1, legs2
    return None


def _transfer_via_group(dep, arr, group, ymd, start, nail_pass=False):
    """클러스터(group) 안에서 출발→[하차역]→[승차역]→도착 환승을 찾는다.

    하차역≠승차역(서울역 도착→용산역 출발)이면 이동 버퍼를 더한다.
    반환: (leg1, leg2, label, total) | None
    """
    members = [s for s in group if s.station_idx not in (dep.station_idx, arr.station_idx)]
    best = None
    for a in members:  # 하차역
        leg1 = _earliest(_legs(dep.nat_code, a.nat_code, ymd, nail_pass), start)
        if not leg1:
            continue
        for d in members:  # 승차역
            gap = TRANSFER_MIN if a.station_idx == d.station_idx else CLUSTER_MOVE
            leg2 = _earliest(_legs(d.nat_code, arr.nat_code, ymd, nail_pass), leg1["arr_time"] + gap)
            if not leg2:
                continue
            if leg2["dep_time"] - leg1["arr_time"] > TRANSFER_MAX:
                continue
            total = (leg2["arr_time"] - leg1["dep_time"]).total_seconds()
            label = a.station_name if a.station_idx == d.station_idx else f"{a.station_name}·{d.station_name}"
            if best is None or total < best[3]:  # 총 소요(승차+환승대기) 최소인 하차·승차 조합 채택
                best = (leg1, leg2, label, total)
    return best


def _journey(dep: Station, arr: Station, ymd: str, start: datetime, groups: list[list[Station]], nail_pass=False):
    """단일 여정을 직통 우선, 없으면 거점 클러스터 환승으로 구한다.

    반환: (picks: list[dict], via_label: str|None, note: str|None)
    """
    direct = _earliest(_legs(dep.nat_code, arr.nat_code, ymd, nail_pass), start)
    if direct:
        return [direct], None, None

    best = None  # (total, leg1, leg2, label)
    for group in groups:
        r = _transfer_via_group(dep, arr, group, ymd, start, nail_pass)
        if r and (best is None or r[3] < best[0]):
            best = (r[3], r[0], r[1], r[2])
    if best:
        _, leg1, leg2, label = best
        return [leg1, leg2], label, f"{label} 환승"
    return [], None, "경로를 찾지 못했습니다."


def _leg_options(a: Station, b: Station, ymd: str, start: datetime, groups: list[list[Station]],
                 nail_pass: bool = False, limit: int = 1) -> list[list[dict]]:
    """a→b 한 '다리'의 후보 여정을 출발 이른 순으로 최대 limit개.

    직통이 있으면 직통 편들을 각각 후보로 내고(첫 다리를 여러 편 시도하기 위함), 직통이 아예
    없으면 거점 클러스터 환승(_journey)으로 이은 1건을 후보로 낸다. 각 후보는 열차 dict 목록
    (직통 1편, 환승 2편)이다.

    경유 다리에 환승을 허용하는 지점 — 예전엔 직통만 봐서 '강릉→부산'처럼 직통이 없는 구간이
    끼면 경유 경로가 통째로 사라지고 직통 왕복만 남았다.
    """
    direct = sorted(
        (t for t in _legs(a.nat_code, b.nat_code, ymd, nail_pass) if t["dep_time"] >= start),
        key=lambda t: t["dep_time"],
    )
    if direct:
        return [[t] for t in direct[:limit]]
    picks, _, _ = _journey(a, b, ymd, start, groups, nail_pass)
    return [picks] if picks else []


def _via_groups(groups: list[list[Station]], *stations: Station) -> list[list[Station]]:
    """경유 다리에 쓸 환승 거점 — 출발역·도착역이 속한 클러스터를 통째로 제외한다.

    경유 다리가 출발역을 거치면 "서울 출발 → 강릉 → (서울) → 부산"처럼 이미 지나온 곳으로
    되돌아갔다가 다시 나가는 경로가 된다. 도착역도 마찬가지로 목적지에 닿았다가 되나오는 꼴이라 막는다.
    클러스터 단위로 빼는 이유: 서울·용산·청량리는 같은 도시의 다른 터미널이라, 청량리 환승은
    사실상 서울 환승이다(역 하나만 빼면 옆 터미널로 그대로 새어 나간다).
    """
    block = {s.station_idx for s in stations}
    return [g for g in groups if not any(m.station_idx in block for m in g)]


def _transfer_label(legs: list[dict]) -> str:
    """환승 다리(열차 2편)의 환승역 표기. 하차역≠승차역이면 '청량리·서울'처럼 둘 다 적는다."""
    off, on = legs[0]["arr_station"], legs[1]["dep_station"]
    return off if off == on else f"{off}·{on}"


def _via_travel_ok(route: RouteCandidate, main: RouteCandidate | None) -> bool:
    """경유 경로의 총 이동시간이 기본 왕복(main) 대비 상한 이내인지.

    환승 허용의 대가로 "출발→경유→(왔던 길)→도착"처럼 되돌아가는 연결도 성립하므로,
    직통 대비 MAX_VIA_TRAVEL_RATIO배(짧은 노선 보호용 +MIN_VIA_TRAVEL_SLACK분) 안쪽만 남긴다.
    """
    base = main.total_travel_minutes if main is not None else 0
    if base <= 0:
        return True
    return route.total_travel_minutes <= max(base * MAX_VIA_TRAVEL_RATIO, base + MIN_VIA_TRAVEL_SLACK)


def _via_hours_ok(route: RouteCandidate) -> bool:
    """경유 경로에 심야 이동이 끼지 않았는지 — 새벽 출발/도착 열차가 하나라도 있으면 탈락.

    환승 다리는 연결만 맞으면 몇 시간을 기다린 뒤 막차/첫차를 타게 되어, 실제로 '동대구 00:28
    출발 → 부산 01:3x 도착' 같은 후보가 나온다. 관광 목적의 경유로는 쓸 수 없어 여기서 거른다.
    """
    for t in list(route.go_trains) + list(route.back_trains):
        if not (NIGHT_END <= t.dep_time.hour < NIGHT_START):
            return False
        if NIGHT_LATE_ARR <= t.arr_time.hour < NIGHT_END:
            return False
    return True


def _via_miss_note(dep: Station, arr: Station, via: Station, ymd: str, start: datetime,
                   groups: list[list[Station]], nail_pass: bool, kind: str) -> str:
    """지정 경유가 성립하지 않은 이유를 구간별로 구분한 안내 문구.

    "체류 조건에 맞는 열차가 없다"로 뭉뚱그리면 실제 원인(구간 열차 자체가 없음)을 알 수 없어
    어느 쪽 다리가 끊겼는지 짚어준다. 조회는 전부 lru_cache 히트라 추가 API 콜이 없다.
    """
    if not _leg_options(dep, via, ymd, start, groups, nail_pass):
        return f"{dep.station_name}→{via.station_name} 구간에 운행 열차가 없어 {via.station_name} 경유를 제외했습니다."
    if not _leg_options(via, arr, ymd, start, groups, nail_pass):
        return f"{via.station_name}→{arr.station_name} 구간에 운행 열차가 없어 {via.station_name} 경유를 제외했습니다."
    return f"{via.station_name} 경유는 {kind}에 맞는 연결편이 없어 제외했습니다."


def _resolve_groups(by_name: dict) -> list[list[Station]]:
    """이름 기반 TRANSFER_GROUPS를 DB에 있는 Station 객체 그룹으로 변환(nat_code 보유분만)."""
    out = []
    for names in TRANSFER_GROUPS:
        members = [by_name[n] for n in names if n in by_name and by_name[n].nat_code]
        if members:
            out.append(members)
    return out


def _candidate_stops(all_stations: list[Station], dep: Station, arr: Station) -> list[Station]:
    """관광 경유 후보(지리적 중간역). KTX·근거리 우선 상위 N."""
    if not (_has_coords(dep) and _has_coords(arr)):
        return []
    base = _haversine(dep, arr)
    if base == 0:
        return []
    min_leg = max(MIN_LEG_KM, base * MIN_LEG_RATIO)  # 출발·도착에서 이만큼은 떨어져야 함
    scored = []
    for s in all_stations:
        if s.station_idx in (dep.station_idx, arr.station_idx):
            continue
        if not s.nat_code or not _has_coords(s):
            continue
        leg_dep, leg_arr = _haversine(dep, s), _haversine(s, arr)
        if leg_dep < min_leg or leg_arr < min_leg:   # 출발/도착에 너무 붙은 역 제외(예: 서울→용산)
            continue
        if leg_dep + leg_arr <= base * MAX_DETOUR_RATIO:
            scored.append((leg_dep + leg_arr, s))
    scored.sort(key=lambda x: (0 if x[1].is_ktx else 1, x[0]))  # KTX 먼저, 그 안에서 우회거리(leg합) 짧은 순
    return [s for _, s in scored[:MAX_CANDIDATES]]


def _build(route_type, dep, via_label, arr, go_picks, stay, back_segs, note=None, return_via=False) -> RouteCandidate:
    go_trains = [_to_train(t) for t in go_picks]
    all_segs = go_trains + back_segs  # 가는편(경유면 2편) + 오는편 전 구간
    travel = sum(t.duration_minutes for t in all_segs)  # 순수 승차시간 합(경유 체류는 stay_minutes로 별도)
    # 총 운임 = 성인 1인 기준 전 구간 편도 요금의 합(=왕복 합). 한 구간이라도 요금이
    # 없으면(None) 합산이 부정확해지므로 total_fare 자체를 None으로 둔다. 인원수·할인 미반영.
    fares = [t.fare for t in all_segs]
    total_fare = sum(fares) if all_segs and all(f is not None for f in fares) else None
    # 경로 표기만으로 방향이 드러나게 한다(별도 필드 없이): 가는편 경유=출발→via→도착,
    # 오는편 경유=왕복(출발→도착→via→출발), 그 외=출발→도착. 코스/프론트는 path·기차 시각으로 방향을 안다.
    if return_via and via_label:
        names = [dep.station_name, arr.station_name, via_label, dep.station_name]
    elif via_label:
        names = [dep.station_name, via_label, arr.station_name]
    else:
        names = [dep.station_name, arr.station_name]
    return RouteCandidate(
        route_type=route_type,
        path="→".join(names),
        go_trains=go_trains,
        stay_minutes=stay,
        back_trains=back_segs,
        total_travel_minutes=travel,
        total_fare=total_fare,
        note=note,
    )


def _assemble_stopover(dep, arr, via, legs1, legs2, *, carry, note, return_via, nights=None) -> RouteCandidate | None:
    """경유 앞/뒤 다리(legs1·legs2, 각각 열차 dict 목록)로 경유 RouteCandidate를 조립.

    4개 경유 빌더의 공통 꼬리. 각 다리는 직통 1편일 수도, 환승 2편일 수도 있다.
    return_via=False(가는편 경유): 가는편이 legs1+legs2, 오는편은 carry(호출측 back_segs).
    return_via=True(오는편 경유): 가는편은 carry(호출측 go_picks), 오는편이 legs1+legs2.
    stay=legs1 도착~legs2 출발(분). nights를 주면 숙박 경유로 via_nights를 붙인다.
    go/back에 따라 leg가 어느 쪽으로 가는지가 헷갈리기 쉬워 이 배치를 한 곳에 모은다.
    """
    # carry(반대 방향 여정)가 비면 왕복이 성립 안 하는 반쪽 경유 → 폐기(호출부는 None을 걸러낸다).
    # 도착역에서 돌아오는 열차가 없을 때 back_trains=[] 인 채 경유가 만들어져 _overnight_segments가
    # back[0]에서 IndexError 나던 것을 원천 차단(4개 빌더 공통 꼬리라 여기 한 곳으로 커버).
    if not carry:
        return None
    via_arr, via_dep = legs1[-1]["arr_time"], legs2[0]["dep_time"]
    stay = int((via_dep - via_arr).total_seconds() // 60)
    picks = list(legs1) + list(legs2)
    if return_via:
        go_trains, back_segs = carry, [_to_train(t) for t in picks]
    else:
        go_trains, back_segs = picks, carry
    # 다리 안에서 갈아탄 역을 note에 남긴다 — path는 출발→경유→도착만 적어 환승이 안 드러난다.
    # 클러스터 환승은 하차역≠승차역일 수 있어(청량리 하차→서울 승차) 다르면 둘 다 적는다.
    xfer = [_transfer_label(legs) for legs in (legs1, legs2) if len(legs) > 1]
    if xfer:
        note = " / ".join(n for n in (note, f"{'·'.join(xfer)} 환승") if n)
    # 경로 표기에 숙박 여부를 넣는다 — 같은 경유역이라도 '3시간 들렀다 가기'와 '하루 자고 가기'는
    # 전혀 다른 여정인데, 박수를 안 적으면 둘 다 "서울역→동대구역→부산역"이 되어 카드가 겹쳐 보인다.
    via_label = f"{via.station_name}({nights}박)" if nights else via.station_name
    route = _build("경유", dep, via_label, arr, go_trains, stay, back_segs, note, return_via=return_via)
    route.via_station_idx = via.station_idx
    # 경유역 체류 시간대를 '위치(인덱스)'가 아니라 값으로 실어 보낸다. 다리에 환승이 끼면
    # go_trains[0]/[1]이 더 이상 경유 도착/출발이 아니므로, 소비자(recommend_service·itinerary)는
    # 반드시 이 두 필드를 봐야 한다.
    route.via_arr_time, route.via_dep_time = via_arr, via_dep
    if nights is not None:
        route.via_nights = nights
    return route


def _stopover(dep, arr, via, go_date, go_start, back_segs, back_note, groups, nail_pass) -> RouteCandidate | None:
    """via 역에 2~6h 관광 체류하는 가는편 경유 경로 1건. 체류 조건에 맞는 열차가 없으면 None."""
    # _via_pair가 MIN_STAY~MAX_STAY로 걸러 체류는 이미 2~6h 범위 안이다.
    pair = _via_pair(dep, arr, via, go_date, go_start, MIN_STAY, MAX_STAY, groups, nail_pass)
    if not pair:
        return None
    return _assemble_stopover(dep, arr, via, *pair, carry=back_segs, note=back_note, return_via=False)


def _stopover_return(dep, arr, via, back_date, back_start, go_picks, go_note, groups, nail_pass) -> RouteCandidate | None:
    """오는편에 via 역 2~6h 관광 체류하는 경유 경로 1건(_stopover의 오는편 대칭).

    가는편은 직통/환승(go_picks 재사용), 오는편을 도착→via→출발 2구간으로 만들고 그 사이 체류.
    체류 조건에 맞는 열차가 없거나 가는편이 없으면 None.
    """
    if not go_picks:  # 가는편이 없으면 왕복 자체가 성립 안 함
        return None
    pair = _via_pair(arr, dep, via, back_date, back_start, MIN_STAY, MAX_STAY, groups, nail_pass)
    if not pair:
        return None
    return _assemble_stopover(dep, arr, via, *pair, carry=go_picks, note=go_note, return_via=True)


def _stopover_overnight(dep, arr, via, go_date, nights, go_start, back_segs, back_note, groups, nail_pass) -> RouteCandidate | None:
    """가는편에 via 역에서 nights박 자고 다음날 목적지로 가는 '날짜 넘는' 경유 경로 1건.

    legs1: 출발→via (go_date, 희망 출발시각 이후 최이른 다리), legs2: via→도착 (go_date+nights,
    _NEXT_DAY_START 이후 최이른 다리 → 아침에 이동해 목적지 그 날을 확보). 당일치기 _stopover와 달리
    2~6h 체류 제약이 없다(하룻밤 넘김). 각 다리는 직통 또는 환승. 하나라도 못 찾으면 None.
    """
    date2 = (_parse_date(go_date) + timedelta(days=nights)).strftime("%Y%m%d")
    opts1 = _leg_options(dep, via, go_date, go_start, groups, nail_pass)
    if not opts1:
        return None
    opts2 = _leg_options(via, arr, date2, _start_dt(_parse_date(date2), _NEXT_DAY_START), groups, nail_pass)
    if not opts2:
        return None
    return _assemble_stopover(dep, arr, via, opts1[0], opts2[0], carry=back_segs, note=back_note, return_via=False, nights=nights)


def _stopover_overnight_return(dep, arr, via, back_date, nights, go_picks, go_note, groups, nail_pass) -> RouteCandidate | None:
    """오는편에 via 역에서 nights박 자고 귀가하는 '날짜 넘는' 경유 경로 1건(_stopover_overnight의 대칭).

    가는편은 직통/환승(go_picks 재사용). 오는편 legs1: 도착역→via (back_date-nights, 그 날 아침 이후
    이동해 경유역서 관광·숙박), legs2: via→출발역 (back_date, _NEXT_DAY_START 이후 최이른 다리).
    귀가는 back_date로 고정돼 여행 창을 넘지 않는다(경유 1박이 마지막 밤을 대체). 못 찾으면 None.
    """
    if not go_picks:  # 가는편이 없으면 왕복 자체가 성립 안 함
        return None
    date1 = (_parse_date(back_date) - timedelta(days=nights)).strftime("%Y%m%d")
    opts1 = _leg_options(arr, via, date1, _start_dt(_parse_date(date1), _NEXT_DAY_START), groups, nail_pass)
    if not opts1:
        return None
    opts2 = _leg_options(via, dep, back_date, _start_dt(_parse_date(back_date), _NEXT_DAY_START), groups, nail_pass)
    if not opts2:
        return None
    return _assemble_stopover(dep, arr, via, opts1[0], opts2[0], carry=go_picks, note=go_note, return_via=True, nights=nights)


def recommend(
    db: Session,
    dep_idx: int,
    arr_idx: int,
    go_date: str,
    back_date: str,
    go_time: str | None = None,
    back_time: str | None = None,
    nail_pass: bool = False,
    via_station_idx: int | None = None,
    via_nights: int = 0,
) -> list[RouteCandidate]:
    """직통/환승/경유 왕복 경로 후보를 반환한다. 기본 왕복은 항상 첫 번째로 보장된다.

    nail_pass=True면 내일로패스 적용 열차(KTX 계열·SRT 제외)로만 경로를 구성한다.
    via_station_idx가 주어지면 그 역을 2~6h 관광 체류로 경유하는 경로를 제공한다. 이때 가는편 경유
    (출발→지정역→도착)와 오는편 경유(도착→지정역→출발)를 둘 다 시도해 성립하는 편을 모두 낸다
    (자동 중간역 경유는 만들지 않음). 미지정이면 지리적 중간역을 자동 선정하고, 각 후보마다 가는편·오는편
    경유를 둘 다 만든다(최종 상위 N개 선별은 recommend_service 몫).
    via_nights>=1이면 '날짜 넘는 경유'(경유역에서 nights박 자고 이동)로 제공한다(지정/자동 공통).
    가는편(자고 목적지로)·오는편(목적지서 자러 갔다 귀가) 둘 다 시도해 성립분을 낸다. 이땐 당일치기
    2~6h 경유는 만들지 않는다. nights가 여행 일수를 넘어 목적지에 머물 날이 없으면 경유를 생략하고 note로 안내.
    (출발·도착역과 같으면 무시, 조건에 맞는 열차가 없으면 경유 없이 main note로 안내.)
    """
    go = _parse_date(go_date)
    back = _parse_date(back_date)
    if back.date() < go.date():
        raise BadRequestException("오는날은 가는날 이후여야 합니다.")
    go_start = _start_dt(go, go_time)
    back_start = _start_dt(back, back_time)
    # 경유역 숙박(날짜 넘는 경유) 가능 여부: nights박 자고 다음날 이동해도 목적지에 최소 하루가
    # 남아야 한다(go+nights < back). 넘으면 숙박 경유는 생략(당일치기 경유로 폴백).
    overnight = via_nights >= 1 and (go + timedelta(days=via_nights)).date() < back.date()

    dep = _station(db, dep_idx, "출발")
    arr = _station(db, arr_idx, "도착")

    # 사용자 지정 경유역. 0/미지정/미존재/철도 미지원·좌표없음이면 경유 없이 진행한다
    # (잘못된 경유값 하나 때문에 직통·환승 등 열차 정보를 통째로 잃지 않도록 예외를 던지지 않는다).
    via = None
    if via_station_idx and via_station_idx not in (dep.station_idx, arr.station_idx):
        cand = station_dao.get_by_idx(db, via_station_idx)
        if cand is not None and cand.nat_code and _has_coords(cand):
            via = cand

    all_stations = station_dao.get_stations(db)
    by_name = {s.station_name: s for s in all_stations}
    groups = _resolve_groups(by_name)

    # 필요한 모든 구간을 병렬 prefetch → 이후 순차 로직은 캐시 히트로 즉답
    # 경유역 지정 시엔 그 역만 경유하므로 지리적 중간역 자동 후보는 만들지 않는다.
    stops = [] if via is not None else _candidate_stops(all_stations, dep, arr)
    prefetch_stops = stops + ([via] if via is not None else [])
    _prefetch_segments(dep, arr, groups, prefetch_stops, go_date, back_date)

    def _prefetch_overnight(targets: list[Station]) -> None:
        # 숙박 경유는 날짜 넘는 구간이 있어 그 날짜들을 별도로 데운다(나머지 방향은 _prefetch_segments가 이미 warm).
        #   가는편 leg2: 경유→도착 (go_date+nights) / 오는편 leg1: 도착→경유 (back_date-nights)
        if not targets:
            return
        date2 = (go + timedelta(days=via_nights)).strftime("%Y%m%d")
        date1 = (back - timedelta(days=via_nights)).strftime("%Y%m%d")
        extra = [(c.nat_code, arr.nat_code, date2) for c in targets]
        extra += [(arr.nat_code, c.nat_code, date1) for c in targets]
        with ThreadPoolExecutor(max_workers=max(1, min(16, len(extra)))) as ex:
            list(ex.map(lambda p: _safe_fetch(*p), extra))

    if overnight:
        _prefetch_overnight([via] if via is not None else stops)

    # 오는편(공통): 도착→출발, 직통 or 환승
    back_picks, back_via, _ = _journey(arr, dep, back_date, back_start, groups, nail_pass)
    back_segs = [_to_train(t) for t in back_picks]
    back_note = (
        f"오는편 {back_via} 환승" if back_via
        else (None if back_picks else "오는편 경로를 찾지 못했습니다.")
    )

    # 1) 기본 왕복: 가는편 직통 or 환승 (무조건 리턴 보장의 바닥)
    go_picks, go_via, _ = _journey(dep, arr, go_date, go_start, groups, nail_pass)
    main_type = "환승" if go_via else "직통"
    go_note = (
        f"가는편 {go_via} 환승" if go_via
        else (None if go_picks else "가는편 경로를 찾지 못했습니다.")
    )
    main_note = " / ".join(n for n in (go_note, back_note) if n) or None
    main = _build(main_type, dep, go_via, arr, go_picks, None, back_segs, main_note)

    def _stopovers_for(candidates: list[Station]) -> list[RouteCandidate]:
        """경유역 후보들로 가는편·오는편 경유를 모두 만들어 성립분을 이동시간 짧은 순으로 낸다.

        지정 경유(단일 후보)와 자동 경유(중간역 여럿) 양쪽이 같은 조립 규칙을 쓰도록 한 곳에 모았다.
        환승 허용 이후에는 연결만 되면 크게 우회하는 경유도 성립하므로 _via_travel_ok로 상한을 건다.
        """
        out: list[RouteCandidate] = []
        # 경유 다리는 출발역·도착역 클러스터를 환승 거점으로 쓰지 않는다(되돌아가기 방지).
        vg = _via_groups(groups, dep, arr)
        for c in candidates:
            if overnight:
                # 숙박 경유: 가는편(자고 목적지로)·오는편(목적지서 자러 갔다 귀가) 둘 다 시도.
                built = (
                    _stopover_overnight(dep, arr, c, go_date, via_nights, go_start, back_segs, back_note, vg, nail_pass),
                    _stopover_overnight_return(dep, arr, c, back_date, via_nights, go_picks, go_note, vg, nail_pass),
                )
            else:
                # 당일치기 경유: 가는 길에 / 오는 길에 둘 다 후보로 낸다
                # (방향은 path·기차 시각으로 드러난다 — 별도 필드 없음).
                built = (
                    _stopover(dep, arr, c, go_date, go_start, back_segs, back_note, vg, nail_pass),
                    _stopover_return(dep, arr, c, back_date, back_start, go_picks, go_note, vg, nail_pass),
                )
            out.extend(r for r in built if r is not None and _via_travel_ok(r, main) and _via_hours_ok(r))
        out.sort(key=lambda r: r.total_travel_minutes)  # 기본 순서(테마 랭킹 전)
        return out

    # 2) 관광 경유(1곳)
    #    - 경유역(via) 지정 시: 그 역만 경유로 제공(모든 경유 루트 = 출발→지정역→도착).
    #      성립하지 않으면 사유를 main.note에 남기고 자동 중간역 경유로 폴백한다.
    #    - 미지정 시: 지리적 중간역을 자동 선정(출발·도착에 붙은 역 제외)해 가는편·오는편 경유를 모두 제공.
    #      최종 상위 N개 선별은 '테마 관련도' 기준으로 recommend_service가 한다(여기선 지리·시각표만).
    if via is not None:
        stopovers = _stopovers_for([via])
        if stopovers:
            return [main] + stopovers
        # 지정 경유 실패 — 원인을 구간별로 구분해 안내하고, 경유 카드가 0장이 되지 않도록
        # 자동 중간역 경유로 폴백한다(예전엔 여기서 직통만 남아 세 카드가 전부 같은 직통이 됐다).
        kind = f"{via_nights}박 숙박 경유" if overnight else "2~6시간 체류 조건"
        miss = _via_miss_note(dep, arr, via, go_date, go_start, _via_groups(groups, dep, arr), nail_pass, kind)
        fallback = _candidate_stops(all_stations, dep, arr)
        if fallback:
            _prefetch_segments(dep, arr, [], fallback, go_date, back_date)  # 거점은 이미 warm
            if overnight:
                _prefetch_overnight(fallback)
            stopovers = _stopovers_for(fallback)
        if stopovers:
            miss = f"{miss} 대신 가는 길의 다른 역 경유를 추천합니다."
        # 이 호출 안에서는 사실이지만, 호출부가 당일치기/숙박 결과를 합치면 어긋날 수 있다
        # (당일치기는 실패해도 숙박 경유로 그 역을 가는 경우). 잘라낼 수 있게 핸들로도 남긴다.
        main.via_miss_note = miss
        main.note = " / ".join(n for n in (main.note, miss) if n) or None
        return [main] + stopovers

    return [main] + _stopovers_for(stops)
