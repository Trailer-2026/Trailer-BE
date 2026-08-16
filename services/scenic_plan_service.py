"""풍경 알림 시각표 — '이 열차가 몇 시쯤 어느 풍경을 지나는지'를 계산해 그 시각에 푸시한다.

**왜 서버가 보내나.** 전에는 앱이 좌표를 들고 `GET /api/scenic-spots/nearby`를 폴링할 때만
알림이 나갔다. 그 폴링 구독이 알림 탭 화면 하나에만 걸려 있었고 그 탭은 열어야 켜지는데,
탑승 중 사용자는 일정 탭에 있어서 **발송이 0건**이었다. 발송 주체를 서버로 옮기면 앱이
어느 화면에 있든, 꺼져 있든 알림이 나간다. `/nearby`는 조회 전용으로 남았다.

**시각표를 저장하지 않는다.** 계산에 필요한 건 전부 이미 있는 참조 데이터다 —
정차 순서(`train_stop`), 구간별 풍경(`scenic_spot_segment`), 역 좌표(`station`). 게다가
결과가 (열차번호, 출발역, 도착역)만으로 정해져 탑승과 무관하므로, **여정 전체를 1로 둔
진행률(0~1)** 로 캐시해 두고 실제 시각은 탑승의 출발·도착 시각에 얹어 만든다. 그래서
1분마다 다시 계산해도 첫 계산 이후 DB를 거의 건드리지 않는다. 대신 '이미 보냈는지'는
어딘가 남아야 하는데, 그것만 `notification_log`가 맡는다(SCENERY 이력).

**시각은 어림짐작이다.** 중간역 통과 시각을 주는 데이터가 없어 역 간 직선거리에 비례해
소요 시간을 나눈다 — 정차 시간·가감속·선로 곡률이 전부 빠져 몇 분씩 틀린다. 그래서
알림 문구는 스팟이 아니라 구간 단위다("지금 대전역 스팟을 지나고 있어요"). 가시 범위가
1.5km라 KTX 기준 18초면 지나가는데, 그 창을 서버 추정만으로 맞추는 건 불가능하다.

**지연은 GPS로 잡는다.** 앱이 포그라운드에 올라올 때 좌표를 보내면(`calibrate`) 그 좌표를
경로에 투영해 '지금쯤이면 몇 시여야 하는지'를 구하고, 실제 시각과의 차를 그 탑승의 보정값
으로 기억한다. 이후 발송은 그만큼 밀린다. 보정값은 **프로세스 메모리에만** 둔다 — 재기동
하면 원래 예정 시각으로 돌아갈 뿐 알림이 끊기지는 않고, 단일 프로세스 배포를 전제한다
(다중 워커로 가면 워커마다 보정이 따로 논다. CLAUDE.md의 `SCENIC_PUSH_AUTOSYNC` 참조).

배치 컨텍스트에서는 요청 스코프가 아닌 자체 세션을 연다(train_departure_service와 같은 성격).
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from databases.daos import (
    notification_log_dao, schedule_dao, scenic_spot_dao, station_dao, ticket_dao,
    train_stop_dao, user_dao,
)
from databases.database import SessionLocal
from services import push_service
from utils import train_api
from utils.scenic import haversine_m, project_on_segment, scenery_image_url
from utils.timezone import now_kst

logger = logging.getLogger(__name__)

# 루프 주기. 발송 창(아래)보다 훨씬 짧아야 한 번 밀려도 놓치지 않는다.
CHECK_INTERVAL_SEC = 60
# 통과 예정 시각이 지난 뒤 이 시간 안이면 보낸다. 창을 두는 이유는 루프가 밀리거나
# 서버가 잠깐 꺼졌다 살아났을 때를 덮기 위해서고, 무한정 열지 않는 이유는 몇 시간 꺼져
# 있었다면 이미 지나간 구간의 "지금 지나고 있어요"가 재기동 때 몰아서 나가기 때문이다.
SEND_WINDOW_MINUTES = 10
# 알림 사이 최소 간격. 이보다 촘촘한 후보는 한 덩어리로 묶어 대표 하나만 보낸다 —
# 풍경이 밀집한 구간에서 알림이 연달아 터지는 걸 막는다.
MIN_GAP_MINUTES = 12
# GPS 보정: 사용자 좌표가 경로에서 이만큼 넘게 벗어나 있으면 매칭 실패로 보고 보정하지 않는다.
# (열차를 타고 있지 않거나, 좌표가 튀었거나, 다른 노선일 때)
MAX_MATCH_M = 20_000
# GPS 보정: 이 이상 차이가 나면 엉뚱한 구간에 매칭된 것으로 보고 무시한다.
MAX_OFFSET_MINUTES = 90
# 앱의 계획 조회가 미리 보여줄 범위 — 아직 안 탔어도 곧 탈 여정이면 계획을 내려준다.
UPCOMING_HOURS = 3
# 직접 입력 승차권의 열차번호를 되짚을 때 허용하는 출발 시각 오차(분).
# 사용자가 표를 보고 손으로 적는 값이라 1~2분쯤 어긋나기 쉬운데, 정확히 일치하는 열차만
# 찾으면 그 승차권은 풍경 알림을 통째로 못 받는다 — 그것도 조용히. 같은 구간에서 2분 안에
# 연달아 떠나는 열차는 사실상 없어, 이만큼 열어도 엉뚱한 열차를 고를 위험은 거의 없다.
TRAIN_MATCH_TOLERANCE_MINUTES = 2

# 경로 프로필 캐시 TTL. 정차 패턴·풍경 구간은 참조 데이터라 거의 안 바뀌지만,
# train_stop이 매일 전량 교체되므로 하루 안에는 새 적재분이 반영되게 둔다.
_PROFILE_TTL = timedelta(hours=6)
# GPS 보정값 수명 — 여정 하나를 덮을 만큼만. 오래된 보정이 다음 탑승에 새는 걸 막는다.
_OFFSET_TTL = timedelta(hours=6)

# (열차번호, 출발역, 도착역) → (프로필, 만든 시각). 프로세스 메모리 캐시.
_PROFILE_CACHE: dict[tuple[str, str, str], tuple["RouteProfile | None", datetime]] = {}
# 직접 입력 승차권은 열차번호가 없어 TAGO로 역추론한다. ticket_idx → (열차번호|None, 시각).
_TRAIN_NO_CACHE: dict[int, tuple[str | None, datetime]] = {}
# 탑승 키 → (보정값, 갱신 시각). 앱이 GPS를 보낼 때마다 갱신된다.
_OFFSETS: dict[tuple[str, int], tuple[timedelta, datetime]] = {}


@dataclass(frozen=True)
class Ride:
    """알림을 붙일 탑승 1건 — 추천 코스 승차권(schedule)이거나 직접 입력 승차권(ticket)이다.

    역명은 '대전역'처럼 접미사를 포함한 형식으로 통일한다(station·scenic_spot_segment와
    같은 표기). schedule.dep_station은 접미사가 없어 만들 때 붙인다.
    시각은 전부 naive KST wall-clock이다.
    """

    user_idx: int
    dep_station: str
    arr_station: str
    dep_at: datetime
    arr_at: datetime
    train_no: str | None = None
    travel_idx: int | None = None
    schedule_idx: int | None = None
    ticket_idx: int | None = None
    # 열차번호 역추론용(직접 입력 승차권만). 역 PK에서 TAGO 역코드를 끌어온다.
    dep_station_idx: int | None = None
    arr_station_idx: int | None = None

    @property
    def key(self) -> tuple[str, int]:
        """보정값 저장 키. 두 출처를 섞어 쓰지 않도록 종류를 함께 담는다."""
        if self.schedule_idx is not None:
            return ("schedule", self.schedule_idx)
        return ("ticket", self.ticket_idx)


@dataclass(frozen=True)
class SpotOnRoute:
    """경로 위 풍경 후보 1개 — 탑승과 무관한 순수 위치 정보라 그대로 캐시된다.

    progress는 여정 전체를 1로 뒀을 때의 진행률이다. 실제 통과 시각은 탑승의
    출발·도착 시각에 이 값을 얹어 만든다.
    """

    scenic_spot_idx: int
    name: str | None
    category: str
    side: str | None
    from_station: str
    to_station: str
    progress: float
    # 구간 직선에서 떨어진 거리(m). 한 덩어리에서 대표를 고를 때 '가장 잘 보이는 것' 기준.
    offset_m: float


@dataclass(frozen=True)
class RouteProfile:
    """한 열차편의 탑승 구간 프로필 — 역 시퀀스와 그 위 풍경 후보들.

    stations: (역명, 진행률, 위도, 경도). GPS 보정이 사용자 좌표를 투영할 대상이다.
    """

    stations: tuple[tuple[str, float, float, float], ...]
    spots: tuple[SpotOnRoute, ...]


# ─────────────────────────────── 발송(배치) ───────────────────────────────

def send_scenery_reminders(now: datetime | None = None) -> int:
    """지금 풍경을 지나는 사용자에게 알림을 보내고 발송 건수를 반환한다. main.py 루프가 부른다.

    now를 주면 그 시각 기준으로 판정한다(테스트용). 기본은 현재 KST다.

    한 여정의 계산이 실패해도(정차역 미적재·좌표 없음 등) 나머지는 그대로 진행한다 —
    풍경은 부가 기능이라 한 사람 때문에 배치가 멈추면 안 된다.
    """
    base = (now or now_kst()).replace(tzinfo=None)
    _prune_caches(base)
    db = SessionLocal()
    try:
        sent = 0
        for ride in _active_rides(db, base):
            try:
                sent += _send_for_ride(db, ride, base)
            except Exception as e:
                logger.warning("풍경 알림 계산 실패(건너뜀) ride=%s: %s", ride.key, e)
        return sent
    finally:
        db.close()


def _prune_caches(now: datetime) -> None:
    """수명이 다한 캐시 항목을 실제로 버린다. 루프가 돌 때마다 1회.

    각 조회 함수도 TTL을 확인하지만 그건 **다시 접근할 때만** 걸린다 — 어제 지나간
    열차 항목은 아무도 건드리지 않아 영원히 남는다. 서버가 몇 주씩 떠 있는 환경이라
    그대로 두면 느리게 새는 메모리가 된다.
    """
    for cache, ttl in (
        (_PROFILE_CACHE, _PROFILE_TTL),
        (_TRAIN_NO_CACHE, _PROFILE_TTL),
        (_OFFSETS, _OFFSET_TTL),
    ):
        for key in [k for k, (_, at) in cache.items() if now - at > ttl]:
            cache.pop(key, None)


def _send_for_ride(db: Session, ride: Ride, now: datetime) -> int:
    """한 탑승에서 지금 발송할 차례가 된 구간을 보낸다. 보낸 건수를 반환."""
    profile = _profile_for(db, ride)
    if profile is None:
        return 0

    items = _timetable(profile, ride, _offset_for(ride))
    # 창 안에 든 것만 남긴다 — 아직 이른 것, 한참 지난 것 모두 뺀다.
    due = [
        item for item in items
        if now - timedelta(minutes=SEND_WINDOW_MINUTES) <= item["eta"] <= now
    ]
    if not due:
        return 0

    # 이 탑승에서 이미 보낸 스팟은 건너뛴다. 창이 10분이라 같은 구간이 열 번 잡힌다.
    already = notification_log_dao.sent_scenic_spots(
        db, ride.user_idx, schedule_idx=ride.schedule_idx, ticket_idx=ride.ticket_idx,
    )
    due = [item for item in due if item["scenic_spot_idx"] not in already]
    if not due:
        return 0

    user = user_dao.get_by_idx(db, ride.user_idx)
    if user is None:
        return 0

    sent = 0
    for item in due:
        if push_service.notify_scenery(
            db, user,
            to_station=item["to_station"],
            scenic_spot_idx=item["scenic_spot_idx"],
            image_url=item["image_url"],
            travel_idx=ride.travel_idx,
            schedule_idx=ride.schedule_idx,
            ticket_idx=ride.ticket_idx,
        ):
            sent += 1
    return sent


# ─────────────────────────────── 조회·보정(요청) ───────────────────────────────

def get_plan(db: Session, user, now: datetime | None = None) -> dict:
    """지금 타고 있거나 곧 탈 여정의 풍경 시각표를 반환한다 (앱의 계획 조회).

    앱은 이 목록으로 알림 화면 상단 카드를 그리고, 원하면 로컬 알림을 미리 예약해
    서버 푸시보다 촘촘하게 안내할 수도 있다. 해당 여정이 없으면 ride=None, items=[]다.

    `delay_minutes`는 마지막 GPS 보정으로 확인된 지연(분)이다 — 양수면 늦게 가고 있다는 뜻.
    """
    base = (now or now_kst()).replace(tzinfo=None)
    ride = _current_ride_of_user(db, user.user_idx, base)
    if ride is None:
        return {"based_at": now_kst(), "ride": None, "delay_minutes": 0, "items": []}

    profile = _profile_for(db, ride)
    offset = _offset_for(ride)
    items = _timetable(profile, ride, offset) if profile else []

    already = notification_log_dao.sent_scenic_spots(
        db, ride.user_idx, schedule_idx=ride.schedule_idx, ticket_idx=ride.ticket_idx,
    )
    for item in items:
        item["is_sent"] = item["scenic_spot_idx"] in already

    return {
        "based_at": now_kst(),
        "ride": _ride_payload(ride),
        "delay_minutes": round(offset.total_seconds() / 60),
        "items": items,
    }


def calibrate(db: Session, user, lat: float, lng: float, now: datetime | None = None) -> dict:
    """현재 좌표로 지연을 계산해 남은 알림 시각을 밀고, 보정된 시각표를 반환한다.

    좌표를 경로의 역 구간들에 투영해 가장 가까운 구간을 찾고, 그 구간을 몇 %쯤 지났는지로
    '예정대로라면 지금은 몇 시' 를 구한다. 실제 시각과의 차가 지연이다.

    보정하지 않는 경우가 둘 있고, 그 때도 에러가 아니라 보정 없는 시각표를 그대로 준다
    (앱이 위치를 보내는 건 부가 요청이라 실패로 만들 이유가 없다).
      - 좌표가 경로에서 MAX_MATCH_M 넘게 벗어남 → 열차 밖이거나 좌표가 튄 것
      - 차이가 MAX_OFFSET_MINUTES를 넘음 → 엉뚱한 구간에 매칭된 것
    """
    base = (now or now_kst()).replace(tzinfo=None)
    ride = _current_ride_of_user(db, user.user_idx, base)
    if ride is not None:
        profile = _profile_for(db, ride)
        if profile is not None:
            scheduled = _scheduled_time_at(profile, ride, lat, lng)
            if scheduled is not None:
                delta = base - scheduled
                if abs(delta) <= timedelta(minutes=MAX_OFFSET_MINUTES):
                    _OFFSETS[ride.key] = (delta, base)
                    logger.info(
                        "풍경 알림 GPS 보정 ride=%s 지연=%+d분",
                        ride.key, round(delta.total_seconds() / 60),
                    )
    return get_plan(db, user, now)


def _scheduled_time_at(
    profile: RouteProfile, ride: Ride, lat: float, lng: float,
) -> datetime | None:
    """좌표가 경로의 어디쯤인지 찾아 '예정대로라면 지금은 몇 시'를 돌려준다. 못 찾으면 None.

    보정값이 아니라 **원래 예정 시각**을 기준으로 계산한다 — 그래야 지연이 누적이 아니라
    절댓값으로 갱신돼, 보정을 여러 번 해도 값이 발산하지 않는다.
    """
    duration = ride.arr_at - ride.dep_at
    if duration <= timedelta(0) or len(profile.stations) < 2:
        return None

    best: tuple[float, float] | None = None  # (벗어난 거리, 진행률)
    for (_, from_p, from_lat, from_lng), (_, to_p, to_lat, to_lng) in zip(
        profile.stations, profile.stations[1:]
    ):
        ratio, off_m = project_on_segment(from_lat, from_lng, to_lat, to_lng, lat, lng)
        if best is None or off_m < best[0]:
            best = (off_m, from_p + ratio * (to_p - from_p))

    if best is None or best[0] > MAX_MATCH_M:
        return None
    return ride.dep_at + duration * best[1]


def _ride_payload(ride: Ride) -> dict:
    return {
        "travel_idx": ride.travel_idx,
        "schedule_idx": ride.schedule_idx,
        "ticket_idx": ride.ticket_idx,
        "dep_station": ride.dep_station,
        "arr_station": ride.arr_station,
        "dep_at": ride.dep_at,
        "arr_at": ride.arr_at,
    }


# ─────────────────────────────── 시각표 만들기 ───────────────────────────────

def _timetable(profile: RouteProfile, ride: Ride, offset: timedelta) -> list[dict]:
    """진행률 프로필에 탑승의 실제 시각을 얹어 발송 목록을 만든다 (eta 오름차순).

    MIN_GAP_MINUTES보다 촘촘한 후보는 한 덩어리로 묶고 구간 직선에서 가장 가까운
    (=가장 잘 보이는) 하나만 대표로 남긴다. 솎는 기준이 시간이라 캐시된 프로필에서는 할 수
    없다 — 같은 열차편이라도 여정 길이는 탑승마다 다르기 때문이다.
    """
    duration = ride.arr_at - ride.dep_at
    if duration <= timedelta(0):
        return []

    gap = timedelta(minutes=MIN_GAP_MINUTES)
    items: list[dict] = []
    group: list[tuple[datetime, SpotOnRoute]] = []

    def flush() -> None:
        if not group:
            return
        eta, spot = min(group, key=lambda pair: pair[1].offset_m)
        items.append({
            "scenic_spot_idx": spot.scenic_spot_idx,
            "name": spot.name,
            "category": spot.category,
            "side": spot.side,
            "from_station": spot.from_station,
            "to_station": spot.to_station,
            "eta": eta,
            "image_url": scenery_image_url(spot.category),
        })

    for spot in profile.spots:  # 프로필은 진행률 오름차순으로 만들어 둔다
        eta = ride.dep_at + duration * spot.progress + offset
        if group and eta - group[0][0] >= gap:
            flush()
            group = []
        group.append((eta, spot))
    flush()
    return items


def _offset_for(ride: Ride) -> timedelta:
    """그 탑승의 GPS 보정값. 없거나 오래됐으면 0(예정대로)."""
    cached = _OFFSETS.get(ride.key)
    if cached is None:
        return timedelta(0)
    delta, at = cached
    if now_kst().replace(tzinfo=None) - at > _OFFSET_TTL:
        _OFFSETS.pop(ride.key, None)
        return timedelta(0)
    return delta


# ─────────────────────────────── 경로 프로필 ───────────────────────────────

def _profile_for(db: Session, ride: Ride) -> RouteProfile | None:
    """탑승 구간의 프로필을 캐시에서 꺼내거나 새로 만든다. 만들 수 없으면 None.

    캐시 키에 탑승 정보가 없다는 게 핵심이다 — 결과가 (열차번호, 출발역, 도착역)만으로
    정해지므로 같은 열차편을 타는 모든 사용자·모든 날짜가 계산 한 번을 나눠 쓴다.
    실패(None)도 캐시한다 — 정차역이 없는 열차(SRT·임시열차)를 매 분 다시 조회하지 않기 위해서다.
    """
    train_no = ride.train_no or _infer_train_no(db, ride)
    if not train_no:
        return None

    key = (train_no, ride.dep_station, ride.arr_station)
    cached = _PROFILE_CACHE.get(key)
    now = now_kst().replace(tzinfo=None)
    if cached is not None and now - cached[1] <= _PROFILE_TTL:
        return cached[0]

    profile = _build_profile(db, train_no, ride.dep_station, ride.arr_station)
    _PROFILE_CACHE[key] = (profile, now)
    return profile


def _build_profile(
    db: Session, train_no: str, dep_station: str, arr_station: str,
) -> RouteProfile | None:
    """정차 순서·역 좌표·구간별 풍경을 모아 진행률 프로필을 만든다.

    거리는 역 간 직선(haversine) 누적이고, 소요 시간은 그 거리에 비례해 나뉜다 —
    정차 시간과 가감속을 모르니 이보다 나은 근사가 없다.

    **통과역도 시퀀스에 남긴다.** 정차역만 쓰면 KTX처럼 중간을 지나치는 열차는 역 사이가
    수십 km라 거리 근사가 거칠어지고, 그 사이에 등록된 풍경 구간을 좌표에 앉힐 기준점도
    사라진다. 정차 수를 세는 recommend_service._stops_between이 통과를 빼는 것과는 목적이 다르다.
    """
    rows = train_stop_dao.get_stops_for(db, {train_no}).get(train_no)
    stop_names = _slice_stops(rows, dep_station, arr_station)
    if stop_names is None:
        return None

    # 좌표가 없는 역은 빼고 앞뒤를 잇는다 — 거리 근사가 조금 거칠어질 뿐, 그 역 때문에
    # 프로필 전체를 버릴 이유는 없다. 역마다 조회하면 KTX 노선 하나에 쿼리가 30~40번
    # 나가므로 IN 한 방으로 받는다.
    coords = station_dao.coords_by_names(db, stop_names)
    located = [
        (name, coords[name]) for name in stop_names if name in coords
    ]
    if len(located) < 2:
        logger.debug("풍경 프로필: 좌표 있는 역이 부족하다 train=%s", train_no)
        return None

    progress = _cumulative_progress([coord for _, coord in located])
    if progress is None:
        return None
    stations = tuple(
        (name, progress[i], coord[0], coord[1])
        for i, (name, coord) in enumerate(located)
    )
    progress_by_station = {name: (p, lat, lng) for name, p, lat, lng in stations}

    spots = _spots_on_route(db, progress_by_station)
    if not spots:
        return None
    return RouteProfile(stations=stations, spots=spots)


def _spots_on_route(
    db: Session, progress_by_station: dict[str, tuple[float, float, float]],
) -> tuple[SpotOnRoute, ...]:
    """경로 위 풍경 구간을 훑어 스팟마다 진행률과 창밖 좌/우를 붙인다 (진행률 오름차순)."""
    rows = scenic_spot_dao.segments_on_route(db, list(progress_by_station))
    out: list[SpotOnRoute] = []
    for seg, spot in rows:
        a, b = progress_by_station[seg.from_station], progress_by_station[seg.to_station]
        if a[0] == b[0]:
            continue  # 같은 지점(중복 역명 등) — 방향도 위치도 정할 수 없다
        # 진행률이 작은 쪽이 이 열차의 진행 방향 기준 '시작역'이다. segment는 등록 방향이
        # 따로 있어(from→to) 좌/우는 그 등록 방향과 비교해 정한다.
        forward = a[0] < b[0]
        from_station = seg.from_station if forward else seg.to_station
        to_station = seg.to_station if forward else seg.from_station
        (from_p, from_lat, from_lng) = a if forward else b
        (to_p, to_lat, to_lng) = b if forward else a

        ratio, off_m = project_on_segment(
            from_lat, from_lng, to_lat, to_lng, spot.lat, spot.lng
        )
        out.append(SpotOnRoute(
            scenic_spot_idx=spot.scenic_spot_idx,
            name=spot.name,
            category=spot.category,
            side=scenic_spot_dao.resolve_side(seg, from_station, to_station),
            from_station=from_station,
            to_station=to_station,
            progress=from_p + ratio * (to_p - from_p),
            offset_m=off_m,
        ))
    out.sort(key=lambda s: s.progress)
    return tuple(out)


def _cumulative_progress(coords: list[tuple[float, float]]) -> list[float] | None:
    """역 좌표 시퀀스를 누적 직선거리 비율(0~1)로 바꾼다. 총거리가 0이면 None."""
    cumulative = [0.0]
    for (lat1, lng1), (lat2, lng2) in zip(coords, coords[1:]):
        cumulative.append(cumulative[-1] + haversine_m(lat1, lng1, lat2, lng2))
    total = cumulative[-1]
    if total <= 0:
        return None
    return [d / total for d in cumulative]


def _slice_stops(rows, dep_station: str, arr_station: str) -> list[str] | None:
    """정차 시퀀스에서 탑승 구간만(양끝 포함) 잘라 '역' 접미사를 붙인 역명 목록으로.

    train_stop.stn_nm은 '대전'처럼 접미사가 없고 station·scenic_spot_segment는 '대전역'이라
    여기서 표기를 통일한다. 출발/도착역이 시퀀스에 없으면(역명 불일치·미적재) None.
    """
    if not rows:
        return None
    names = [r.stn_nm for r in rows]
    try:
        i = names.index(_strip_suffix(dep_station))
        j = names.index(_strip_suffix(arr_station))
    except ValueError:
        return None
    ordered = names[i:j + 1] if i <= j else names[j:i + 1][::-1]
    return [_with_suffix(n) for n in ordered]


def _with_suffix(name: str) -> str:
    """'대전' → '대전역' (이미 붙어 있으면 그대로)."""
    return name if name.endswith("역") else f"{name}역"


def _strip_suffix(name: str) -> str:
    """'대전역' → '대전' (train_stop 표기에 맞춘다)."""
    return name[:-1] if len(name) > 1 and name.endswith("역") else name


def _infer_train_no(db: Session, ride: Ride) -> str | None:
    """직접 입력 승차권의 열차번호를 TAGO 시간표에서 역추론한다. 못 찾으면 None.

    직접 입력 승차권에는 열차번호 입력칸이 없어(CLAUDE.md '승차권 두 갈래') 정차역을
    끌어올 열쇠가 없다. 출발역·도착역·출발 시각이 맞는 열차는 사실상 하나뿐이라 그걸로 되짚는다.

    시각은 TRAIN_MATCH_TOLERANCE_MINUTES만큼 열어 두고 **그 안에서 가장 가까운** 열차를
    고른다 — 사용자가 손으로 적은 값이라 1~2분 어긋나는 일이 흔한데, 정확 일치만 보면
    그 승차권은 풍경 알림을 통째로, 그것도 아무 신호 없이 못 받는다.

    실패도 캐시한다 — 1분마다 공공 API를 두드리지 않기 위해서다.
    """
    if ride.ticket_idx is None:
        return None
    cached = _TRAIN_NO_CACHE.get(ride.ticket_idx)
    now = now_kst().replace(tzinfo=None)
    if cached is not None and now - cached[1] <= _PROFILE_TTL:
        return cached[0]

    train_no = None
    try:
        dep = station_dao.get_by_idx(db, ride.dep_station_idx)
        arr = station_dao.get_by_idx(db, ride.arr_station_idx)
        if dep and arr and dep.nat_code and arr.nat_code:
            tolerance = timedelta(minutes=TRAIN_MATCH_TOLERANCE_MINUTES)
            # (시각 차, 열차번호) 중 최솟값 — 차가 같으면 열차번호 순으로 갈라 결과를 고정한다.
            candidates = [
                (abs(train["dep_time"].replace(tzinfo=None) - ride.dep_at), train["train_no"])
                for train in train_api.fetch_trains(
                    dep.nat_code, arr.nat_code, ride.dep_at.strftime("%Y%m%d")
                )
            ]
            within = [c for c in candidates if c[0] <= tolerance]
            if within:
                train_no = min(within)[1]
    except Exception as e:
        logger.debug("열차번호 역추론 실패 ticket=%s: %s", ride.ticket_idx, e)

    _TRAIN_NO_CACHE[ride.ticket_idx] = (train_no, now)
    return train_no


# ─────────────────────────────── 탑승 모으기 ───────────────────────────────

def _active_rides(db: Session, now: datetime) -> list[Ride]:
    """지금 운행 중인 탑승 전부 — 배치가 훑을 대상."""
    return [r for r in _rides_around(db, now) if _is_riding(r, now)]


def _is_riding(ride: Ride, now: datetime) -> bool:
    """지금 그 열차에 타고 있다고 볼 시각대인지.

    끝을 예정 도착 시각으로 딱 자르면 **지연된 열차의 마지막 구간이 통째로 사라진다** —
    20분 늦게 가는 열차는 도착 직전 풍경의 통과 시각도 20분 밀리는데, 그 시각엔 이미
    '도착한 것'으로 취급돼 대상에서 빠지기 때문이다. 그래서 뒤쪽은 보정값만큼,
    그리고 발송 창만큼 더 열어 둔다(창은 통과 시각이 도착 시각과 겹칠 때를 위한 여유).

    앞쪽은 예정 출발 시각 그대로다. 열차가 늦게 떠나면 통과 시각도 함께 밀려 어차피
    발송되지 않으니, 여기서 좁힐 이유가 없다.
    """
    tail = _offset_for(ride) + timedelta(minutes=SEND_WINDOW_MINUTES)
    return ride.dep_at <= now <= ride.arr_at + tail


def _current_ride_of_user(db: Session, user_idx: int, now: datetime) -> Ride | None:
    """사용자가 지금 타고 있는 탑승. 없으면 곧 출발할(UPCOMING_HOURS 이내) 가장 이른 것.

    앱은 탑승 전에도 계획을 미리 받아 로컬 알림을 예약할 수 있어야 한다.
    """
    rides = _rides_around(db, now, user_idx=user_idx)
    riding = [r for r in rides if _is_riding(r, now)]
    if riding:
        return min(riding, key=lambda r: r.dep_at)
    upcoming = [
        r for r in rides
        if now < r.dep_at <= now + timedelta(hours=UPCOMING_HOURS)
    ]
    return min(upcoming, key=lambda r: r.dep_at) if upcoming else None


def _rides_around(db: Session, now: datetime, user_idx: int | None = None) -> list[Ride]:
    """현재 시각 근처의 탑승을 두 출처에서 모은다.

    자정을 넘겨 달리는 열차가 있어 어제 출발분까지 함께 본다. 정확한 시각 판정은
    호출측이 하고, 여기서는 날짜로만 넉넉히 좁힌다(train_departure_service와 같은 방식).
    """
    dates: list[date] = sorted({(now - timedelta(days=1)).date(), now.date()})
    rides: list[Ride] = []

    for schedule, travel in schedule_dao.list_trains_covering(
        db, dates[0], dates[-1], user_idx=user_idx
    ):
        day = travel.start_date + timedelta(days=schedule.day_no - 1)
        dep_at = datetime.combine(day, schedule.start_time)
        arr_at = datetime.combine(day, schedule.end_time)
        # 도착이 출발보다 '이르면' 자정을 넘긴 것이다. 같은 경우는 넘기지 않는다 —
        # 24시간짜리 열차로 둔갑해 다음 날 종일 알림이 잡히기 때문이다(잘못 입력된 일정도
        # 있다). 소요 시간 0인 탑승은 _timetable이 빈 목록으로 걸러낸다.
        if arr_at < dep_at:
            arr_at += timedelta(days=1)
        if not schedule.dep_station or not schedule.arr_station:
            continue  # 역명이 없으면 정차 시퀀스를 못 찾는다
        rides.append(Ride(
            user_idx=schedule.user_idx,
            dep_station=_with_suffix(schedule.dep_station),
            arr_station=_with_suffix(schedule.arr_station),
            dep_at=dep_at, arr_at=arr_at,
            train_no=schedule.train_no,
            travel_idx=schedule.travel_idx,
            schedule_idx=schedule.schedule_idx,
        ))

    for ticket, dep_name, arr_name in ticket_dao.list_departing_on(
        db, dates, user_idx=user_idx
    ):
        rides.append(Ride(
            user_idx=ticket.user_idx,
            dep_station=dep_name,
            arr_station=arr_name,
            dep_at=datetime.combine(ticket.dep_date, ticket.dep_time),
            arr_at=datetime.combine(ticket.arr_date, ticket.arr_time),
            ticket_idx=ticket.ticket_idx,
            dep_station_idx=ticket.dep_station_idx,
            arr_station_idx=ticket.arr_station_idx,
        ))

    return rides
