"""열차 탑승 알림 서비스 — '10분 뒤 출발해요'를 1분마다 확인해 보낸다.

배치 컨텍스트라 요청 스코프가 아닌 자체 세션을 연다(trip_reminder_service와 같은 성격).
main.py의 루프가 이 함수를 호출한다.

**승차권이 두 곳에 나뉘어 있어 두 번 훑는다**(CLAUDE.md '승차권 두 갈래' 참조).
  - 추천 코스 승차권: schedule kind=train — 출발 일시 = 여행 시작일 + (day_no-1)일 + start_time
  - 직접 입력 승차권: ticket — 출발 일시 = dep_date + dep_time
조회 결과를 합쳐 내려주지는 않는다는 원칙은 그대로다. 여기서는 '알림 보낼 출발'을 모으는
것뿐이고, 두 출처는 이력에서도 schedule_idx / ticket_idx로 구분돼 남는다.

중복 발송은 notification_log로 막는다 — 출발 1건당 1회다. 10분 창을 1분마다 훑으므로
같은 열차를 열 번 집지만, 이력 검사와 부분 유니크 인덱스가 1회로 줄인다. 덕분에 서버를
재시작해도, 루프가 한두 번 밀려도 사용자에게 같은 알림이 두 번 가지 않는다.
"""
import logging
import math
from datetime import datetime, timedelta

from databases.daos import schedule_dao, ticket_dao
from databases.database import SessionLocal
from services import push_service
from utils.timezone import now_kst

logger = logging.getLogger(__name__)

# 출발 몇 분 전에 보낼지. 창의 길이이기도 하다 — '남은 시간이 0분 초과 10분 이하'면 보낸다.
LEAD_MINUTES = 10
# 루프 주기. 창(10분)보다 훨씬 짧아야 한 번 밀려도 놓치지 않는다.
CHECK_INTERVAL_SEC = 60


def send_departure_reminders(now: datetime | None = None) -> int:
    """곧 출발하는 열차의 소유자에게 탑승 알림을 보내고 발송 건수를 반환한다.

    now를 주면 그 시각 기준으로 판정한다(테스트용). 기본은 현재 KST다.

    이미 보낸 출발은 push_service.notify_train_departure가 걸러낸다(멱등). 개별 발송
    실패도 거기서 삼키므로 한 사람 때문에 나머지가 밀리지 않는다.
    """
    # DB의 시각 컬럼이 naive KST wall-clock이므로 현재 시각도 tzinfo를 떼고 비교한다.
    base = (now or now_kst()).replace(tzinfo=None)
    deadline = base + timedelta(minutes=LEAD_MINUTES)

    db = SessionLocal()
    try:
        sent = 0
        for dep in _departures_between(db, base, deadline):
            if push_service.notify_train_departure(db, **dep):
                sent += 1
        return sent
    finally:
        db.close()


def _departures_between(db, after: datetime, until: datetime) -> list[dict]:
    """(after, until] 구간에 출발하는 열차를 push_service 인자 형태로 모은다.

    경계가 열림/닫힘인 이유: 이미 떠난 열차(<= after)에는 보내지 않고, 정확히 10분 뒤
    출발은 보낸다. 서버가 한동안 꺼져 있었다면 남은 시간이 10분보다 짧은 열차도 이
    구간에 들어와 '늦은 알림'이 나가는데, 못 보내는 것보다 낫다고 보고 허용한다.
    """
    # 창이 자정을 넘으면 출발 날짜가 이틀에 걸친다.
    dates = sorted({after.date(), until.date()})
    departures = []

    for schedule, travel in schedule_dao.list_trains_covering(db, dates[0], dates[-1]):
        dep_at = datetime.combine(
            travel.start_date + timedelta(days=schedule.day_no - 1), schedule.start_time
        )
        if not after < dep_at <= until:
            continue
        departures.append({
            "user_idx": schedule.user_idx,
            # dep_station은 nullable이다. 비었으면 그대로 None으로 넘겨 문구에서 역명을
            # 빼게 한다 — 여행 제목 같은 다른 값으로 메우면 '부산 여행역'이 돼 버린다.
            "dep_station": schedule.dep_station,
            "dep_at": dep_at,
            "minutes_left": _minutes_left(after, dep_at),
            "train_label": _train_label(schedule),
            "seat_label": _seat_label(schedule.car_no, schedule.seat_no),
            "travel_idx": schedule.travel_idx,
            "schedule_idx": schedule.schedule_idx,
        })

    for ticket, dep_station, _arr_station in ticket_dao.list_departing_on(db, dates):
        dep_at = datetime.combine(ticket.dep_date, ticket.dep_time)
        if not after < dep_at <= until:
            continue
        departures.append({
            "user_idx": ticket.user_idx,
            "dep_station": dep_station,
            "dep_at": dep_at,
            "minutes_left": _minutes_left(after, dep_at),
            # 직접 입력 승차권엔 열차번호·등급 입력칸이 없다.
            "train_label": None,
            "seat_label": _seat_label(ticket.car_no, ticket.seat_no),
            "ticket_idx": ticket.ticket_idx,
        })

    return departures


def _minutes_left(now: datetime, dep_at: datetime) -> int:
    """출발까지 남은 분(올림, 최소 1). 문구에 그대로 쓴다.

    LEAD_MINUTES를 그대로 쓰지 않는 이유: 루프가 밀리거나 서버가 잠깐 꺼져 있었으면
    남은 시간이 10분보다 짧은 열차에도 알림이 나간다. 그 때 '10분 뒤'라고 하면 안 된다.
    올림이라 9분 30초 남았으면 '10분 뒤'다 — 1분 주기로 도는 루프의 정상 케이스가
    9~10분 사이라 대부분 10으로 떨어진다.
    """
    return max(1, math.ceil((dep_at - now).total_seconds() / 60))


def _train_label(schedule) -> str | None:
    """'KTX 101' 형태의 열차 표기. 등급·번호가 둘 다 없으면 None."""
    parts = [p for p in (schedule.train_grade, schedule.train_no) if p]
    return " ".join(parts) or None


def _seat_label(car_no: str | None, seat_no: str | None) -> str | None:
    """'3호차 12A' 형태의 좌석 표기. 둘 다 없으면 None (선택 입력이라 흔하다)."""
    parts = []
    if car_no:
        parts.append(f"{car_no}호차")
    if seat_no:
        parts.append(seat_no)
    return " ".join(parts) or None
