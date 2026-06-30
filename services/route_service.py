"""기차 경로 추천 엔진 v3.

출발/도착/가는날/오는날만으로 직통·환승·경유 왕복 경로를 생성한다.
- 노드: station DB (좌표·등급)  - 엣지: 구간 API(train_api, (역,날짜) 캐시)

연결성(환승)과 관광(경유)을 분리한다:
  · 직통        : 1열차
  · 환승 연결   : 직통이 없을 때 철도 거점에서 갈아타기 → 연결성 확보
  · 관광 경유   : 지리적 중간역에 길게(2h~6h) 체류 → 관광 (다음 단계 TourAPI가 활용)

환승 거점은 "클러스터"로 둔다. 서울·용산·청량리는 한 도시의 다른 터미널이므로
한 그룹으로 묶고 역간 이동 버퍼를 준다(강릉→서울 도착, 용산→여수 출발 같은 환승).
가는편/오는편 둘 다 직통이 없으면 환승으로 잇는다.

무조건 리턴 보장: 가는편 기본 왕복(직통 or 환승)을 항상 첫 번째로 반환한다.
"""
import logging
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

# 내일로패스 미적용(제외): SRT만 (수서고속철도 운영, 코레일 아님). train_service와 동일 정책.
# KTX 계열은 횟수 제한은 있으나 탑승 가능하므로 포함한다.
_NAIL_EXCLUDED_PREFIX = ("SRT",)


def _nail_eligible(grade: str) -> bool:
    return not grade.startswith(_NAIL_EXCLUDED_PREFIX)


# 관광 경유(지리적 중간역에 체류)
MIN_STAY = timedelta(hours=2)
MAX_STAY = timedelta(hours=6)
MAX_DETOUR_RATIO = 1.4
MAX_CANDIDATES = 6
TOP_STOPOVERS = 3

# 환승 연결(철도 거점에서 갈아타기)
TRANSFER_MIN = timedelta(minutes=20)   # 같은 역 환승 최소 시간
CLUSTER_MOVE = timedelta(minutes=40)   # 같은 도시 다른 터미널 이동 버퍼(예: 서울역↔용산역)
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
    return datetime.combine(d.date(), t)


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


def _earliest(trains: tuple, not_before: datetime) -> dict | None:
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


def _via_pair(dep_nat, arr_nat, via_nat, ymd, start, min_gap, max_gap, nail_pass=False):
    """출발→경유→도착 2구간 연결(같은 역 환승/관광용). 간격 만족 시 (leg1, leg2)."""
    leg1 = _earliest(_legs(dep_nat, via_nat, ymd, nail_pass), start)
    if not leg1:
        return None
    leg2 = _earliest(_legs(via_nat, arr_nat, ymd, nail_pass), leg1["arr_time"] + min_gap)
    if not leg2:
        return None
    if leg2["dep_time"] - leg1["arr_time"] > max_gap:
        return None
    return leg1, leg2


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
            if best is None or total < best[3]:
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
    scored = []
    for s in all_stations:
        if s.station_idx in (dep.station_idx, arr.station_idx):
            continue
        if not s.nat_code or not _has_coords(s):
            continue
        detour = _haversine(dep, s) + _haversine(s, arr)
        if detour <= base * MAX_DETOUR_RATIO:
            scored.append((detour, s))
    scored.sort(key=lambda x: (0 if x[1].is_ktx else 1, x[0]))
    return [s for _, s in scored[:MAX_CANDIDATES]]


def _build(route_type, dep, via_label, arr, go_picks, stay, back_segs, note=None) -> RouteCandidate:
    go_trains = [_to_train(t) for t in go_picks]
    all_segs = go_trains + back_segs
    travel = sum(t.duration_minutes for t in all_segs)
    fares = [t.fare for t in all_segs]
    total_fare = sum(fares) if all_segs and all(f is not None for f in fares) else None
    names = [dep.station_name] + ([via_label] if via_label else []) + [arr.station_name]
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


def recommend(
    db: Session,
    dep_idx: int,
    arr_idx: int,
    go_date: str,
    back_date: str,
    go_time: str | None = None,
    back_time: str | None = None,
    nail_pass: bool = False,
) -> list[RouteCandidate]:
    """직통/환승/경유 왕복 경로 후보를 반환한다. 기본 왕복은 항상 첫 번째로 보장된다.

    nail_pass=True면 내일로패스 적용 열차(KTX 계열·SRT 제외)로만 경로를 구성한다.
    """
    go = _parse_date(go_date)
    back = _parse_date(back_date)
    if back.date() < go.date():
        raise BadRequestException("오는날은 가는날 이후여야 합니다.")
    go_start = _start_dt(go, go_time)
    back_start = _start_dt(back, back_time)

    dep = _station(db, dep_idx, "출발")
    arr = _station(db, arr_idx, "도착")

    all_stations = station_dao.get_stations(db)
    by_name = {s.station_name: s for s in all_stations}
    groups = _resolve_groups(by_name)

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

    # 2) 관광 경유(1곳): 지리적 중간역에 2~6h 체류
    stopovers = []
    for c in _candidate_stops(all_stations, dep, arr):
        pair = _via_pair(dep.nat_code, arr.nat_code, c.nat_code, go_date, go_start, MIN_STAY, MAX_STAY, nail_pass)
        if not pair:
            continue
        leg1, leg2 = pair
        stay = int((leg2["dep_time"] - leg1["arr_time"]).total_seconds() // 60)
        stopovers.append(_build("경유", dep, c.station_name, arr, [leg1, leg2], stay, back_segs, back_note))

    stopovers.sort(key=lambda r: r.total_travel_minutes)
    return [main] + stopovers[:TOP_STOPOVERS]
